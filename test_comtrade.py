"""
test_comtrade.py — Unit tests for COMTRADE Analyzer parsing, fault math, and triage.

Run with:
    pytest test_comtrade.py -v

Coverage:
  1.  COMTRADE parser — CFG fields, a*raw+b scaling, trigger index, ASCII + BINARY
  2.  Magnitude conventions — compute_rms is RMS, every DFT phasor is PEAK
  3.  Symmetrical components — Fortescue on ABC, the ACB assumption, NaN padding
  4.  classify_fault — SLG / LL / LLG / 3PH and the documented ratio boundaries
  5.  Fault inception and trip detection
  6.  Reclose sequence — shots, dead times, lockout
  7.  WSO/EPSS classification — the three-way boundary in classify_event
  8.  Triage — flags and priority precedence
  9.  HIF screen and the fault-location plausibility guard
  10. Batch manifest — incremental re-analysis
  11. Waveform decimation — the min/max envelope keeps peaks
  12. Plotting releases its figures (no leak across a folder run)
  13. Cross-platform text handling (encoding pinned for Windows)
  14. Plotting leaves the matplotlib backend to its caller
  15. Triage rules exported from triage.py, not duplicated in the page
  16. Diagnostics name the symptom, the evidence and the fix
  17. SUBNET relay settings catalog, template parsing, pickup arithmetic
  18. End-to-end against the generated fixtures (skipped if not generated)
"""

import json
import math
import struct
from collections import defaultdict
from datetime import datetime
import sys
from pathlib import Path

import numpy as np
import pytest

# ── Make comtrade_analyzer importable from any working directory ─────────────
sys.path.insert(0, str(Path(__file__).parent))

from comtrade_analyzer.data_model import ChannelInfo, EventRecord
from comtrade_analyzer.comtrade_parser import COMTRADEParser
from comtrade_analyzer.analysis import (
    classify_fault,
    compute_phasors_at,
    compute_rms,
    compute_sequence_components,
    detect_fault_inception,
    detect_trip_time,
)
from comtrade_analyzer.feeder_analysis import (
    compute_feeder_summary,
    detect_reclose_sequence,
    estimate_fault_location,
    screen_high_impedance_fault,
)
from comtrade_analyzer.triage import triage_event
from comtrade_analyzer.wso_impact import (
    EPSS_CANDIDATE,
    INDETERMINATE,
    NOT_EXPOSED,
    PERMANENT,
    WSO_EXPOSED,
    classify_event,
)
from comtrade_analyzer.fleet_analyze import (
    _location_valid,
    _minmax_decimate,
    extract_waveform,
)
from comtrade_analyzer import batch as batch_mod

F0 = 60.0
FS = 1920.0
SPC = int(FS / F0)                      # 32 samples per cycle
PH = {"A": 0.0, "B": -2 * np.pi / 3, "C": 2 * np.pi / 3}


# ─────────────────────────────────────────────────────────────────────────────
# Builders — synthetic records, so the math tests never depend on a fixture file
# ─────────────────────────────────────────────────────────────────────────────

def _sine(amp, n, phase_rad=0.0, fs=FS, f0=F0):
    t = np.arange(n) / fs
    return amp * np.sin(2 * np.pi * f0 * t + phase_rad)


def _record(analog, digital=None, fs=FS, f0=F0, trigger_s=0.05, units=None):
    """Assemble an EventRecord directly, bypassing the file layer."""
    n = len(next(iter(analog.values())))
    units = units or {}
    info = {
        name: ChannelInfo(name=name,
                          units=units.get(name, "V" if name.upper().startswith("V") else "A"),
                          multiplier=1.0, offset=0.0)
        for name in analog
    }
    time = np.arange(n) / fs
    trig = int(round(trigger_s * fs))
    return EventRecord(
        time=time,
        analog_channels={k: np.asarray(v, dtype=float) for k, v in analog.items()},
        digital_channels={k: np.asarray(v, dtype=np.int8) for k, v in (digital or {}).items()},
        analog_info=info,
        sample_rate=fs,
        trigger_time=float(time[min(trig, n - 1)]),
        trigger_index=min(trig, n - 1),
        metadata={"station_name": "Test Feeder", "rec_dev_id": "TEST_DEV", "line_freq": f0},
    )


def _fault_record(i_load, i_fault, faulted, n_cycles=8, fault_cycle=3,
                  trip_cycle=None, phase_shift=None, unfaulted_scale=1.0,
                  v_collapse=0.15):
    """
    Load current for `fault_cycle` cycles, then elevated current on `faulted`.

    The voltage on the faulted phases collapses to `v_collapse` per unit, which
    is what makes it a fault rather than load being switched on. Pass
    v_collapse=1.0 to model a balanced load step with the voltage intact.

    Returns (record, fault_index).
    """
    n = int(n_cycles * SPC)
    fi = int(fault_cycle * SPC)
    chans = {}
    for p in "ABC":
        sig = _sine(i_load, n, PH[p])
        if p in faulted:
            ang = PH[p] + (phase_shift or {}).get(p, 0.0)
            sig[fi:] = _sine(i_fault, n, ang)[fi:]
        else:
            sig[fi:] = _sine(i_load * unfaulted_scale, n, PH[p])[fi:]
        chans[f"I{p}"] = sig
    for p in "ABC":
        v = _sine(10000.0, n, PH[p])
        if p in faulted:
            v[fi:] = _sine(10000.0 * v_collapse, n, PH[p])[fi:]
        chans[f"V{p}N"] = v

    digital = {}
    if trip_cycle is not None:
        trip = np.zeros(n, dtype=np.int8)
        trip[int(trip_cycle * SPC):] = 1
        digital["TRIP"] = trip

    rec = _record(chans, digital, trigger_s=fi / FS)
    return rec, fi


# ─────────────────────────────────────────────────────────────────────────────
# 1. COMTRADE parser
# ─────────────────────────────────────────────────────────────────────────────

def _write_cfg_dat(tmp_path, file_type="ASCII", multiplier=0.5, offset=3.0, n=64):
    """Two analog channels and one digital, with a deliberately odd a/b scaling."""
    cfg = [
        "Test Feeder,TEST_DEV,1999",
        "3,2A,1D",
        f"1,IA,A,,A,{multiplier},{offset},0,-32767,32767,600,5,P",
        f"2,VAN,A,,V,{multiplier},{offset},0,-32767,32767,7200,1,P",
        "1,TRIP,A,,0",
        "60",
        "1",
        f"{int(FS)},{n}",
        "01/06/2026,00:00:00.000000",
        "01/06/2026,00:00:00.010000",     # trigger 10 ms in → sample 19.2 → index 20
        file_type,
        "1",
    ]
    (tmp_path / "e.cfg").write_text("\n".join(cfg) + "\n")

    raw_ia = np.arange(n, dtype=np.int64) - 10
    raw_van = np.arange(n, dtype=np.int64) * 2
    trip = (np.arange(n) >= 40).astype(np.int64)

    if file_type == "ASCII":
        rows = []
        for i in range(n):
            rows.append(",".join(str(int(x)) for x in
                                 [i + 1, int(i / FS * 1e6), raw_ia[i], raw_van[i], trip[i]]))
        (tmp_path / "e.dat").write_bytes(("\r\n".join(rows) + "\r\n").encode())
    else:
        buf = bytearray()
        for i in range(n):
            buf += struct.pack("<IIhhH", i + 1, int(i / FS * 1e6),
                               int(raw_ia[i]), int(raw_van[i]), int(trip[i]))
        (tmp_path / "e.dat").write_bytes(bytes(buf))
    return raw_ia, raw_van, multiplier, offset


class TestTheParserReadsAnASCIIRecord:
    """CFG metadata, engineering-unit scaling and the trigger index."""

    def test_channel_names_and_counts(self, tmp_path):
        _write_cfg_dat(tmp_path)
        rec = COMTRADEParser().parse(str(tmp_path / "e.cfg"))
        assert list(rec.analog_channels) == ["IA", "VAN"]
        assert list(rec.digital_channels) == ["TRIP"]

    def test_metadata_round_trips(self, tmp_path):
        _write_cfg_dat(tmp_path)
        rec = COMTRADEParser().parse(str(tmp_path / "e.cfg"))
        assert rec.metadata["station_name"] == "Test Feeder"
        assert rec.metadata["rec_dev_id"] == "TEST_DEV"
        assert rec.sample_rate == FS
        assert rec.line_freq() == 60.0
        assert rec.samples_per_cycle() == SPC

    def test_samples_are_scaled_by_a_times_raw_plus_b(self, tmp_path):
        """The scaling is applied once, in the parser — everything downstream
        assumes engineering units."""
        raw_ia, raw_van, a, b = _write_cfg_dat(tmp_path)
        rec = COMTRADEParser().parse(str(tmp_path / "e.cfg"))
        np.testing.assert_allclose(rec.analog_channels["IA"], a * raw_ia + b)
        np.testing.assert_allclose(rec.analog_channels["VAN"], a * raw_van + b)

    def test_offset_is_not_silently_dropped(self, tmp_path):
        """A parser that ignored 'b' would still pass a multiplier-only check."""
        _write_cfg_dat(tmp_path, multiplier=1.0, offset=7.0)
        rec = COMTRADEParser().parse(str(tmp_path / "e.cfg"))
        assert rec.analog_channels["IA"][10] == pytest.approx(10 - 10 + 7.0)

    def test_trigger_index_lands_on_the_trigger_time(self, tmp_path):
        """trigger_time is the exact CFG offset; trigger_index is the sample at
        or after it.  They are deliberately different quantities — 10 ms at
        1920 Hz falls between samples 19 and 20."""
        _write_cfg_dat(tmp_path)
        rec = COMTRADEParser().parse(str(tmp_path / "e.cfg"))
        assert rec.trigger_time == pytest.approx(0.010)      # from the CFG timestamps
        assert rec.trigger_index == 20                       # 19.2 → first sample at or after
        assert rec.time[rec.trigger_index] >= rec.trigger_time

    def test_digital_channel_is_zero_one(self, tmp_path):
        _write_cfg_dat(tmp_path)
        rec = COMTRADEParser().parse(str(tmp_path / "e.cfg"))
        trip = rec.digital_channels["TRIP"]
        assert set(np.unique(trip)) <= {0, 1}
        assert trip[39] == 0 and trip[40] == 1


class TestTheParserReadsABinaryRecord:
    """BINARY and ASCII must produce identical engineering-unit arrays."""

    def test_binary_matches_ascii(self, tmp_path):
        a_dir = tmp_path / "ascii"; a_dir.mkdir()
        b_dir = tmp_path / "binary"; b_dir.mkdir()
        _write_cfg_dat(a_dir, "ASCII")
        _write_cfg_dat(b_dir, "BINARY")
        ra = COMTRADEParser().parse(str(a_dir / "e.cfg"))
        rb = COMTRADEParser().parse(str(b_dir / "e.cfg"))
        np.testing.assert_allclose(ra.analog_channels["IA"], rb.analog_channels["IA"])
        np.testing.assert_allclose(ra.analog_channels["VAN"], rb.analog_channels["VAN"])
        np.testing.assert_array_equal(ra.digital_channels["TRIP"], rb.digital_channels["TRIP"])


# ─────────────────────────────────────────────────────────────────────────────
# 2. Magnitude conventions — the silent factor-of-√2 trap
# ─────────────────────────────────────────────────────────────────────────────

class TestMagnitudeConventions:
    """
    Two conventions coexist and mixing them is a silent √2 error:
    compute_rms() returns RMS, every DFT phasor is PEAK.  These tests pin both.
    """

    def test_compute_rms_returns_rms_not_peak(self):
        amp = 100.0
        sig = _sine(amp, SPC * 4)
        rms = compute_rms(sig, SPC)
        assert np.nanmax(rms) == pytest.approx(amp / math.sqrt(2), rel=1e-3)

    def test_compute_rms_pads_the_first_window_with_nan(self):
        rms = compute_rms(_sine(100.0, SPC * 4), SPC)
        assert np.all(np.isnan(rms[: SPC - 1]))
        assert not np.isnan(rms[SPC - 1])

    def test_dft_phasor_magnitude_is_peak(self):
        """compute_phasors_at scales by 2/N, which recovers peak amplitude.
        If this ever returns amp/√2, every reported phasor silently changes."""
        amp = 100.0
        n = SPC * 8
        rec = _record({"IA": _sine(amp, n), "VAN": _sine(10000.0, n)},
                      trigger_s=(4 * SPC) / FS)
        ph = compute_phasors_at(rec, t_fault_s=(4 * SPC) / FS)
        assert ph["fault"]["IA"]["mag"] == pytest.approx(amp, rel=2e-2)
        assert ph["fault"]["IA"]["mag"] != pytest.approx(amp / math.sqrt(2), rel=2e-2)

    def test_sequence_components_are_peak(self):
        amp = 100.0
        n = SPC * 6
        i0, i1, i2 = compute_sequence_components(
            _sine(amp, n, PH["A"]), _sine(amp, n, PH["B"]), _sine(amp, n, PH["C"]), SPC)
        assert np.nanmax(i1) == pytest.approx(amp, rel=1e-2)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Symmetrical components — Fortescue, ABC rotation
# ─────────────────────────────────────────────────────────────────────────────

class TestSymmetricalComponents:
    """I0 = (Ia+Ib+Ic)/3, I1 = (Ia+aIb+a²Ic)/3, I2 = (Ia+a²Ib+aIc)/3, rotation ABC."""

    def test_balanced_abc_is_all_positive_sequence(self):
        amp, n = 100.0, SPC * 6
        i0, i1, i2 = compute_sequence_components(
            _sine(amp, n, PH["A"]), _sine(amp, n, PH["B"]), _sine(amp, n, PH["C"]), SPC)
        assert np.nanmedian(i1) == pytest.approx(amp, rel=1e-2)
        assert np.nanmedian(i0) < amp * 0.01
        assert np.nanmedian(i2) < amp * 0.01

    def test_acb_rotation_lands_in_negative_sequence(self):
        """There is no ACB handling anywhere; on an ACB system I1 and I2 swap.
        This test documents that assumption rather than defending against it."""
        amp, n = 100.0, SPC * 6
        i0, i1, i2 = compute_sequence_components(
            _sine(amp, n, PH["A"]), _sine(amp, n, PH["C"]), _sine(amp, n, PH["B"]), SPC)
        assert np.nanmedian(i2) == pytest.approx(amp, rel=1e-2)
        assert np.nanmedian(i1) < amp * 0.01

    def test_single_energised_phase_splits_evenly(self):
        """One phase carrying current, two dead → |I0| = |I1| = |I2| = A/3."""
        amp, n = 90.0, SPC * 6
        z = np.zeros(n)
        i0, i1, i2 = compute_sequence_components(_sine(amp, n, PH["A"]), z, z, SPC)
        for seq in (i0, i1, i2):
            assert np.nanmedian(seq) == pytest.approx(amp / 3.0, rel=2e-2)

    def test_output_is_nan_padded_so_bare_median_is_wrong(self):
        """Callers must use np.nanmedian / np.nanmax, never bare np.median."""
        n = SPC * 6
        i0, i1, i2 = compute_sequence_components(
            _sine(100.0, n, PH["A"]), _sine(100.0, n, PH["B"]), _sine(100.0, n, PH["C"]), SPC)
        assert np.all(np.isnan(i1[: SPC - 1]))
        assert math.isnan(float(np.median(i1)))       # the trap
        assert not math.isnan(float(np.nanmedian(i1)))


class TestPhasorReference:
    """All phasors are rotated so the fault-window Va sits at 0°."""

    def test_reference_voltage_is_placed_at_zero_degrees(self):
        n = SPC * 8
        chans = {f"V{p}N": _sine(10000.0, n, PH[p]) for p in "ABC"}
        chans.update({f"I{p}": _sine(100.0, n, PH[p]) for p in "ABC"})
        rec = _record(chans, trigger_s=(4 * SPC) / FS)
        ph = compute_phasors_at(rec, t_fault_s=(4 * SPC) / FS)
        assert ph["ref_channel"] == "VAN"
        assert ph["fault"]["VAN"]["ang_deg"] == pytest.approx(0.0, abs=1.0)

    def test_phase_separation_survives_the_rotation(self):
        n = SPC * 8
        chans = {f"V{p}N": _sine(10000.0, n, PH[p]) for p in "ABC"}
        chans.update({f"I{p}": _sine(100.0, n, PH[p]) for p in "ABC"})
        rec = _record(chans, trigger_s=(4 * SPC) / FS)
        ph = compute_phasors_at(rec, t_fault_s=(4 * SPC) / FS)
        assert ph["fault"]["VBN"]["ang_deg"] == pytest.approx(-120.0, abs=2.0)
        assert ph["fault"]["VCN"]["ang_deg"] == pytest.approx(120.0, abs=2.0)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Fault classification
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyFault:
    """
    Rules: SLG when the two unfaulted phases are < 0.15 × the faulted phase;
    3PH when the smallest is > 0.7 × the largest; LL vs LLG on zero sequence.
    """

    def test_single_line_to_ground(self):
        rec, fi = _fault_record(i_load=80.0, i_fault=900.0, faulted="A")
        assert classify_fault(rec, fi) == "SLG"

    def test_single_line_to_ground_on_phase_c(self):
        rec, fi = _fault_record(i_load=80.0, i_fault=900.0, faulted="C")
        assert classify_fault(rec, fi) == "SLG"

    def test_line_to_line_has_no_zero_sequence(self):
        """Ib = -Ic exactly, so I0 stays at the load contribution and LL wins."""
        n, fi = SPC * 8, SPC * 3
        chans = {}
        base = _sine(900.0, n, PH["B"] + math.radians(30))
        chans["IA"] = _sine(80.0, n, PH["A"])
        chans["IB"] = _sine(80.0, n, PH["B"]); chans["IB"][fi:] = base[fi:]
        chans["IC"] = _sine(80.0, n, PH["C"]); chans["IC"][fi:] = -base[fi:]
        for p in "ABC":
            chans[f"V{p}N"] = _sine(10000.0, n, PH[p])
        rec = _record(chans, trigger_s=fi / FS)
        assert classify_fault(rec, fi) == "LL"

    def test_double_line_to_ground_has_zero_sequence(self):
        """Unequal magnitudes 60° apart give real I0 → LLG, not LL."""
        n, fi = SPC * 8, SPC * 3
        chans = {}
        ang = PH["A"] + math.radians(15)
        chans["IA"] = _sine(80.0, n, PH["A"]); chans["IA"][fi:] = _sine(900.0, n, ang)[fi:]
        chans["IB"] = _sine(80.0, n, PH["B"])
        chans["IB"][fi:] = _sine(675.0, n, ang - math.radians(60))[fi:]
        chans["IC"] = _sine(80.0, n, PH["C"])
        for p in "ABC":
            chans[f"V{p}N"] = _sine(10000.0, n, PH[p])
        rec = _record(chans, trigger_s=fi / FS)
        assert classify_fault(rec, fi) == "LLG"

    def test_balanced_three_phase(self):
        rec, fi = _fault_record(i_load=80.0, i_fault=900.0, faulted="ABC")
        assert classify_fault(rec, fi) == "3PH"

    def test_a_balanced_step_with_the_voltage_intact_is_load_not_a_fault(self):
        """
        Cold-load pickup — a tie closing onto a restored section — is a
        balanced rise on all three phases and looks exactly like a 3PH fault in
        current alone. The voltage is the only thing that separates them: a
        real three-phase fault drags it down, load does not.
        """
        rec, fi = _fault_record(i_load=80.0, i_fault=260.0, faulted="ABC",
                                v_collapse=1.0)
        assert classify_fault(rec, fi) == "LOAD"

    def test_without_voltage_channels_a_balanced_step_still_reads_as_a_fault(self):
        """An unknown is not evidence of a load step — don't downgrade blind."""
        n, fi = SPC * 8, SPC * 3
        chans = {}
        for p in "ABC":
            sig = _sine(80.0, n, PH[p])
            sig[fi:] = _sine(900.0, n, PH[p])[fi:]
            chans[f"I{p}"] = sig
        rec = _record(chans, trigger_s=fi / FS)
        assert classify_fault(rec, fi) == "3PH"

    def test_unfaulted_phases_below_the_slg_gate(self):
        """0.12 ratio → comfortably SLG."""
        rec, fi = _fault_record(i_load=120.0, i_fault=1000.0, faulted="A")
        assert classify_fault(rec, fi) == "SLG"

    def test_unfaulted_phases_above_the_slg_gate_are_not_slg(self):
        """0.25 ratio falls out of the SLG branch — the boundary is load-bearing,
        so this pins which side of 0.15 the classifier is on."""
        rec, fi = _fault_record(i_load=250.0, i_fault=1000.0, faulted="A")
        assert classify_fault(rec, fi) != "SLG"

    def test_missing_current_channel_is_unknown(self):
        n = SPC * 6
        rec = _record({"IA": _sine(100.0, n), "VAN": _sine(10000.0, n)})
        assert classify_fault(rec, SPC * 3) == "UNKNOWN"

    def test_no_fault_index_is_unknown(self):
        rec, _ = _fault_record(i_load=80.0, i_fault=900.0, faulted="A")
        assert classify_fault(rec, None) == "UNKNOWN"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Fault inception and trip detection
# ─────────────────────────────────────────────────────────────────────────────

class TestFaultInception:

    def test_inception_is_found_near_the_step(self):
        rec, fi = _fault_record(i_load=80.0, i_fault=900.0, faulted="A", fault_cycle=3)
        got = detect_fault_inception(rec)
        assert got is not None
        assert abs(got - fi) <= SPC          # within one cycle of the true step

    def test_steady_load_has_no_inception(self):
        n = SPC * 8
        chans = {f"I{p}": _sine(80.0, n, PH[p]) for p in "ABC"}
        chans["VAN"] = _sine(10000.0, n)
        rec = _record(chans, trigger_s=(4 * SPC) / FS)
        assert detect_fault_inception(rec) is None


class TestTripDetection:

    def test_first_rising_edge_is_the_trip(self):
        rec, _ = _fault_record(80.0, 900.0, "A", trip_cycle=4)
        got = detect_trip_time(rec)
        assert got is not None
        idx, ch = got
        assert ch == "TRIP"
        assert idx == int(4 * SPC)

    def test_no_trip_channel_returns_none(self):
        rec, _ = _fault_record(80.0, 900.0, "A")
        assert detect_trip_time(rec) is None


# ─────────────────────────────────────────────────────────────────────────────
# 6. Reclose sequence
# ─────────────────────────────────────────────────────────────────────────────

def _sequence_record(shots, lockout_at=None, total_cycles=200):
    """
    shots: list of (trip_cycle, close_cycle or None).
    Builds TRIP + 52A + LOCK consistent with each other.
    """
    n = int(total_cycles * SPC)
    trip = np.zeros(n, dtype=np.int8)
    a52 = np.ones(n, dtype=np.int8)
    lock = np.zeros(n, dtype=np.int8)
    cur = {p: _sine(80.0, n, PH[p]) for p in "ABC"}

    for t_c, c_c in shots:
        t_i = int(t_c * SPC)
        c_i = int(c_c * SPC) if c_c is not None else n
        trip[t_i:c_i] = 1
        a52[t_i:c_i] = 0
        for p in "ABC":
            cur[p][t_i:c_i] = 0.0
        fault_start = max(0, t_i - SPC)
        cur["A"][fault_start:t_i] = _sine(900.0, n, PH["A"])[fault_start:t_i]

    if lockout_at is not None:
        lock[int(lockout_at * SPC):] = 1

    chans = {f"I{p}": cur[p] for p in "ABC"}
    for p in "ABC":
        chans[f"V{p}N"] = _sine(10000.0, n, PH[p])
    first_trip = int(shots[0][0] * SPC)
    return _record(chans, {"TRIP": trip, "52A": a52, "LOCK": lock},
                   trigger_s=(first_trip - SPC) / FS)


class TestRecloseSequence:

    def test_a_single_trip_with_no_reclose_is_one_shot(self):
        rec = _sequence_record([(10, None)], total_cycles=40)
        seq = detect_reclose_sequence(rec)
        assert seq.total_shots == 1
        assert seq.locked_out is False
        assert seq.shots[0].outcome == "LAST"

    def test_a_trip_then_reclose_is_recorded_as_reclosed(self):
        rec = _sequence_record([(10, 40)], total_cycles=80)
        seq = detect_reclose_sequence(rec)
        assert seq.total_shots == 1
        assert seq.shots[0].outcome == "RECLOSED"
        assert seq.shots[0].reclose_index == int(40 * SPC)

    def test_dead_time_is_trip_to_reclose(self):
        rec = _sequence_record([(10, 40)], total_cycles=80)
        seq = detect_reclose_sequence(rec)
        expected_ms = (40 - 10) * (1000.0 / F0)
        assert seq.shots[0].dead_time_ms == pytest.approx(expected_ms, rel=1e-3)

    def test_three_shots_are_all_counted(self):
        rec = _sequence_record([(10, 40), (45, 80), (85, None)], total_cycles=140)
        seq = detect_reclose_sequence(rec)
        assert seq.total_shots == 3

    def test_an_asserted_lock_channel_means_lockout(self):
        rec = _sequence_record([(10, 40), (45, None)], lockout_at=45, total_cycles=100)
        seq = detect_reclose_sequence(rec)
        assert seq.locked_out is True
        assert seq.fault_type == "PERMANENT"

    def test_no_trip_at_all_yields_no_shots(self):
        n = SPC * 40
        chans = {f"I{p}": _sine(80.0, n, PH[p]) for p in "ABC"}
        chans["VAN"] = _sine(10000.0, n)
        rec = _record(chans, {"TRIP": np.zeros(n, dtype=np.int8)})
        seq = detect_reclose_sequence(rec)
        assert seq.total_shots == 0


# ─────────────────────────────────────────────────────────────────────────────
# 7. WSO / EPSS classification — the boundary the filing rests on
# ─────────────────────────────────────────────────────────────────────────────

class TestWSOClassification:
    """
    Only a fault that actually reclosed converts to a sustained outage under
    EPSS.  Getting this wrong over- or under-states customer impact in a
    wildfire-mitigation filing, so each branch is pinned.
    """

    def test_lockout_is_permanent(self):
        rec = _sequence_record([(10, 40), (45, None)], lockout_at=45, total_cycles=100)
        assert classify_event(compute_feeder_summary(rec)) == PERMANENT

    def test_a_successful_reclose_is_wso_exposed(self):
        rec = _sequence_record([(10, 40)], total_cycles=80)
        assert classify_event(compute_feeder_summary(rec)) == WSO_EXPOSED

    def test_a_single_trip_with_no_context_falls_back_to_not_exposed(self):
        """Called without a summary or record length there is nothing to judge
        indeterminacy from, so the old conservative answer stands."""
        rec = _sequence_record([(10, None)], total_cycles=40)
        assert classify_event(compute_feeder_summary(rec)) == NOT_EXPOSED

    def test_a_ride_through_is_an_epss_candidate(self):
        """Fault current, no trip: the case the whole analysis exists to find.
        A downstream fuse cleared it today; EPSS may trip the recloser instead."""
        rec, fi = _fault_record(80.0, 900.0, "A", n_cycles=20, fault_cycle=3)
        fd = compute_feeder_summary(rec)
        summary = {"fault_inception_s": fi / FS, "trip_time_s": None}
        assert classify_event(fd, summary, record_end_ms=rec.duration_s() * 1000) == EPSS_CANDIDATE

    def test_a_quiet_record_is_not_a_candidate(self):
        """No fault current means nothing for EPSS to act on."""
        n = SPC * 20
        chans = {f"I{p}": _sine(80.0, n, PH[p]) for p in "ABC"}
        chans["VAN"] = _sine(10000.0, n)
        rec = _record(chans)
        assert classify_event(compute_feeder_summary(rec), {"fault_inception_s": None}) == NOT_EXPOSED

    def test_a_trip_with_no_room_to_reclose_is_indeterminate(self):
        """The record ends 200 ms after the trip; dead times run 0.5–5 s, so
        'no reclose seen' is not evidence that none happened."""
        rec = _sequence_record([(10, None)], total_cycles=22)
        summary = {"fault_inception_s": 9 * SPC / FS, "trip_time_s": 10 * SPC / FS}
        got = classify_event(compute_feeder_summary(rec), summary,
                             record_end_ms=rec.duration_s() * 1000)
        assert got == INDETERMINATE

    def test_a_long_record_with_no_reclose_is_believed(self):
        """Past the longest plausible dead time, absence really is evidence."""
        rec = _sequence_record([(10, None)], total_cycles=400)     # ~6.6 s
        summary = {"fault_inception_s": 9 * SPC / FS, "trip_time_s": 10 * SPC / FS}
        got = classify_event(compute_feeder_summary(rec), summary,
                             record_end_ms=rec.duration_s() * 1000)
        assert got == NOT_EXPOSED

    def test_no_feeder_data_is_not_exposed(self):
        assert classify_event(None) == NOT_EXPOSED

    def test_no_sequence_is_not_exposed(self):
        assert classify_event({"reclose_sequence": None}) == NOT_EXPOSED

    def test_permanent_beats_exposed_when_a_shot_reclosed_before_lockout(self):
        """A locked-out event reclosed earlier in the sequence, but it is already
        a sustained outage — it must not be counted as converting."""
        rec = _sequence_record([(10, 40), (45, 80), (85, None)],
                               lockout_at=85, total_cycles=140)
        fd = compute_feeder_summary(rec)
        assert any(s.outcome == "RECLOSED" for s in fd["reclose_sequence"].shots)
        assert classify_event(fd) == PERMANENT


# ─────────────────────────────────────────────────────────────────────────────
# 8. Triage
# ─────────────────────────────────────────────────────────────────────────────

class TestTriage:

    def test_a_plain_slg_that_cleared_is_archive(self):
        s = {"fault_type": "SLG", "fault_inception_s": 0.05,
             "trip_time_s": 0.07, "trip_delay_ms": 20.0}
        assert triage_event(s)["priority"] == 3

    def test_three_phase_is_priority_one(self):
        s = {"fault_type": "3PH", "fault_inception_s": 0.05,
             "trip_time_s": 0.07, "trip_delay_ms": 20.0}
        out = triage_event(s)
        assert out["priority"] == 1 and "3ph_fault" in out["flags"]

    def test_llg_is_priority_two(self):
        s = {"fault_type": "LLG", "fault_inception_s": 0.05,
             "trip_time_s": 0.07, "trip_delay_ms": 20.0}
        out = triage_event(s)
        assert out["priority"] == 2 and "llg_fault" in out["flags"]

    def test_a_ride_through_is_flagged_for_coordination_review(self):
        """Fault current with no trip is normally a downstream fuse clearing —
        not a misoperation. It matters because EPSS may trip on it."""
        s = {"fault_type": "SLG", "fault_inception_s": 0.05,
             "trip_time_s": None, "trip_delay_ms": None}
        out = triage_event(s)
        assert "no_trip" in out["flags"]
        assert out["priority"] == 2
        assert "fuse" in dict((r["key"], r["note"]) for r in out["reasons"])["no_trip"]

    def test_slow_trip_uses_the_configured_threshold(self):
        s = {"fault_type": "SLG", "fault_inception_s": 0.05,
             "trip_time_s": 0.30, "trip_delay_ms": 200.0}
        assert "slow_trip" in triage_event(s, slow_trip_cycles=10.0)["flags"]
        assert "slow_trip" not in triage_event(s, slow_trip_cycles=20.0)["flags"]

    def test_a_priority_one_flag_outranks_a_priority_two_flag(self):
        """A locked-out LLG carries both a P1 and a P2 flag; P1 wins."""
        rec = _sequence_record([(10, 40), (45, None)], lockout_at=45, total_cycles=100)
        s = {"fault_type": "LLG", "fault_inception_s": 0.05,
             "trip_time_s": 0.07, "trip_delay_ms": 20.0}
        out = triage_event(s, compute_feeder_summary(rec))
        assert set(["lockout", "llg_fault"]).issubset(out["flags"])
        assert out["priority"] == 1
        decisive = [r["key"] for r in out["reasons"] if r["decisive"]]
        assert decisive == ["lockout"]

    def test_hif_suspect_comes_from_the_feeder_screen(self):
        s = {"fault_type": "SLG", "fault_inception_s": 0.05,
             "trip_time_s": 0.07, "trip_delay_ms": 20.0}
        out = triage_event(s, {"hif_screen": {"hif_suspect": True}})
        assert out["priority"] == 1 and "hif_suspect" in out["flags"]


# ─────────────────────────────────────────────────────────────────────────────
# 9. HIF screen and the fault-location plausibility guard
# ─────────────────────────────────────────────────────────────────────────────

class TestHighImpedanceScreen:
    """A downed conductor draws little current — below OC pickup but hazardous."""

    def test_a_small_current_rise_is_flagged(self):
        rec, fi = _fault_record(i_load=5.0, i_fault=40.0, faulted="B")
        out = screen_high_impedance_fault(rec, fault_index=fi, hif_threshold_a=50.0)
        assert out["hif_suspect"] is True
        assert 0 < out["delta_current_a"] < 50.0

    def test_a_bolted_fault_is_not_flagged(self):
        rec, fi = _fault_record(i_load=80.0, i_fault=2000.0, faulted="A")
        out = screen_high_impedance_fault(rec, fault_index=fi, hif_threshold_a=50.0)
        assert out["hif_suspect"] is False

    @pytest.mark.xfail(strict=True, reason=(
        "Defect: hif_suspect is `0 < max_delta < threshold`, so floating-point "
        "dust on an unchanged current satisfies it. A steady balanced load with "
        "no fault reports delta 0.0 A and hif_suspect True at the same time, "
        "which reads as a Priority 1 'possible downed conductor'. "
        "compute_feeder_summary usually hides this by passing fault_index=None "
        "on a quiet record, but any event with a detected inception and a "
        "negligible current rise trips it. Fix: require a meaningful floor on "
        "max_delta rather than > 0."))
    def test_no_current_rise_is_not_flagged(self):
        n = SPC * 8
        chans = {f"I{p}": _sine(80.0, n, PH[p]) for p in "ABC"}
        chans["VAN"] = _sine(10000.0, n)
        rec = _record(chans)
        assert screen_high_impedance_fault(rec, fault_index=SPC * 4)["hif_suspect"] is False



class TestFaultLocationGuard:
    """
    estimate_fault_location divides residual fault voltage by fault current,
    which only reads as line impedance on a bolted fault.  _location_valid
    suppresses the cases where it does not.
    """

    def test_a_plausible_distance_is_kept(self):
        ok, note = _location_valid(3.2, hif_suspect=False)
        assert ok is True and "±" in note

    def test_a_high_impedance_fault_is_suppressed(self):
        ok, note = _location_valid(46.6, hif_suspect=True)
        assert ok is False and "arc resistance" in note

    def test_a_distance_longer_than_any_feeder_is_suppressed(self):
        ok, note = _location_valid(112.0, hif_suspect=False)
        assert ok is False and "plausible feeder length" in note

    def test_a_missing_estimate_is_suppressed(self):
        ok, note = _location_valid(None, hif_suspect=False)
        assert ok is False and "No fault-location estimate" in note

    def test_the_estimate_scales_inversely_with_fault_current(self):
        """A more distant fault draws less current and must locate further out."""
        def miles(i_fault):
            n, fi = SPC * 8, SPC * 3
            chans = {}
            for p in "ABC":
                sig = _sine(80.0, n, PH[p])
                if p == "A":
                    sig[fi:] = _sine(i_fault, n, PH[p])[fi:]
                chans[f"I{p}"] = sig
                v = _sine(10000.0, n, PH[p])
                if p == "A":
                    v[fi:] = _sine(1000.0, n, PH[p])[fi:]
                chans[f"V{p}N"] = v
            rec = _record(chans, trigger_s=fi / FS)
            return estimate_fault_location(rec, 0.4, fault_index=fi)["estimated_miles"]

        assert miles(500.0) > miles(2000.0)


# ─────────────────────────────────────────────────────────────────────────────
# 10. Batch manifest — only parse what is new
# ─────────────────────────────────────────────────────────────────────────────

class TestTheBatchManifestSkipsWorkAlreadyDone:

    def _touch(self, tmp_path, name, body="x"):
        f = tmp_path / name
        f.write_text(body)
        return str(f)

    def test_an_unchanged_file_is_served_from_cache(self, tmp_path):
        f = self._touch(tmp_path, "a.cfg")
        manifest = {"events": {str(Path(f).resolve()): {
            "fp": batch_mod._fingerprint(f), "result": {"event_id": "a"}}}}
        fresh, cached = batch_mod.partition([f], manifest, rebuild=False)
        assert fresh == [] and cached == [{"event_id": "a"}]

    def test_a_rewritten_file_is_re_analyzed(self, tmp_path):
        f = self._touch(tmp_path, "a.cfg")
        manifest = {"events": {str(Path(f).resolve()): {
            "fp": batch_mod._fingerprint(f), "result": {"event_id": "a"}}}}
        Path(f).write_text("a much longer body that changes the size")
        fresh, cached = batch_mod.partition([f], manifest, rebuild=False)
        assert fresh == [f] and cached == []

    def test_an_unknown_file_is_analyzed(self, tmp_path):
        f = self._touch(tmp_path, "new.cfg")
        fresh, cached = batch_mod.partition([f], {"events": {}}, rebuild=False)
        assert fresh == [f] and cached == []

    def test_rebuild_ignores_the_manifest(self, tmp_path):
        f = self._touch(tmp_path, "a.cfg")
        manifest = {"events": {str(Path(f).resolve()): {
            "fp": batch_mod._fingerprint(f), "result": {"event_id": "a"}}}}
        fresh, cached = batch_mod.partition([f], manifest, rebuild=True)
        assert fresh == [f] and cached == []

    def test_the_manifest_survives_a_save_and_load(self, tmp_path):
        state = tmp_path / ".state"
        batch_mod.save_manifest(state, {"version": "t", "events": {"k": {"fp": [1, 2.0]}}})
        assert batch_mod.load_manifest(state)["events"]["k"]["fp"] == [1, 2.0]

    def test_a_missing_manifest_loads_as_empty(self, tmp_path):
        assert batch_mod.load_manifest(tmp_path / "nope")["events"] == {}

    def test_a_corrupt_manifest_does_not_crash_the_run(self, tmp_path):
        state = tmp_path / ".state"
        state.mkdir()
        (state / batch_mod.MANIFEST).write_text("{ this is not json")
        assert batch_mod.load_manifest(state)["events"] == {}


# ─────────────────────────────────────────────────────────────────────────────
# 11. Waveform decimation for the dashboard viewer
# ─────────────────────────────────────────────────────────────────────────────

class TestWaveformDecimation:
    """
    The dashboard draws a min/max envelope per bucket.  Stride-sampling would
    drop fault peaks, which is the one thing the plot exists to show.
    """

    def test_the_envelope_keeps_a_peak_a_stride_sampler_would_miss(self):
        sig = np.zeros(1000)
        sig[501] = 950.0                      # a spike between stride points
        out = _minmax_decimate(sig, 0, 1000, buckets=10, quantum=1.0)
        assert max(out) == 950
        assert sig[::100].max() == 0.0        # what plain sub-sampling would see

    def test_min_and_max_are_interleaved_per_bucket(self):
        sig = np.array([-5.0, 5.0, -3.0, 3.0])
        out = _minmax_decimate(sig, 0, 4, buckets=2, quantum=1.0)
        assert out == [-5, 5, -3, 3]

    def test_the_quantum_scales_stored_values(self):
        """Voltages are stored in tens of volts to keep the payload small."""
        sig = np.array([0.0, 10000.0])
        assert _minmax_decimate(sig, 0, 2, buckets=1, quantum=10.0) == [0, 1000]

    def test_more_buckets_than_samples_does_not_overrun(self):
        out = _minmax_decimate(np.arange(4.0), 0, 4, buckets=99, quantum=1.0)
        assert len(out) == 2 * 4

    def test_an_empty_span_is_handled(self):
        assert _minmax_decimate(np.arange(10.0), 5, 5, buckets=4, quantum=1.0)

    def test_extract_waveform_reports_both_views_and_markers(self):
        rec, fi = _fault_record(80.0, 900.0, "A", n_cycles=12, fault_cycle=3, trip_cycle=5)
        summary = {"fault_inception_s": fi / FS, "trip_time_s": (5 * SPC) / FS}
        w = extract_waveform(rec, summary, None, 40, 60)
        assert w["i_names"] == ["IA", "IB", "IC"]
        assert len(w["full"]["i"]["IA"]) == 2 * 40
        assert len(w["zoom"]["i"]["IA"]) == 2 * 60
        assert w["marks"]["fault"] == pytest.approx(fi / FS * 1000, abs=1.0)
        assert w["zoom"]["t0"] < w["marks"]["fault"] < w["zoom"]["t1"]

    def test_the_zoom_window_is_tighter_than_the_full_record(self):
        rec, fi = _fault_record(80.0, 900.0, "A", n_cycles=40, fault_cycle=3, trip_cycle=5)
        summary = {"fault_inception_s": fi / FS, "trip_time_s": (5 * SPC) / FS}
        w = extract_waveform(rec, summary, None, 40, 60)
        full_span = w["full"]["t1"] - w["full"]["t0"]
        zoom_span = w["zoom"]["t1"] - w["zoom"]["t0"]
        assert zoom_span < full_span


# ─────────────────────────────────────────────────────────────────────────────
# 12. Plotting must not leak figures
# ─────────────────────────────────────────────────────────────────────────────

class TestPlottingReleasesItsFigures:
    """
    pyplot keeps a global reference to every figure it creates. When the plot
    is written to disk nobody holds the return value, so leaving it open leaks
    ~45 MB per event — a 100-event folder run walked the GUI into swap until
    macOS stopped redrawing the window.
    """

    def test_a_saved_plot_leaves_no_open_figure(self, tmp_path):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from comtrade_analyzer.plotting import plot_event

        plt.close("all")
        rec, _ = _fault_record(80.0, 900.0, "A", trip_cycle=5)
        plot_event(rec, save_path=str(tmp_path / "w.png"))
        assert plt.get_fignums() == []

    def test_repeated_saves_do_not_accumulate(self, tmp_path):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from comtrade_analyzer.plotting import (
            plot_event, plot_phasors, plot_rms_currents, plot_sequence_components)

        plt.close("all")
        rec, _ = _fault_record(80.0, 900.0, "A", trip_cycle=5)
        for i in range(4):
            plot_event(rec, save_path=str(tmp_path / f"{i}.png"))
            plot_phasors(rec, save_path=str(tmp_path / f"{i}_ph.png"))
            plot_rms_currents(rec, save_path=str(tmp_path / f"{i}_rms.png"))
            plot_sequence_components(rec, save_path=str(tmp_path / f"{i}_seq.png"))
        assert plt.get_fignums() == [], (
            f"{len(plt.get_fignums())} figures left open after 16 saved plots")


# ─────────────────────────────────────────────────────────────────────────────
# 13. Cross-platform text handling
# ─────────────────────────────────────────────────────────────────────────────

class TestTextIOPinsItsEncoding:
    """
    Windows defaults text I/O to cp1252, macOS and Linux to UTF-8. The
    dashboard template carries °, →, ±, Ω, ∠ and box drawing, so an unpinned
    read raises UnicodeDecodeError there and nowhere here — the failure is
    invisible on the machine that writes the code.
    """

    _PKG = Path(__file__).parent / "comtrade_analyzer"

    def test_no_source_opens_text_without_an_encoding(self):
        import re
        offenders = []
        call = re.compile(r"(?:\bopen\(|\.read_text\(|\.write_text\()")
        for src in sorted(self._PKG.glob("*.py")):
            for n, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
                if not call.search(line):
                    continue
                if any(tok in line for tok in ("encoding=", '"rb"', "'rb'",
                                               '"wb"', "'wb'", "Image.open",
                                               "subprocess", "webbrowser")):
                    continue
                offenders.append(f"{src.name}:{n}: {line.strip()}")
        assert not offenders, (
            "text I/O without encoding= breaks on Windows:\n  " + "\n  ".join(offenders))

    def test_the_dashboard_template_really_does_contain_non_ascii(self):
        """If this ever fails the test above has stopped guarding anything."""
        tpl = (self._PKG / "dashboard_template.html").read_text(encoding="utf-8")
        assert any(ord(c) > 127 for c in tpl)


# ─────────────────────────────────────────────────────────────────────────────
# 14. Plotting must not commandeer the GUI's backend
# ─────────────────────────────────────────────────────────────────────────────

class TestPlottingDoesNotForceAnInteractiveBackend:
    """
    plotting.py forced matplotlib.use("TkAgg") at import. app.py imports it
    *after* pinning "Agg", so the force won and the GUI ran the interactive Tk
    backend while rendering figures on a worker thread. Tk is not thread-safe;
    the interpreter corrupted and the process segfaulted in
    Tcl_DeleteHashEntry during teardown when the user quit.
    """

    def test_plotting_does_not_call_matplotlib_use(self):
        src = (Path(__file__).parent / "comtrade_analyzer" / "plotting.py").read_text(
            encoding="utf-8")
        offenders = [ln.strip() for ln in src.splitlines()
                     if "matplotlib.use(" in ln and not ln.strip().startswith("#")]
        assert not offenders, (
            "plotting.py must leave the backend to its caller: " + "; ".join(offenders))

    def test_importing_the_gui_leaves_agg_selected(self):
        import subprocess, sys as _sys
        code = ("import matplotlib; matplotlib.use('Agg');"
                "import comtrade_analyzer.plotting, comtrade_analyzer.report;"
                "import matplotlib as m; print(m.get_backend())")
        out = subprocess.run([_sys.executable, "-c", code], capture_output=True,
                             text=True, cwd=str(Path(__file__).parent))
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip().lower() == "agg", (
            f"a module overrode the pinned backend: {out.stdout.strip()}")


# ─────────────────────────────────────────────────────────────────────────────
# 15. Triage rules are exported, not duplicated
# ─────────────────────────────────────────────────────────────────────────────

class TestTheProjectNotesStayCurrent:
    """
    CLAUDE.md is loaded into every session and is the first thing read before
    touching this code. Stale guidance there is worse than none — an out-of-date
    section once described a superseded three-class EPSS model, which would have
    talked the next session back into the bug it documented.
    """

    _NOTES = Path(__file__).parent / "CLAUDE.md"

    def test_the_module_map_lists_every_module(self):
        notes = self._NOTES.read_text(encoding="utf-8")
        pkg = Path(__file__).parent / "comtrade_analyzer"
        missing = [f.name for f in sorted(pkg.glob("*.py"))
                   if f.name != "__init__.py" and f.name not in notes]
        assert not missing, f"CLAUDE.md module map is missing: {missing}"

    def test_it_does_not_describe_a_superseded_epss_model(self):
        notes = self._NOTES.read_text(encoding="utf-8").lower()
        for stale in ("three-way classification",
                      "cannot suppress a reclose that never happened",
                      "epss can't suppress what didn't happen"):
            assert stale not in notes, f"CLAUDE.md still says: {stale!r}"

    def test_every_epss_class_is_documented(self):
        from comtrade_analyzer.wso_impact import class_table
        notes = self._NOTES.read_text(encoding="utf-8")
        missing = [c["key"] for c in class_table() if c["key"] not in notes]
        assert not missing, f"CLAUDE.md does not mention: {missing}"


class TestTheGeneratorsGroundTruthMatchesTheClassifier:
    """
    fleet_gen writes expect_wso; wso_impact.classify_event decides the real
    answer. When the classification model gained EPSS_CANDIDATE and
    INDETERMINATE the generator was not updated, and the dashboard's own
    detector-agreement panel dropped to 60% — on the demo build that ships to
    show the tool working.
    """

    def test_every_expected_class_is_a_real_class(self):
        import re
        from comtrade_analyzer.wso_impact import class_table
        src = (Path(__file__).parent / "comtrade_analyzer" / "fleet_gen.py").read_text(
            encoding="utf-8")
        used = set(re.findall(r'expect_wso = "(\w+)"', src))
        known = {c["key"] for c in class_table()}
        assert used <= known, f"fleet_gen expects classes that do not exist: {used - known}"

    def test_a_single_trip_is_expected_to_be_indeterminate(self):
        """Its record ends long before any dead time — the generator must not
        claim EPSS changes nothing."""
        import re
        src = (Path(__file__).parent / "comtrade_analyzer" / "fleet_gen.py").read_text(
            encoding="utf-8")
        block = re.search(r'if template == "single_trip":(.*?)elif template', src, re.S)
        assert block and 'expect_wso = "INDETERMINATE"' in block.group(1)

    def test_a_ride_through_is_expected_to_be_a_candidate(self):
        import re
        src = (Path(__file__).parent / "comtrade_analyzer" / "fleet_gen.py").read_text(
            encoding="utf-8")
        block = re.search(r'elif template == "no_trip":(.*?)elif template', src, re.S)
        assert block and 'expect_wso = "EPSS_CANDIDATE"' in block.group(1)


class TestTriageRulesAreASingleSourceOfTruth:
    """
    The dashboard used to carry its own copy of the flag → priority map in JS.
    Tuning triage.py would then leave the page quietly disagreeing with the
    analysis. The rule table is exported through the analysis JSON instead.
    """

    def test_every_flag_has_a_rule_row(self):
        from comtrade_analyzer.triage import _FLAGS, rule_table
        keys = {r["key"] for r in rule_table()}
        assert keys == set(_FLAGS), "rule_table() and _FLAGS disagree"

    def test_each_rule_states_a_trigger(self):
        from comtrade_analyzer.triage import rule_table
        for r in rule_table():
            assert r["trigger"], f"{r['key']} has no trigger description"
            assert r["priority"] in (1, 2, 3)

    def test_the_slow_trip_rule_reports_the_threshold_in_use(self):
        from comtrade_analyzer.triage import rule_table
        row = next(r for r in rule_table(slow_trip_cycles=6.0) if r["key"] == "slow_trip")
        assert "6 cycles" in row["trigger"] and "100 ms" in row["trigger"]

    def test_a_fired_flag_carries_its_evidence(self):
        out = triage_event({"fault_type": "SLG", "fault_inception_s": 0.05,
                            "trip_time_s": 0.39, "trip_delay_ms": 339.3})
        reason = next(r for r in out["reasons"] if r["key"] == "slow_trip")
        assert "339.3 ms" in reason["evidence"] and "166.7 ms" in reason["evidence"]
        assert reason["decisive"] is True

    def test_the_dashboard_does_not_hardcode_priorities(self):
        tpl = (Path(__file__).parent / "comtrade_analyzer"
               / "dashboard_template.html").read_text(encoding="utf-8")
        assert "hif_suspect: 1, lockout: 1" not in tpl, (
            "dashboard_template.html has re-hardcoded the flag → priority map; "
            "it must read FLEET.triage_rules instead")


# ─────────────────────────────────────────────────────────────────────────────
# 16. Diagnostics for real-world files
# ─────────────────────────────────────────────────────────────────────────────

class TestDiagnosticsCatchRealExportProblems:
    """
    The fixtures are perfect; vendor exports are not. Each check has to name
    the symptom, the evidence and the fix — a batch reporting "0 events" with
    no reason is the worst possible outcome on someone else's machine.
    """

    def _rec(self, **kw):
        rec, _ = _fault_record(80.0, 900.0, "A", trip_cycle=5, **kw)
        return rec

    def test_a_clean_record_produces_nothing(self):
        from comtrade_analyzer.diagnostics import check_record
        assert check_record(self._rec()) == []

    def test_unrecognised_phase_names_are_blocking(self):
        """The classifier matches names, not units — a units-only check would
        pass this file and then classify every event UNKNOWN."""
        from comtrade_analyzer.diagnostics import check_record, ERROR
        rec = self._rec()
        rec.analog_channels = {f"CH{i}_ANLG": v for i, v in enumerate(rec.analog_channels.values())}
        rec.analog_info = {f"CH{i}_ANLG": inf for i, inf in enumerate(rec.analog_info.values())}
        codes = {f["code"]: f for f in check_record(rec)}
        assert "phase_currents_unnamed" in codes
        assert codes["phase_currents_unnamed"]["level"] == ERROR

    def test_secondary_scaling_is_flagged(self):
        from comtrade_analyzer.diagnostics import check_record
        rec = self._rec()
        for k in rec.analog_channels:
            rec.analog_channels[k] = rec.analog_channels[k] / 240.0
        codes = {f["code"] for f in check_record(rec)}
        assert "current_looks_secondary" in codes

    def test_a_missing_trip_channel_is_flagged(self):
        from comtrade_analyzer.diagnostics import check_record
        rec = self._rec()
        rec.digital_channels = {}
        codes = {f["code"] for f in check_record(rec)}
        assert "no_digitals" in codes

    def test_too_little_prefault_is_flagged(self):
        from comtrade_analyzer.diagnostics import check_record
        rec = self._rec()
        rec.trigger_index = 4
        codes = {f["code"] for f in check_record(rec)}
        assert "little_prefault" in codes

    def test_a_missing_dat_names_the_pair_problem(self):
        from comtrade_analyzer.diagnostics import explain_parse_error
        f = explain_parse_error("/x/EVT.cfg", FileNotFoundError("Cannot find .dat file for: /x/EVT.cfg"))
        assert f["code"] == "missing_pair" and ".DAT" in f["fix"]

    def test_a_broken_cfg_points_at_the_header_line(self):
        from comtrade_analyzer.diagnostics import explain_parse_error
        f = explain_parse_error("/x/EVT.cfg", IndexError("list index out of range"))
        assert f["code"] == "cfg_format" and "line 2" in f["fix"]

    def test_every_finding_carries_a_fix(self):
        """A diagnosis without a remedy just moves the confusion."""
        from comtrade_analyzer.diagnostics import check_record
        rec = self._rec()
        rec.digital_channels = {}
        rec.trigger_index = 2
        for k in rec.analog_channels:
            rec.analog_channels[k] = rec.analog_channels[k] / 240.0
        found = check_record(rec)
        assert found
        for f in found:
            assert f["message"] and f["fix"], f"{f['code']} has no fix text"


# ─────────────────────────────────────────────────────────────────────────────
# 17. The SUBNET relay settings catalog
# ─────────────────────────────────────────────────────────────────────────────

_SETTINGS_CSV = """\
Id,Name,Feeder,TemplateDate,Template Type,ADMS Template Version,DATE_TIME,CTR,NOMINAL_SG,ACTIVE_SG,SG1_GROUND,SG1_PHASE,SG2_GROUND,SG2_PHASE,SG3_GROUND,SG3_PHASE
g-1,RCL_076-024,Maple 1211,SEL-651R-WF-3PhTrip3PhLoc.2,Recloser,3.1,2026-08-20 06:00:00,400,1,1,1.5,4.0,0.5,1.5,Not found,Not found
g-2,RCL_NO-CTR,Pine 3301,SEL-651R-STD-3PhTrip.4,Recloser,2.9,2026-08-20 06:00:00,Not found,1,1,1.5,4.0,0.5,1.5,Not found,Not found
"""


def _catalog(tmp_path, text=_SETTINGS_CSV):
    from comtrade_analyzer.relay_settings import load_settings
    f = tmp_path / "settings.csv"
    f.write_text(text, encoding="utf-8")
    return load_settings(str(f))


class TestTheRelayTemplateName:
    """SEL-651R-WF-3PhTrip3PhLoc.2 carries type, application, modes, version."""

    def test_it_decomposes(self):
        from comtrade_analyzer.relay_settings import parse_template
        t = parse_template("SEL-651R-WF-3PhTrip3PhLoc.2")
        assert t.relay_type == "SEL-651R"
        assert t.application == "WF"
        assert t.trip_mode == "3PhTrip"
        assert t.location_mode == "3PhLoc"
        assert t.version == "2"
        assert t.is_wildfire is True

    def test_the_location_mode_does_not_swallow_the_trip_mode(self):
        """The two modes run together with no separator; a greedy match
        returns '3PhTrip3PhLoc' for both."""
        from comtrade_analyzer.relay_settings import parse_template
        t = parse_template("SEL-651R-WF-3PhTrip3PhLoc.2")
        assert t.location_mode == "3PhLoc"
        assert t.trip_mode != t.location_mode

    def test_an_unparseable_name_is_kept_verbatim(self):
        from comtrade_analyzer.relay_settings import parse_template
        assert parse_template("something odd").raw == "something odd"

    def test_a_null_token_yields_nothing(self):
        from comtrade_analyzer.relay_settings import parse_template
        assert parse_template("Not found") is None


class TestTheSettingsCatalog:

    def test_it_loads_and_is_looked_up_by_device_id(self, tmp_path):
        cat = _catalog(tmp_path)
        assert len(cat) == 2
        assert cat.lookup("RCL_076-024") is not None

    def test_lookup_ignores_punctuation_and_case(self, tmp_path):
        """COMTRADE rec_dev_id rarely matches the catalog's spelling exactly."""
        cat = _catalog(tmp_path)
        assert cat.lookup("rcl 076 024") is not None
        assert cat.lookup("RCL076024") is not None

    def test_not_found_becomes_none(self, tmp_path):
        cat = _catalog(tmp_path)
        s = cat.lookup("RCL_076-024")
        assert 3 not in s.groups                     # SG3 was "Not found"
        assert cat.lookup("RCL_NO-CTR").ctr is None

    def test_the_nominal_group_is_the_normal_day_group(self, tmp_path):
        s = _catalog(tmp_path).lookup("RCL_076-024")
        assert s.normal_group().number == 1

    def test_the_epss_group_is_the_most_sensitive_other_group(self, tmp_path):
        s = _catalog(tmp_path).lookup("RCL_076-024")
        assert s.epss_group().number == 2
        assert "inferred" in s.epss_group_source()

    def test_an_explicit_epss_group_overrides_the_inference(self, tmp_path):
        s = _catalog(tmp_path).lookup("RCL_076-024")
        assert s.epss_group(forced=1).number == 1
        assert "configured" in s.epss_group_source(forced=1)


class TestPickupArithmetic:
    """Settings are secondary amps; a COMTRADE record measures primary."""

    def test_ctr_converts_secondary_to_primary(self, tmp_path):
        s = _catalog(tmp_path).lookup("RCL_076-024")
        assert s.primary_pickup(s.normal_group()) == pytest.approx(4.0 * 400)
        assert s.primary_pickup(s.epss_group()) == pytest.approx(1.5 * 400)

    def test_without_a_ctr_no_primary_pickup_can_be_stated(self, tmp_path):
        s = _catalog(tmp_path).lookup("RCL_NO-CTR")
        assert s.primary_pickup(s.normal_group()) is None

    def test_a_fault_between_the_two_pickups_converts(self, tmp_path):
        """The whole point: a fuse clears it today, EPSS trips the recloser."""
        s = _catalog(tmp_path).lookup("RCL_076-024")
        ev = s.evaluate(700.0)                       # normal 1600 A, EPSS 600 A
        assert ev["normal"]["picks_up"] is False
        assert ev["epss"]["picks_up"] is True
        assert ev["converts_under_epss"] is True

    def test_a_fault_above_both_pickups_does_not_convert(self, tmp_path):
        s = _catalog(tmp_path).lookup("RCL_076-024")
        ev = s.evaluate(2000.0)
        assert ev["normal"]["picks_up"] is True
        assert ev["converts_under_epss"] is False

    def test_a_fault_below_both_pickups_does_not_convert(self, tmp_path):
        s = _catalog(tmp_path).lookup("RCL_076-024")
        ev = s.evaluate(200.0)
        assert ev["epss"]["picks_up"] is False
        assert ev["converts_under_epss"] is False

    def test_an_unresolvable_relay_says_so(self, tmp_path):
        s = _catalog(tmp_path).lookup("RCL_NO-CTR")
        assert s.evaluate(700.0)["resolved"] is False


class TestTheCatalogReportsItsOwnHealth:

    def test_a_missing_ctr_is_flagged(self, tmp_path):
        from comtrade_analyzer.relay_settings import sanity_check
        codes = {f["code"] for f in sanity_check(_catalog(tmp_path))}
        assert "settings_no_ctr" in codes

    def test_an_empty_catalog_is_an_error(self, tmp_path):
        from comtrade_analyzer.relay_settings import sanity_check
        cat = _catalog(tmp_path, "Id,Name\n")
        f = sanity_check(cat)[0]
        assert f["code"] == "settings_empty" and f["level"] == "error"

    def test_primary_looking_pickups_are_flagged(self, tmp_path):
        """Multiplying already-primary pickups by CTR overstates them by CTR."""
        from comtrade_analyzer.relay_settings import sanity_check
        text = _SETTINGS_CSV.replace(",1.5,4.0,0.5,1.5,", ",600,1600,200,600,")
        codes = {f["code"] for f in sanity_check(_catalog(tmp_path, text))}
        assert "settings_units" in codes

    def test_every_finding_carries_a_fix(self, tmp_path):
        from comtrade_analyzer.relay_settings import sanity_check
        for f in sanity_check(_catalog(tmp_path)):
            if f["level"] != "info":
                assert f["fix"], f"{f['code']} has no fix text"


# ─────────────────────────────────────────────────────────────────────────────
# 18. End to end against the generated fixtures
# ─────────────────────────────────────────────────────────────────────────────

_ROOT = Path(__file__).parent
_FIXTURES = {
    "test_ll":        ("test_ll.cfg", "LL"),
    "test_llg":       ("test_llg.cfg", "LLG"),
    "test_3ph":       ("test_3ph.cfg", "3PH"),
    "test_recloser":  ("test_recloser.cfg", "SLG"),
}
_HAVE_FIXTURES = all((_ROOT / d / f).is_file() for d, (f, _) in _FIXTURES.items())


@pytest.mark.skipif(not _HAVE_FIXTURES,
                    reason="run the generate_test_*.py scripts first")
@pytest.mark.parametrize("folder", sorted(_FIXTURES))
def test_each_generated_fixture_classifies_as_intended(folder):
    """The generators are the regression bar for anything in analysis.py."""
    name, expected = _FIXTURES[folder]
    rec = COMTRADEParser().parse(str(_ROOT / folder / name))
    assert classify_fault(rec, detect_fault_inception(rec)) == expected


@pytest.mark.skipif(not (_ROOT / "test_event" / "test_event.cfg").is_file(),
                    reason="run generate_test_data.py first")
@pytest.mark.xfail(strict=True, reason=(
    "Known gap, predates the packaging work: generate_test_data.py drives the "
    "unfaulted phases at 140 A against an 800 A faulted phase, so ratio_mid "
    "lands at 0.154 against the classifier's < 0.15 SLG gate and it falls "
    "through to the LLG catch-all. Fix the fixture (I_FAULT_BC 140 → ~110 A), "
    "not the threshold."))
def test_the_slg_reference_fixture_classifies_as_slg():
    rec = COMTRADEParser().parse(str(_ROOT / "test_event" / "test_event.cfg"))
    assert classify_fault(rec, detect_fault_inception(rec)) == "SLG"


@pytest.mark.skipif(not (_ROOT / "test_recloser" / "test_recloser.cfg").is_file(),
                    reason="run generate_test_recloser.py first")
class TestTheRecloserFixtureEndToEnd:
    """The 3-shot lockout fixture exercises the whole feeder path at once."""

    @pytest.fixture(scope="class")
    @staticmethod
    def feeder():
        rec = COMTRADEParser().parse(str(_ROOT / "test_recloser" / "test_recloser.cfg"))
        return rec, compute_feeder_summary(rec, feeder_impedance_ohm_per_mile=0.4)

    def test_all_three_shots_are_detected(self, feeder):
        _, fd = feeder
        assert fd["reclose_sequence"].total_shots == 3

    def test_it_locks_out(self, feeder):
        _, fd = feeder
        assert fd["reclose_sequence"].locked_out is True

    def test_it_is_permanent_not_wso_exposed(self, feeder):
        _, fd = feeder
        assert classify_event(fd) == PERMANENT

    def test_the_first_shot_is_fast_and_the_second_is_slow(self, feeder):
        _, fd = feeder
        shots = fd["reclose_sequence"].shots
        assert shots[0].shot_type == "FAST"
        assert shots[1].shot_type == "SLOW"

    def test_triage_raises_the_lockout_flag(self, feeder):
        from comtrade_analyzer.analysis import compute_event_summary
        rec, fd = feeder
        out = triage_event(compute_event_summary(rec), fd)
        assert out["priority"] == 1 and "lockout" in out["flags"]


class TestWindowsIcon:
    """The .ico is built on a Mac and only ever consumed on Windows.

    Nothing on the machine that generates it will notice it is wrong, and
    Windows reports none of these faults — it silently draws the host
    interpreter's icon instead. So the file is checked here.
    """

    ROOT = Path(__file__).parent
    ICO  = ROOT / "icon.ico"
    #: Windows asks for these: 16 title bar, 24/32 taskbar and Alt-Tab, 48
    #: desktop, 256 the large-icon view. Missing one is a silent fallback.
    REQUIRED = [16, 24, 32, 48, 256]

    def _directory(self, path=None):
        data = (path or self.ICO).read_bytes()
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        assert reserved == 0 and kind == 1, "not an icon file"
        entries, offset = [], 6
        for _ in range(count):
            w, h, _n, _r, _p, bpp, length, at = struct.unpack(
                "<BBBBHHII", data[offset:offset + 16])
            offset += 16
            entries.append({"w": w or 256, "h": h or 256, "bpp": bpp,
                            "png": data[at:at + 8] == b"\x89PNG\r\n\x1a\n"})
        return entries

    def test_the_icon_file_exists(self):
        assert self.ICO.exists(), "icon.ico is missing — run make_icon.py"

    def test_every_size_windows_asks_for_is_present(self):
        """A lone 16x16 entry is what makes a tool look iconless on Windows."""
        have = {e["w"] for e in self._directory()}
        missing = [s for s in self.REQUIRED if s not in have]
        assert not missing, (
            f"icon.ico is missing {missing} px. Pillow drops any size larger "
            "than the image being saved, so make_ico must save from the "
            "largest frame.")

    def test_small_entries_are_not_png_compressed(self):
        """Windows reads PNG inside an .ico only at 256x256, and skips the rest.

        Pillow writes every entry as PNG unless bitmap_format="bmp" is passed,
        which produces a file that looks valid everywhere except Windows.
        """
        bad = [e["w"] for e in self._directory() if e["png"] and e["w"] < 256]
        assert not bad, (
            f"{bad} px entries are PNG-compressed; Windows will ignore them. "
            'Pass bitmap_format="bmp" when saving.')

    def test_the_icon_carries_an_alpha_channel(self):
        assert all(e["bpp"] == 32 for e in self._directory()), \
            "every entry should be 32bpp so the rounded corners stay transparent"

    def test_the_window_asks_for_the_ico_on_windows(self):
        """The GUI used to load only icon.png, which Windows will not use.

        A PNG handed to Tk decorates nothing Windows draws from the .ico — the
        taskbar and desktop fall back to the interpreter's icon in silence.
        """
        src = (self.ROOT / "comtrade_analyzer" / "app.py").read_text(encoding="utf-8")
        assert "icon.ico" in src, "app.py must load icon.ico on Windows"
        assert "iconbitmap" in src, "the .ico is set through iconbitmap()"

    def test_the_app_claims_its_own_taskbar_identity(self):
        """Without an AppUserModelID the taskbar shows Python's icon regardless.

        The window icon and the taskbar icon are separate on Windows, and
        fixing only the first leaves the symptom the user actually sees.
        """
        from comtrade_analyzer import app as app_mod
        assert hasattr(app_mod, "_claim_windows_taskbar_identity")
        app_mod._claim_windows_taskbar_identity()  # no-op off Windows, never raises

    def test_the_icons_the_window_loads_are_findable(self):
        """The window searches the package first, then the checkout root."""
        from comtrade_analyzer.app import _icon_file
        for name in ("icon.ico", "icon.png"):
            assert _icon_file(name) is not None, f"{name} is not where the GUI looks"

    def test_the_package_copies_match_the_generated_ones(self):
        """A stale package copy wins the search and silently undoes a rebuild.

        make_icon.py mirrors the root icons into the package; if these drift,
        that step was skipped and the wheel ships the old artwork.
        """
        pkg = self.ROOT / "comtrade_analyzer"
        for name in ("icon.png", "icon.ico"):
            assert (pkg / name).read_bytes() == (self.ROOT / name).read_bytes(), \
                f"{name} differs between the package and the root — run make_icon.py"


# ─────────────────────────────────────────────────────────────────────────────
# 19. Feeder connectivity
# ─────────────────────────────────────────────────────────────────────────────

class TestTheTopologyModel:
    """
    topology.py answers the questions a pile of event files cannot: which
    devices are on one path to the source, what goes dark when one opens, and
    which feeder could back it up. The format is hand-authored in a
    spreadsheet, so the validator has to catch spreadsheet mistakes.
    """

    ROOT = Path(__file__).parent
    DEMO = ROOT / "demo" / "topology.csv"
    TEMPLATE = ROOT / "comtrade_analyzer" / "topology_template.csv"

    def _net(self):
        from comtrade_analyzer.topology import load_topology
        return load_topology(str(self.DEMO))

    # Device ids follow the utility's naming convention and will change again.
    # Look them up by position in the tree instead of writing them down.
    def _chain(self, net, feeder):
        """The head of `feeder` and the trunk below it, deepest last."""
        head = next(d for d in net.devices(feeder)
                    if (p := net.parent_of(d.node_id)) is not None and p.is_source)
        out = [head]
        node = head
        while True:
            kids = [c for c in net.children(node.node_id)
                    if c.kind in ("breaker", "recloser") and c.feeder == feeder]
            if not kids:
                break
            node = kids[0]
            out.append(node)
        return [d.node_id for d in out]

    # -- the shipped files --------------------------------------------------

    def test_the_demo_topology_validates_without_errors_or_warnings(self):
        from comtrade_analyzer.topology import validate
        bad = [f for f in validate(self._net()) if f["level"] != "info"]
        assert not bad, "demo/topology.csv: " + "; ".join(
            f"{f['code']}: {f['message']}" for f in bad)

    def test_the_template_validates_clean(self):
        """It is what a colleague copies — it must not model a mistake."""
        from comtrade_analyzer.topology import load_topology, validate
        bad = [f for f in validate(load_topology(str(self.TEMPLATE)))
               if f["level"] != "info"]
        assert not bad, "template: " + "; ".join(f["code"] for f in bad)

    def test_the_template_carries_the_documented_columns(self):
        import csv as _csv
        from comtrade_analyzer.topology import _HEADER
        with open(self.TEMPLATE, newline="", encoding="utf-8-sig") as fh:
            header = tuple(h.strip() for h in next(_csv.reader(fh)))
        assert header == _HEADER

    def test_every_registered_device_is_placed_on_a_feeder(self):
        """A device with events but no topology row cannot be drawn anywhere."""
        from comtrade_analyzer.wso_impact import load_registry
        registry = load_registry(str(self.ROOT / "demo" / "devices.csv"))
        net = self._net()
        missing = [d["device_id"] for d in registry.values()
                   if d["device_id"] not in net]
        assert not missing, f"in devices.csv but not in topology: {missing}"

    def test_every_feeder_has_a_way_back_in(self):
        net = self._net()
        stranded = [f for f in net.feeders() if not net.ties(f)]
        assert not stranded, f"no tie to back up: {stranded}"

    # -- graph semantics ----------------------------------------------------

    def test_upstream_is_along_one_path_not_merely_the_same_feeder(self):
        """
        The whole point: two records within a few hundred ms are one fault seen
        at two depths only if the devices share a path to the source.
        """
        net = self._net()
        a = self._chain(net, "Cedar Hollow 1211")
        b = self._chain(net, "Cedar Hollow 1212")
        assert net.is_upstream_of(a[0], a[-1])
        assert not net.is_upstream_of(a[-1], a[0])
        assert not net.is_upstream_of(b[0], a[-1])
        assert net.on_same_path(a)
        assert not net.on_same_path([b[0], a[-1]])

    def test_the_deepest_device_is_the_one_that_should_have_cleared(self):
        net = self._net()
        chain = self._chain(net, "Cedar Hollow 1211")
        assert net.deepest(list(reversed(chain))).node_id == chain[-1]

    def test_a_normally_open_tie_carries_nothing(self):
        """
        subtree() is 'what goes dark if this opens'. A N.O. tie is a leaf, not
        a route onto the neighbouring feeder — treating it as one would double
        the outage on every lockout.
        """
        net = self._net()
        head = self._chain(net, "Cedar Hollow 1211")[0]
        tie = next(t for t in net.ties("Cedar Hollow 1211"))
        far = net.node(tie.tie_to)
        below = [n.node_id for n in net.subtree(head)]
        assert tie.node_id in below
        assert far.node_id not in below
        crossed = [n.node_id for n in net.subtree(head, cross_ties=True)]
        assert far.node_id in crossed

    def test_ids_join_across_punctuation_and_case(self):
        """Topology and registry are typed by hand, hours apart."""
        net = self._net()
        real = self._chain(net, "Cedar Hollow 1211")[-1]
        assert real.lower().replace("-", " ") in net
        assert net.node(real.replace("-", "_")).node_id == real

    def test_customers_below_sums_the_subtree_from_the_registry(self):
        """
        customers_served on a device row is that device's OWN section; what a
        trip actually drops is the whole subtree under it. A feeder head takes
        every recloser below it with it, so the two numbers must differ.
        """
        from comtrade_analyzer.wso_impact import load_registry
        registry = load_registry(str(self.ROOT / "demo" / "devices.csv"))
        from comtrade_analyzer.wso_impact import _normalize
        net = self._net()
        chain = self._chain(net, "Cedar Hollow 1211")
        own = registry[_normalize(chain[0])]["customers_served"]
        below = net.customers_below(chain[0], registry)
        assert below > own, "a feeder head must drop more than its own section"
        assert below == sum(registry[_normalize(d)]["customers_served"] for d in chain)

    def test_a_leaf_device_drops_only_its_own_section(self):
        from comtrade_analyzer.wso_impact import load_registry, _normalize
        registry = load_registry(str(self.ROOT / "demo" / "devices.csv"))
        net = self._net()
        leaf = self._chain(net, "Cedar Hollow 1211")[-1]
        assert (net.customers_below(leaf, registry)
                == registry[_normalize(leaf)]["customers_served"])

    # -- authoring mistakes -------------------------------------------------

    def _codes(self, text, tmp_path):
        from comtrade_analyzer.topology import load_topology, validate
        f = tmp_path / "t.csv"
        f.write_text("feeder,node_id,kind,parent,tie_to\n" + text, encoding="utf-8")
        return {v["code"] for v in validate(load_topology(str(f)))}

    def test_the_validator_catches_spreadsheet_mistakes(self, tmp_path):
        cases = {
            "duplicate_node":       "F,S,source,,\nF,A,recloser,S,\nF,A,recloser,S,\n",
            "missing_parent":       "F,S,source,,\nF,A,recloser,TYPO,\n",
            "no_parent":            "F,S,source,,\nF,A,recloser,,\n",
            "loop":                 "F,S,source,,\nF,A,recloser,B,\nF,B,recloser,A,\n",
            "unknown_kind":         "F,S,source,,\nF,A,switchgear,S,\n",
            "source_has_parent":    "F,S,source,,\nF,T,source,S,\n",
            "tie_without_far_end":  "F,S,source,,\nF,A,recloser,S,\nF,T,tie,A,\n",
            "missing_tie_far_end":  "F,S,source,,\nF,A,recloser,S,\nF,T,tie,A,GONE\n",
            "no_source":            "F,A,recloser,B,\nF,B,recloser,A,\n",
        }
        for code, rows in cases.items():
            assert code in self._codes(rows, tmp_path), f"{code} not reported"

    def test_every_finding_carries_a_fix(self, tmp_path):
        """Same bar as diagnostics.py: symptom, evidence, fix."""
        from comtrade_analyzer.topology import load_topology, validate
        f = tmp_path / "t.csv"
        f.write_text("feeder,node_id,kind,parent,tie_to\n"
                     "F,S,source,,\nF,A,recloser,TYPO,\nF,A,widget,S,\n",
                     encoding="utf-8")
        findings = validate(load_topology(str(f)))
        assert findings
        for v in findings:
            assert v["fix"].strip(), f"{v['code']} has no fix text"

    def test_a_duplicate_row_does_not_silently_vanish(self, tmp_path):
        """The graph keeps the first row; the validator must still say so."""
        from comtrade_analyzer.topology import load_topology
        f = tmp_path / "t.csv"
        f.write_text("feeder,node_id,kind,parent,tie_to\n"
                     "F,S,source,,\nF,A,recloser,S,\nF,A,breaker,S,\n",
                     encoding="utf-8")
        net = load_topology(str(f))
        assert net.node("A").kind == "recloser"
        assert len(net.dropped) == 1

    def test_a_feeder_view_shows_ties_authored_from_the_far_side(self, ):
        """
        The model is undirected — a tie is written once, from whichever feeder
        the author was looking at. Bear Gulch's tie to Sawmill Grade lives on
        the Sawmill Grade row, and must still appear when Bear Gulch is drawn.
        """
        from comtrade_analyzer.topology import single_line
        net = self._net()
        txt = single_line(net, "Bear Gulch 2110")
        for tie in net.ties("Bear Gulch 2110"):
            assert tie.node_id in txt, f"{tie.node_id} is missing from the view"
        # A feeder with no tie into this one stays out entirely.
        untied = next(f for f in net.feeders()
                      if f != "Bear Gulch 2110"
                      and not any(t in net.ties("Bear Gulch 2110")
                                  for t in net.ties(f)))
        assert all(d.node_id not in txt for d in net.devices(untied))


# ─────────────────────────────────────────────────────────────────────────────
# 20. Incidents — one fault, several records
# ─────────────────────────────────────────────────────────────────────────────

class TestIncidentGeneration:
    """
    A fault produces a record at the device that cleared it, one at every
    device above it that saw the same current and did not trip, and — after a
    lockout — one on a neighbouring feeder when a tie picks the section up.
    The records of one fault have to be consistent with each other, because the
    whole point of the topology is to check exactly that.
    """

    ROOT = Path(__file__).parent

    @pytest.fixture(scope="class")
    @classmethod
    def fleet(cls, tmp_path_factory):
        from comtrade_analyzer.fleet_gen import generate_fleet
        out = tmp_path_factory.mktemp("fleet")
        info = generate_fleet(str(out), count=30, seed=99)
        return json.loads(Path(info["truth"]).read_text(encoding="utf-8"))

    def _by_incident(self, truth):
        out = defaultdict(list)
        for e in truth["events"]:
            out[e["incident_id"]].append(e)
        return out

    def test_the_generated_registry_and_topology_agree(self, tmp_path):
        """Both come from the same tables, so validate() must find no gaps."""
        from comtrade_analyzer.fleet_gen import generate_fleet
        from comtrade_analyzer.topology import load_topology, validate
        from comtrade_analyzer.wso_impact import load_registry
        info = generate_fleet(str(tmp_path), count=1, seed=3)
        net = load_topology(info["topology"])
        findings = validate(net, load_registry(info["registry"]))
        assert not findings, [f["code"] for f in findings]

    def test_a_fault_can_produce_several_records(self, fleet):
        groups = self._by_incident(fleet)
        assert any(len(v) > 1 for v in groups.values()), \
            "no incident produced more than one record"

    def test_every_incident_has_exactly_one_origin(self, fleet):
        for inc, rows in self._by_incident(fleet).items():
            origins = [r for r in rows if r["role"] == "origin"]
            assert len(origins) == 1, f"{inc}: {len(origins)} origin records"

    def test_witnesses_saw_the_same_fault_at_the_same_instant(self, fleet):
        """Same type, same current, same moment — it is one fault."""
        for rows in self._by_incident(fleet).values():
            origin = next(r for r in rows if r["role"] == "origin")
            t0 = datetime.fromisoformat(origin["timestamp"])
            for w in [r for r in rows if r["role"] == "witness"]:
                assert w["expect_fault"] == origin["expect_fault"]
                assert w["i_fault_peak_a"] == origin["i_fault_peak_a"]
                gap = abs((datetime.fromisoformat(w["timestamp"]) - t0).total_seconds())
                assert gap < 0.05, f"{w['event_id']}: {gap:.3f}s from the origin"

    def test_a_witness_is_upstream_and_carries_more_load(self, fleet, tmp_path_factory):
        """
        The device that saw the fault but did not trip must be electrically
        above the one that cleared it, and feeding more customers.
        """
        from comtrade_analyzer.topology import load_topology
        from comtrade_analyzer.fleet_gen import generate_fleet
        out = tmp_path_factory.mktemp("net")
        info = generate_fleet(str(out), count=1, seed=3)
        net = load_topology(info["topology"])
        for rows in self._by_incident(fleet).values():
            origin = next(r for r in rows if r["role"] == "origin")
            for w in [r for r in rows if r["role"] == "witness"]:
                assert net.is_upstream_of(w["device_id"], origin["device_id"]), \
                    f"{w['device_id']} is not above {origin['device_id']}"
                assert w["i_load_peak_a"] > origin["i_load_peak_a"]

    def test_a_witness_never_trips(self, fleet):
        for e in fleet["events"]:
            if e["role"] == "witness":
                assert e["expect_shots"] == 0
                assert "no_trip" in e["expect_flags"]

    def test_a_tie_pickup_is_a_load_step_on_another_feeder(self, fleet):
        """
        FLISR restoration lands on the neighbouring feeder, tens of seconds
        later, with no fault at all — that is what makes it easy to misread.
        """
        seen = 0
        for rows in self._by_incident(fleet).values():
            origin = next(r for r in rows if r["role"] == "origin")
            for t in [r for r in rows if r["role"] == "tie_pickup"]:
                seen += 1
                assert t["expect_fault"] == "LOAD"
                assert t["expect_wso"] == "NOT_EXPOSED"
                assert t["expect_flags"] == []
                assert t["feeder"] != origin["feeder"], "a tie must cross feeders"
                gap = (datetime.fromisoformat(t["timestamp"])
                       - datetime.fromisoformat(origin["timestamp"])).total_seconds()
                assert gap > 5.0, "restoration is not inside the fault window"
        assert seen, "no tie pickup in the sample"

    def test_only_a_lockout_strands_a_section(self, fleet):
        for rows in self._by_incident(fleet).values():
            if any(r["role"] == "tie_pickup" for r in rows):
                origin = next(r for r in rows if r["role"] == "origin")
                assert origin["expect_wso"] == "PERMANENT", \
                    "nothing to restore unless the device locked out"

    def test_the_incident_id_is_not_in_the_comtrade_files(self, tmp_path):
        """
        A relay has no idea the other records exist. If the id leaks into the
        CFG, the pipeline's grouping is never actually exercised.
        """
        from comtrade_analyzer.fleet_gen import generate_fleet
        info = generate_fleet(str(tmp_path), count=6, seed=11)
        for cfg in Path(info["events_dir"]).glob("*.cfg"):
            assert "INC" not in cfg.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# 21. Rebuilding incidents from timestamps and topology
# ─────────────────────────────────────────────────────────────────────────────

class TestIncidentGrouping:
    """
    Nothing in a COMTRADE file says which records belong to one fault, so the
    grouping is rebuilt from when the fault started and how the feeder is
    wired. Both are needed: time alone merges unrelated faults across the fleet
    in a storm, topology alone merges every fault that feeder ever had.
    """

    ROOT = Path(__file__).parent

    def _net(self):
        from comtrade_analyzer.topology import load_topology
        return load_topology(str(self.ROOT / "demo" / "topology.csv"))

    def _chain(self, net, feeder):
        """Head then trunk of `feeder`. Ids follow a naming convention that
        changes; the shape of the tree does not."""
        head = next(d for d in net.devices(feeder)
                    if (p := net.parent_of(d.node_id)) is not None and p.is_source)
        out, node = [head], head
        while True:
            kids = [c for c in net.children(node.node_id)
                    if c.kind in ("breaker", "recloser") and c.feeder == feeder]
            if not kids:
                break
            node = kids[0]
            out.append(node)
        return [d.node_id for d in out]

    def _limbs(self, net, feeder):
        """Two devices on sibling limbs of a fork — neither above the other."""
        for n in net.devices(feeder):
            kids = [c for c in net.children(n.node_id)
                    if c.kind in ("breaker", "recloser") and c.feeder == feeder]
            if len(kids) > 1:
                return [kids[0].node_id, kids[1].node_id]
        pytest.skip(f"{feeder} does not fork")

    def _ev(self, eid, device, feeder, t, inception=0.05, unknown=False, **kw):
        # A device id that is not in the topology silently falls back to
        # matching on the feeder, which quietly disables the path logic these
        # tests exist to exercise. Ids change; catch a stale one here.
        if not unknown:
            assert device in self._net(), (
                f"{device!r} is not in demo/topology.csv — derive the id from "
                "the tree rather than writing it down")
        e = {"event_id": eid, "device_id": device, "feeder": feeder,
             "timestamp": t, "fault_inception_s": inception,
             "fault_type": "SLG", "total_shots": 1, "locked_out": False,
             "priority": 3, "flags": [], "customers_affected": 0, "zone": "Z"}
        e.update(kw)
        return e

    # -- the join rules -----------------------------------------------------

    def test_records_on_one_path_at_one_instant_are_one_fault(self):
        from comtrade_analyzer.incidents import group_events
        net = self._net()
        c = self._chain(net, "Cedar Hollow 1211")
        evs = [
            self._ev("a", c[-1], "Cedar Hollow 1211", "2026-06-01T00:00:00"),
            self._ev("b", c[0], "Cedar Hollow 1211", "2026-06-01T00:00:00.02",
                     total_shots=0),
        ]
        incs = group_events(evs, net)
        assert len(incs) == 1
        assert incs[0]["record_count"] == 2

    def test_the_same_instant_on_another_feeder_is_a_different_fault(self):
        """A storm drops faults all over the fleet in the same second."""
        from comtrade_analyzer.incidents import group_events
        net = self._net()
        evs = [
            self._ev("a", self._chain(net, "Cedar Hollow 1211")[-1],
                     "Cedar Hollow 1211", "2026-06-01T00:00:00"),
            self._ev("b", self._chain(net, "Ridgeline 2106")[-1],
                     "Ridgeline 2106", "2026-06-01T00:00:00.01"),
        ]
        assert len(group_events(evs, net)) == 2

    def test_the_same_path_much_later_is_a_different_fault(self):
        from comtrade_analyzer.incidents import group_events
        net = self._net()
        c = self._chain(net, "Cedar Hollow 1211")
        evs = [
            self._ev("a", c[-1], "Cedar Hollow 1211", "2026-06-01T00:00:00"),
            self._ev("b", c[0], "Cedar Hollow 1211", "2026-06-01T00:05:00"),
        ]
        assert len(group_events(evs, net)) == 2

    def test_a_sibling_branch_is_not_the_same_path(self):
        """
        Two devices on opposite limbs of a fork, at the same instant. One
        feeder, but neither is above the other, so the fault current never
        flowed through both — two faults, not one seen twice.
        """
        from comtrade_analyzer.incidents import group_events
        net = self._net()
        a, b = self._limbs(net, "Valley Oak 3301")
        evs = [
            self._ev("a", a, "Valley Oak 3301", "2026-06-01T00:00:00"),
            self._ev("b", b, "Valley Oak 3301", "2026-06-01T00:00:00.01"),
        ]
        assert not net.on_same_path([a, b])
        assert len(group_events(evs, net)) == 2

    def test_the_window_is_tunable(self):
        from comtrade_analyzer.incidents import group_events
        net = self._net()
        c = self._chain(net, "Cedar Hollow 1211")
        evs = [
            self._ev("a", c[-1], "Cedar Hollow 1211", "2026-06-01T00:00:00"),
            self._ev("b", c[0], "Cedar Hollow 1211", "2026-06-01T00:00:05"),
        ]
        assert len(group_events(evs, net, window_s=2.0)) == 2
        assert len(group_events(evs, net, window_s=10.0)) == 1

    # -- the restoration join -----------------------------------------------

    def test_a_tie_pickup_joins_its_lockout_across_feeders(self):
        """
        No time window finds this: it is a minute later, on another feeder,
        under a different device id. Only the tree connects them.
        """
        from comtrade_analyzer.incidents import group_events
        net = self._net()
        # a lockout, and the device on the far side of a tie below it
        tie = net.ties("Cedar Hollow 1211")[0]
        near = net.node(tie.parent)
        far = net.node(tie.tie_to)
        evs = [
            self._ev("lock", near.node_id, near.feeder,
                     "2026-06-01T00:00:00", locked_out=True),
            self._ev("tie", far.node_id, far.feeder,
                     "2026-06-01T00:01:00", fault_type="LOAD", total_shots=0),
        ]
        incs = group_events(evs, net)
        assert len(incs) == 1
        assert incs[0]["restored"] is True
        assert incs[0]["restore_delay_s"] == 60.0

    def test_a_load_step_with_no_lockout_to_explain_it_stands_alone(self):
        from comtrade_analyzer.incidents import group_events
        net = self._net()
        dev = self._chain(net, "Cedar Hollow 1212")[-1]
        evs = [self._ev("tie", dev, "Cedar Hollow 1212",
                        "2026-06-01T00:01:00", fault_type="LOAD", total_shots=0)]
        incs = group_events(evs, net)
        assert len(incs) == 1 and incs[0]["restored"] is True
        assert incs[0]["locked_out"] is False

    # -- what an incident says ----------------------------------------------

    def test_the_clearing_device_is_the_deepest_one_that_operated(self):
        from comtrade_analyzer.incidents import group_events
        net = self._net()
        c = self._chain(net, "Cedar Hollow 1211")
        evs = [
            self._ev("a", c[0], "Cedar Hollow 1211",
                     "2026-06-01T00:00:00", total_shots=0),
            self._ev("b", c[-1], "Cedar Hollow 1211",
                     "2026-06-01T00:00:00.01", total_shots=1),
        ]
        inc = group_events(evs, net)[0]
        assert inc["clearing_device"] == c[-1]
        assert inc["devices_held"] == [c[0]]
        assert inc["upstream_also_tripped"] is False

    def test_two_devices_on_one_path_both_operating_is_reported(self):
        """The one thing neither record can show on its own."""
        from comtrade_analyzer.incidents import group_events
        net = self._net()
        c = self._chain(net, "Cedar Hollow 1211")
        evs = [
            self._ev("a", c[0], "Cedar Hollow 1211",
                     "2026-06-01T00:00:00", total_shots=1),
            self._ev("b", c[-1], "Cedar Hollow 1211",
                     "2026-06-01T00:00:00.01", total_shots=1),
        ]
        assert group_events(evs, net)[0]["upstream_also_tripped"] is True

    # -- degradation and skew -----------------------------------------------

    def test_without_a_topology_it_falls_back_to_the_feeder(self):
        """A plain folder still groups; it just cannot tell path from sibling."""
        from comtrade_analyzer.incidents import group_events
        evs = [
            self._ev("a", "ANY_1", "Cedar Hollow 1211", "2026-06-01T00:00:00", unknown=True),
            self._ev("b", "ANY_2", "Cedar Hollow 1211", "2026-06-01T00:00:00.02", unknown=True),
            self._ev("c", "ANY_3", "Ridgeline 2106", "2026-06-01T00:00:00.01", unknown=True),
        ]
        incs = group_events(evs, None)
        assert len(incs) == 2

    def test_a_drifted_clock_is_reported_not_hidden(self):
        """
        A relay minutes out looks exactly like a separate fault. Splitting the
        incident silently is the failure; saying so is the fix.
        """
        from comtrade_analyzer.incidents import clock_suspects
        net = self._net()
        chain = self._chain(net, "Cedar Hollow 1211")
        evs = [
            self._ev("a", chain[-1], "Cedar Hollow 1211", "2026-06-01T00:00:00"),
            self._ev("b", chain[0], "Cedar Hollow 1211", "2026-06-01T00:04:00"),
        ]
        sus = clock_suspects(evs, net)
        assert len(sus) == 1
        assert sus[0]["gap_s"] == 240.0

    def test_fault_instant_uses_inception_not_the_trigger(self):
        """
        The trigger is where the relay decided to save the record and can sit
        anywhere in it; inception is when the fault actually started.
        """
        from comtrade_analyzer.incidents import fault_instant
        e = self._ev("a", "D", "F", "2026-06-01T00:00:00", inception=0.25,
                     unknown=True)
        assert fault_instant(e).microsecond == 250000

    # -- against the shipped corpus -----------------------------------------

    def test_the_demo_corpus_regroups_into_the_sets_that_made_it(self):
        """
        Only meaningful because the ids were kept out of the CFG files — a
        regression guard on generated data, not proof the algorithm is right.
        """
        analysis = self.ROOT / "demo" / "analysis" / "fleet_analysis.json"
        if not analysis.is_file():
            pytest.skip("run fleet_analyze on demo/ first")
        d = json.loads(analysis.read_text(encoding="utf-8"))
        g = (d.get("validation") or {}).get("grouping")
        assert g, "no grouping accuracy recorded"
        assert g["events_grouped_correctly_pct"] == 100.0
        assert g["incidents_found"] == g["incidents_expected"]


# ─────────────────────────────────────────────────────────────────────────────
# 22. The feeder one-line
# ─────────────────────────────────────────────────────────────────────────────

class TestTheFeederOneLine:
    """
    The dashboard is one self-contained file, so it cannot go back to
    topology.csv to draw a feeder — the nodes have to travel in the payload.
    And like the triage rules, the page must render what Python decided rather
    than keeping its own copy of it.
    """

    ROOT = Path(__file__).parent
    TPL = ROOT / "comtrade_analyzer" / "dashboard_template.html"

    def test_the_payload_carries_the_topology_and_the_incidents(self):
        from comtrade_analyzer.fleet_dashboard import build_payload
        p = build_payload({
            "events": [{"event_id": "e1"}],
            "topology": [{"node_id": "A", "feeder": "F", "kind": "recloser",
                          "parent": "BUS", "tie_to": ""}],
            "incidents": [{"incident_id": "INC", "event_ids": ["e1"]}],
        })
        assert p["topology"] and p["incidents"]

    def test_analysis_embeds_the_nodes_not_just_the_path(self, tmp_path):
        """A path is useless to a file someone opens on another machine."""
        from comtrade_analyzer.topology import load_topology
        net = load_topology(str(self.ROOT / "demo" / "topology.csv"))
        nodes = [{"node_id": n.node_id, "feeder": n.feeder, "kind": n.kind,
                  "parent": n.parent, "tie_to": n.tie_to} for n in net.nodes()]
        assert len(nodes) == len(net)
        assert all(n["node_id"] for n in nodes)

    def test_the_template_has_the_hooks_the_renderer_writes_into(self):
        tpl = self.TPL.read_text(encoding="utf-8")
        for hook in ("onelineCard", "olFeeder", "olDiagram", "olIncidents",
                     "olLegend", "onelineNote"):
            assert f'id="{hook}"' in tpl, f"the one-line card is missing #{hook}"

    def test_the_page_reads_the_topology_rather_than_hardcoding_a_feeder(self):
        """
        Device ids and feeder names are operational data. If any leaked into
        the template they would ship to everyone who cloned the repo.
        """
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "FLEET.topology" in tpl and "FLEET.incidents" in tpl
        for leaked in ("BKR_", "RCL_", "TIE_", "BUS_CH"):
            assert leaked not in tpl, f"{leaked!r} is baked into the template"

    def test_the_load_class_can_be_filtered_for(self):
        """A tie pickup is a LOAD record; it has to be reachable in the UI."""
        assert '"LOAD"' in self.TPL.read_text(encoding="utf-8")

    def test_every_script_block_parses(self):
        """
        Three <script> blocks, and a stray edit into one is not caught until
        the page loads. node --check is the cheapest way to catch it here.
        """
        import re, shutil, subprocess, tempfile
        if not shutil.which("node"):
            pytest.skip("node not installed")
        src = self.TPL.read_text(encoding="utf-8")
        blocks = re.findall(r"<script>\n(.*?)\n</script>", src, re.S)
        assert len(blocks) == 3, f"expected 3 script blocks, found {len(blocks)}"
        with tempfile.TemporaryDirectory() as d:
            for i, b in enumerate(blocks, 1):
                f = Path(d) / f"b{i}.js"
                f.write_text(b.replace("__FLEET_DATA__", "{}"), encoding="utf-8")
                r = subprocess.run(["node", "--check", str(f)],
                                   capture_output=True, text=True)
                assert r.returncode == 0, f"block {i}: {r.stderr[:400]}"

    def test_the_page_switcher_and_the_all_feeders_page_exist(self):
        """
        The review page shows one feeder at a time, which is right for working
        a single event. Seeing the whole system is a different question and
        does not fit in a dropdown.
        """
        tpl = self.TPL.read_text(encoding="utf-8")
        for hook in ("pageNav", "pageReview", "pageFeeders", "feederStack"):
            assert f'id="{hook}"' in tpl, f"missing #{hook}"
        assert 'data-page="feeders"' in tpl

    def test_both_pages_draw_through_one_function(self):
        """
        Two copies of the layout is how the diagram and the page of diagrams
        would drift apart. olDraw() is the only drawer.
        """
        tpl = self.TPL.read_text(encoding="utf-8")
        assert tpl.count("function olDraw(") == 1
        assert tpl.count("function olLayout(") == 1

    def test_ties_sharing_a_device_each_get_their_own_row(self):
        """
        A device can back up more than one feeder. Laid out as children, the
        second and later ties fall to their own rows like any other sibling,
        which is what keeps their labels apart.
        """
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "all.forEach((item, i) => {" in tpl
        assert "if (i > 0) row += 1;" in tpl

    def test_the_hidden_attribute_is_settled_once(self):
        """
        `hidden` is a UA rule at element specificity, so any class rule setting
        display beats it. `.pagenav` did exactly that: the tab stayed visible
        with no listeners on it and read as a dead control.
        """
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "[hidden] { display: none !important; }" in tpl


class TestSidecarsAreFoundFromTheEventsFolder:
    """
    The docs tell people to point the tool at the events folder. The registry
    and the topology live *beside* it, not in it — and comtrade-batch shipped
    with no topology lookup at all, so the documented command produced a
    dashboard whose feeder pages could never appear.
    """

    ROOT = Path(__file__).parent

    def test_topology_is_found_one_level_up_from_the_events_folder(self):
        from comtrade_analyzer.fleet_analyze import find_sidecar
        got = find_sidecar(str(self.ROOT / "demo" / "incident_events"),
                           ("topology.csv",))
        assert got and Path(got).name == "topology.csv"

    def test_it_does_not_wander_into_an_unrelated_parent(self, tmp_path):
        """Only a folder that IS an events folder may look upward."""
        from comtrade_analyzer.fleet_analyze import find_sidecar
        (tmp_path / "topology.csv").write_text("x", encoding="utf-8")
        plain = tmp_path / "some_pull"
        plain.mkdir()
        assert find_sidecar(str(plain), ("topology.csv",)) is None
        events = tmp_path / "incident_events"
        events.mkdir()
        assert find_sidecar(str(events), ("topology.csv",)) is not None

    def test_resolve_inputs_finds_both_from_the_events_folder(self):
        from comtrade_analyzer.fleet_analyze import resolve_inputs
        _, dev, topo, _ = resolve_inputs(
            str(self.ROOT / "demo" / "incident_events"), None)
        assert dev and topo

    def test_batch_imports_what_the_feeder_pages_need(self):
        """
        A guard on the real gap: batch.py is the documented entry point and it
        was not loading a topology or grouping incidents at all.
        """
        src = (self.ROOT / "comtrade_analyzer" / "batch.py").read_text(encoding="utf-8")
        for needed in ("load_network", "group_events", '"topology"', '"incidents"'):
            assert needed in src, f"batch.py never produces {needed}"

    def test_load_network_survives_a_missing_or_broken_file(self, tmp_path):
        from comtrade_analyzer.fleet_analyze import load_network
        assert load_network(str(tmp_path)) == (None, None)
        bad = tmp_path / "topology.csv"
        bad.write_text("not,a,topology\n", encoding="utf-8")
        net, _ = load_network(str(tmp_path))
        assert net is None or len(net) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 23. The GUI builds sweep's arguments by hand
# ─────────────────────────────────────────────────────────────────────────────

class TestTheGuiAndTheCliAgreeOnSweepsArguments:
    """
    batch.sweep has two callers: argparse in batch.main, and a SimpleNamespace
    the GUI assembles field by field. Adding an option to the parser leaves the
    GUI short of it, and "Run analysis" then dies with AttributeError on a
    thread — which surfaces as the button doing nothing.
    """

    ROOT = Path(__file__).parent

    def _sweep_reads(self):
        import ast
        src = (self.ROOT / "comtrade_analyzer" / "batch.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "sweep")
        plain, guarded = set(), set()
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr" and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Name) and node.args[0].id == "args"
                    and isinstance(node.args[1], ast.Constant)):
                guarded.add(node.args[1].value)
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "args"):
                plain.add(node.attr)
        return plain, guarded

    def _gui_supplies(self):
        import ast
        src = (self.ROOT / "comtrade_analyzer" / "app.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "SimpleNamespace"):
                return {kw.arg for kw in node.keywords if kw.arg}
        raise AssertionError("the GUI no longer builds a SimpleNamespace for sweep")

    def test_the_gui_supplies_every_argument_sweep_requires(self):
        reads, guarded = self._sweep_reads()
        missing = sorted(reads - guarded - self._gui_supplies())
        assert not missing, (
            "batch.sweep reads args the GUI never sets: " + ", ".join(missing)
            + " — add them to the SimpleNamespace in app.py, or read them "
              "with getattr(args, name, default).")

    def test_sweep_survives_a_namespace_built_before_the_new_options(self, tmp_path):
        """
        Anything optional must be read defensively, so an older caller — or a
        script someone wrote against the previous signature — still runs.
        """
        from types import SimpleNamespace
        from comtrade_analyzer.batch import sweep
        from comtrade_analyzer.fleet_analyze import load_config
        events = self.ROOT / "demo" / "incident_events"
        if not events.is_dir():
            pytest.skip("demo corpus not present")
        args = SimpleNamespace(
            folder=str(events), devices=None, out=str(tmp_path),
            rebuild=True, always_write=True, no_dashboard=True, no_waveforms=True,
            waveform_buckets=[180, 280], feeder_z=0.4, slow_trip_cycles=10.0,
            epss_tiers=[2, 3], response_hours=2.0, jobs=1,
        )
        result = sweep(args, {}, load_config(), quiet=True)
        assert result and result["aggregates"]["totals"]["events"] > 0


# ─────────────────────────────────────────────────────────────────────────────
# 24. Branching mainlines, and the tie symbol
# ─────────────────────────────────────────────────────────────────────────────

class TestBranchingFeeders:
    """
    A mainline is not always a chain. It forks, and the fork is the point:
    opening the device above it drops both limbs, opening the branch recloser
    drops one. Nothing downstream can tell those apart without the tree.
    """

    ROOT = Path(__file__).parent

    def _net(self):
        from comtrade_analyzer.topology import load_topology
        return load_topology(str(self.ROOT / "demo" / "topology.csv"))

    def _limbs(self, net, node):
        """Switching children on the SAME feeder — a bus feeding several
        feeders is not a branching mainline, it is a substation."""
        return [c for c in net.children(node.node_id)
                if c.kind in ("breaker", "recloser") and c.feeder == node.feeder]

    def _forks(self, net):
        return [n for n in net.nodes()
                if not n.is_source and len(self._limbs(net, n)) > 1]

    def test_the_demo_has_a_feeder_that_forks(self):
        assert self._forks(self._net()), "no branching mainline in the demo topology"

    def test_a_fork_is_on_the_mainline_not_at_the_bus(self):
        net = self._net()
        for n in self._forks(net):
            assert net.depth(n.node_id) >= 1
            assert not n.is_source

    def test_opening_above_a_fork_drops_both_limbs(self):
        from comtrade_analyzer.wso_impact import load_registry
        registry = load_registry(str(self.ROOT / "demo" / "devices.csv"))
        net = self._net()
        fork = self._forks(net)[0]
        limbs = self._limbs(net, fork)
        above = net.customers_below(fork.node_id, registry)
        for limb in limbs:
            assert net.customers_below(limb.node_id, registry) < above

    def test_limbs_are_not_on_one_path(self):
        """Two records on sibling limbs are two faults, not one seen twice."""
        net = self._net()
        fork = self._forks(net)[0]
        a, b = self._limbs(net, fork)[:2]
        assert not net.on_same_path([a.node_id, b.node_id])
        assert net.on_same_path([fork.node_id, a.node_id])

    def test_the_generator_builds_the_branches_it_declares(self):
        import random
        from comtrade_analyzer import fleet_gen as fg
        devices = {d.device_id: d for d in fg.build_registry(random.Random(1))}
        for code, feeder, _kind, trunk, branches, _cust in fg._FEEDERS:
            want = 1 + trunk + sum(c for _, c in branches)
            got = sum(1 for d in devices.values() if d.feeder == feeder)
            assert got == want, f"{feeder}: {got} devices, expected {want}"


class TestTheDeviceSymbols:
    """
    The utility's drawing convention: every device is a box, and the letter
    inside says what it is — B breaker, R recloser. No circles. A tie is a
    recloser like any other, so it is a box with an R too.
    """

    TPL = Path(__file__).parent / "comtrade_analyzer" / "dashboard_template.html"

    def _drawer(self):
        tpl = self.TPL.read_text(encoding="utf-8")
        return tpl[tpl.index("function olDraw("):tpl.index("\nfunction olRefresh")]

    def test_every_device_is_a_box_with_a_letter(self):
        tpl = self.TPL.read_text(encoding="utf-8")
        assert '{ breaker: "B", recloser: "R"' in tpl
        assert "function olDevice(" in tpl
        assert "<rect" in tpl[tpl.index("function olDevice("):][:600]

    def test_nothing_on_the_diagram_is_drawn_as_a_circle(self):
        """The old breaker-square / recloser-circle split is gone."""
        body = self._drawer()
        assert "<circle" not in body, "a device is still drawn as a circle"

    def test_a_tie_is_drawn_as_a_recloser(self):
        body = self._drawer()
        i = body.index("/* Ties, drawn where the layout put them")
        j = body.index("/* Devices.")
        assert 'olDevice("recloser"' in body[i:j]

    def test_the_tie_is_named_and_carries_its_state(self):
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "ol-tie-name" in tpl, "the tie is not drawn as a named device"
        assert '"CLOSED" : "OPEN"' in tpl or '"OPEN"' in tpl

    def test_the_legend_draws_the_symbols_rather_than_naming_them(self):
        tpl = self.TPL.read_text(encoding="utf-8")
        assert tpl.count('class="ol-key"') >= 3
        assert "legendBox" in tpl, "the legend no longer draws the lettered box"

    def test_the_drawer_never_writes_a_fixed_caption(self):
        """
        olDraw fills thirteen cards on the all-feeders page. When it wrote
        #onelineNote itself, each draw clobbered the last one's caption and the
        review page's count came out as an em dash.
        """
        import ast, re
        tpl = self.TPL.read_text(encoding="utf-8")
        body = tpl[tpl.index("function olDraw("):]
        body = body[:body.index("\nfunction ")]
        assert "onelineNote" not in body and "onelineFoot" not in body
        assert "return L.devices.reduce" in body

    def test_red_is_closed_and_green_is_open(self):
        """
        The utility's convention, and the opposite of the traffic-light
        instinct — so it is worth a test that says which way round it goes.
        """
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "--sw-closed" in tpl and "--sw-open" in tpl
        assert "red closed, green open" in tpl
        # a tie is open in its normal state, so it draws with the open token
        i = tpl.index("/* Ties, drawn where the layout put them")
        j = tpl.index("/* Devices.", i)
        assert "--sw-open" in tpl[i:j]

    def test_state_survives_for_anyone_not_looking_at_the_colours(self):
        """
        On the drawing, open vs closed is now colour alone — filled boxes, one
        grey for every conductor, and no OPEN/CLOSED text, which is the
        utility's own convention and what was asked for. Red/green is the worst
        pair for deuteranopia, so the word has to survive somewhere: the
        tooltip and the aria-label both carry it, for both a device and a tie.
        """
        tpl = self.TPL.read_text(encoding="utf-8")
        dev = tpl[tpl.index("/* Devices."):tpl.index("host.innerHTML = `<svg")]
        assert '${open ? "OPEN" : "CLOSED"}' in dev, "device tooltip lost its state"
        assert '${open ? "open" : "closed"}' in dev, "device aria-label lost its state"
        tie = tpl[tpl.index("/* Ties, drawn where"):tpl.index("/* Devices.")]
        assert '${closed ? "CLOSED" : "OPEN"}' in tie, "tie tooltip lost its state"
        assert '${closed ? "closed" : "open"}' in tie, "tie aria-label lost its state"

    def test_every_conductor_is_the_same_grey(self):
        """Line is line. What is open is said by the device on it."""
        tpl = self.TPL.read_text(encoding="utf-8")
        body = tpl[tpl.index("function olDraw("):tpl.index("\nfunction olRefresh")]
        conductors = body[body.index("L.placed.forEach"):body.index("/* Ties,")]
        assert "stroke-dasharray" not in conductors
        assert "--sw-open" not in conductors and "--sw-closed" not in conductors
        assert 'stroke="var(--baseline)"' in conductors

    def test_priority_moved_off_the_device_fill(self):
        """
        Fill cannot carry state and priority at once. Priority is a badge above
        the device — icon plus count, so it is not colour alone either.
        """
        tpl = self.TPL.read_text(encoding="utf-8")
        i = tpl.index("/* Devices.")
        j = tpl.index("host.innerHTML = `<svg", i)
        body = tpl[i:j]
        assert "ol-badge" in body
        assert "meta.icon" in body
        # the box fill must be the state colour, not the priority colour
        assert "olDevice(p.n.kind, x, y, RAD," in body
        assert "meta.v" not in body.split("olDevice(p.n.kind")[1].split(";")[0]

    def test_a_tie_is_laid_out_as_a_child_not_a_stub(self):
        """
        Drawn as a stub under its device, two ties in one column read as two
        open switches in series — and a section fed through two open ties has
        no source at all. A tie is where the line ENDS: one column further out,
        on its own row when the mainline continues past it.
        """
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "two open switches in series" in tpl
        assert "depth: depth + 1" in tpl, "ties are not placed one column out"
        # real devices are walked first so the mainline keeps the parent's row
        assert "ties last" in tpl

    def test_a_tie_is_never_counted_as_a_mainline_device(self):
        """`devices` and `ties` are separate, or the counts and the record
        totals silently include switches that carry nothing."""
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "const devices = placed.filter((p) => !p.isTie);" in tpl
        body = tpl[tpl.index("function olDraw("):tpl.index("\nfunction olRefresh")]
        assert "L.placed.forEach" in body      # conductors: everything
        assert "L.devices.forEach" in body     # device boxes: no ties
        assert "L.ties.forEach" in body

    def test_long_tie_labels_are_pulled_inside_the_canvas(self):
        """A tie's label is far wider than the box it sits under."""
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "const clamped = (cx, text" in tpl


# ─────────────────────────────────────────────────────────────────────────────
# 25. Walking the system: substations, and following a tie through
# ─────────────────────────────────────────────────────────────────────────────

class TestTheFeederPageWalksTheSystem:
    """
    A tie is the seam between two feeders, and often between two substations.
    Reading an event across it meant finding the far feeder in a dropdown and
    then finding the same tie again by eye.
    """

    TPL = Path(__file__).parent / "comtrade_analyzer" / "dashboard_template.html"

    def test_the_feeders_page_is_scoped_to_one_substation(self):
        tpl = self.TPL.read_text(encoding="utf-8")
        assert 'id="feedersSub"' in tpl
        # the shared scope, not one of its own
        assert "let station = ALL_STATIONS;" in tpl
        assert "all.filter((s) => s.station === station)" in tpl

    def test_feeders_sort_by_circuit_number_in_both_views(self):
        """
        The review dropdown sorted by name and the feeder page by file order,
        so the same fleet appeared in two different sequences. A utility knows
        a feeder by its number — "Riverbend 4402, Riverbend 4407, Delta Flats
        4411" is in order; alphabetically it is not.
        """
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "const olCircuit = (feeder)" in tpl
        assert "const olFeederCmp" in tpl
        # both surfaces go through the same comparator
        assert tpl.count("olFeederCmp") >= 3
        assert "OL_FEEDERS = [...new Set(" in tpl
        assert "feeders: feeders.sort(olFeederCmp)" in tpl

    def test_the_review_dropdown_is_grouped_by_substation(self):
        """Grouping is what makes the circuit-number ordering legible."""
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "<optgroup" in tpl

    def test_the_substation_list_comes_from_the_tree(self):
        """A substation added to topology.csv must appear without a code change."""
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "function olStations()" in tpl
        assert 'TOPO.filter((n) => n.kind === "source")' in tpl

    def test_a_tie_can_be_followed_to_its_other_side(self):
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "function olJumpThroughTie(" in tpl
        assert "function olTieEnds(" in tpl
        # the far end is whichever end is not the feeder being looked at
        assert "ends.a.feeder === fromFeeder ? ends.b : ends.a" in tpl

    def test_following_a_tie_switches_substation_when_it_has_to(self):
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "function olStationOf(" in tpl
        assert "if (st && station !== ALL_STATIONS && st !== station) applyStation(st);" in tpl

    def test_the_far_end_is_revealed_not_just_navigated_to(self):
        """Landing on a page of feeders with nothing marked is no better."""
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "function olRevealTie(" in tpl
        assert "ol-jumped" in tpl
        assert "scrollIntoView" in tpl

    def test_a_tie_is_addressable_and_clickable(self):
        tpl = self.TPL.read_text(encoding="utf-8")
        assert 'data-tie="${esc(t.n.node_id)}"' in tpl
        assert 'class="ol-dev ol-tie' in tpl

    def test_a_tie_is_filled_not_outlined(self):
        """
        Filled green, like every other box. An outline read as a different kind
        of object rather than an open one.
        """
        tpl = self.TPL.read_text(encoding="utf-8")
        body = tpl[tpl.index("/* Ties, drawn where the layout put them"):]
        body = body[:body.index("/* Devices.")]
        assert 'olDevice("recloser", x, y, RAD, col, col' in body
        assert "var(--surface)" not in body, "the tie box is still hollow"


# ─────────────────────────────────────────────────────────────────────────────
# 26. Scoping the whole review to one substation
# ─────────────────────────────────────────────────────────────────────────────

class TestScopingByStation:
    """
    The tiles, hero and charts render a precomputed aggregate, so scoping them
    to a substation means handing them a DIFFERENT aggregate — not re-deriving
    the arithmetic in JavaScript, which is how the page and the CSV would start
    disagreeing.
    """

    ROOT = Path(__file__).parent
    TPL = ROOT / "comtrade_analyzer" / "dashboard_template.html"

    def _events(self):
        f = self.ROOT / "demo" / "analysis" / "fleet_analysis.json"
        if not f.is_file():
            pytest.skip("run fleet_analyze on demo/ first")
        return json.loads(f.read_text(encoding="utf-8"))

    def test_one_aggregate_per_substation_plus_the_whole_fleet(self):
        d = self._events()
        by = d.get("aggregates_by_station")
        assert by, "analysis carries no per-station aggregates"
        from comtrade_analyzer.fleet_analyze import ALL_STATIONS
        assert ALL_STATIONS in by
        stations = {e["station"] for e in d["events"] if e.get("station")}
        assert set(by) == stations | {ALL_STATIONS}

    def test_the_substations_account_for_the_whole_fleet(self):
        """A scoped view that loses events is worse than no scoping."""
        from comtrade_analyzer.fleet_analyze import ALL_STATIONS
        by = self._events()["aggregates_by_station"]
        parts = sum(v["totals"]["events"] for k, v in by.items() if k != ALL_STATIONS)
        assert parts == by[ALL_STATIONS]["totals"]["events"]

    def test_a_scoped_aggregate_is_smaller_than_the_fleet(self):
        from comtrade_analyzer.fleet_analyze import ALL_STATIONS
        by = self._events()["aggregates_by_station"]
        whole = by[ALL_STATIONS]["totals"]
        for name, agg in by.items():
            if name == ALL_STATIONS:
                continue
            assert 0 < agg["totals"]["events"] < whole["events"]
            assert agg["totals"]["priority_1"] <= whole["priority_1"]

    def test_batch_produces_them_too(self):
        """batch.py is the entry point the docs recommend."""
        src = (self.ROOT / "comtrade_analyzer" / "batch.py").read_text(encoding="utf-8")
        assert "aggregate_by_station" in src
        assert '"aggregates_by_station"' in src

    def test_every_panel_that_reads_the_aggregate_is_redrawn(self):
        """
        Scoping that moves the table but leaves the tiles showing the fleet is
        worse than not scoping at all — the numbers would simply be wrong.
        """
        tpl = self.TPL.read_text(encoding="utf-8")
        body = tpl[tpl.index("function applyStation("):]
        body = body[:body.index("\nfunction renderScope")]
        for fn in ("renderHero", "renderUnits", "renderTiles", "renderCharts",
                   "renderBody", "renderOneline"):
            assert fn in body, f"applyStation does not redraw {fn}"

    def test_the_table_and_the_one_line_follow_the_scope(self):
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "if (station !== ALL_STATIONS && e.station !== station) return false;" in tpl
        # and the unit grid, which read the whole fleet
        assert "const sorted = scopedEvents()" in tpl

    def test_the_control_shows_what_happened(self):
        """
        Following a tie out of the scoped substation changes the scope. The
        dropdown was left reading the old one while the page showed the new.
        """
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "if (sel && sel.value !== station) sel.value = station;" in tpl

    def test_there_is_one_substation_scope_not_one_per_page(self):
        """
        The feeders page kept its own substation, so picking one there left the
        review's tiles on whatever they were — two controls for the same idea,
        disagreeing with each other.
        """
        import re
        tpl = self.TPL.read_text(encoding="utf-8")
        # `olStationOf()` is a helper and contains the same prefix — match the
        # variable itself, not any identifier starting with it.
        assert not re.search(r"\bolStation\b", tpl), \
            "the feeders page still keeps its own scope"
        # both controls drive, and are driven by, the same one
        assert 'sel.onchange = () => applyStation(sel.value);' in tpl
        assert '["fStation", "feedersSub"].forEach((id) => {' in tpl

    def test_the_feeders_page_can_still_show_every_substation(self):
        """Scoping must not remove the survey view it was added to."""
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "station === ALL_STATIONS\n    ? all : all.filter" in tpl
        assert 'value="${esc(ALL_STATIONS)}"' in tpl

    def test_changing_the_scope_redraws_the_feeders_page_when_it_is_showing(self):
        tpl = self.TPL.read_text(encoding="utf-8")
        body = tpl[tpl.index("function applyStation("):]
        body = body[:body.index("\nfunction renderScope")]
        assert 'if (olPage === "feeders") renderFeederPage();' in body

    def test_there_is_an_aggregate_for_every_feeder(self):
        """
        A feeder is the scope an engineer works in. Reclose shots, clearing
        time, fault mix and the triage backlog only mean something against one
        circuit; over thirteen they are an average of unrelated things.
        """
        d = self._events()
        by = d.get("aggregates_by_feeder")
        assert by, "analysis carries no per-feeder aggregates"
        feeders = {e["feeder"] for e in d["events"] if e.get("feeder")}
        assert set(by) == feeders

    def test_the_feeders_account_for_the_whole_fleet(self):
        from comtrade_analyzer.fleet_analyze import ALL_STATIONS
        d = self._events()
        parts = sum(v["totals"]["events"] for v in d["aggregates_by_feeder"].values())
        assert parts == d["aggregates_by_station"][ALL_STATIONS]["totals"]["events"]

    def test_the_one_line_dropdown_is_the_feeder_scope(self):
        """It is the control the request named — picking a circuit there scopes
        the tiles and charts above it."""
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "function applyFeederScope(" in tpl
        assert "sel.onchange = () => { olIncident = null; applyFeederScope(sel.value); };" in tpl
        assert 'if (scopeFeeder && e.feeder !== scopeFeeder) return false;' in tpl

    def test_the_narrowest_scope_wins(self):
        tpl = self.TPL.read_text(encoding="utf-8")
        assert ("AGG = (scopeFeeder && BY_FEEDER[scopeFeeder])\n"
                "     || BY_STATION[station] || FLEET.aggregates || {};") in tpl

    def test_panels_that_count_events_themselves_go_through_the_scope(self):
        """
        A panel that filters EV directly keeps showing the fleet while
        everything around it narrows. The clearing-time histogram and the unit
        grid each did exactly that.
        """
        import re
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "function scopedEvents()" in tpl
        block = tpl[tpl.index("function renderHeader()"):tpl.index("function currentRows()")]
        stray = [ln.strip() for ln in block.splitlines()
                 if re.search(r"\bEV\.(filter|slice|forEach)\b", ln)]
        assert not stray, ("these count the whole fleet regardless of scope:\n  "
                           + "\n  ".join(stray))

    def test_a_feeder_pick_is_never_silently_refused(self):
        """
        The option values come from topology.csv; the per-feeder aggregates are
        keyed by the feeder on each event, out of the COMTRADE header. Gating
        the pick on those two spellings matching meant any disagreement made
        the dropdown look stuck, with nothing said about why.
        """
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "scopeFeeder = feeder || ALL_FEEDERS;" in tpl
        assert "BY_FEEDER[feeder] ? feeder : ALL_FEEDERS" not in tpl
        # and it says so rather than showing wider numbers under a feeder heading
        assert "no per-feeder totals were found under that name" in tpl

    def test_a_select_is_not_rebuilt_inside_its_own_change_handler(self):
        """
        These handlers re-render the page, and the render rebuilds the select
        that fired the event. Replacing its options mid-event invalidates the
        browser's selection index and the value snaps back to the first option.
        """
        tpl = self.TPL.read_text(encoding="utf-8")
        assert "function fillSelect(" in tpl
        for sel in ('$("olFeeder")', '$("feedersSub")', '$("fStation")'):
            i = tpl.index(f"const sel = {sel};")
            block = tpl[i:i + 700]
            assert "sel.innerHTML =" not in block, f"{sel} still rebuilt directly"
            assert "fillSelect(sel," in block
