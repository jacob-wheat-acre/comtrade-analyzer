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
  17. End-to-end against the generated fixtures (skipped if not generated)
"""

import json
import math
import struct
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
                  trip_cycle=None, phase_shift=None, unfaulted_scale=1.0):
    """
    Load current for `fault_cycle` cycles, then elevated current on `faulted`.

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
        chans[f"V{p}N"] = _sine(10000.0, n, PH[p])

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
# 17. End to end against the generated fixtures
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
