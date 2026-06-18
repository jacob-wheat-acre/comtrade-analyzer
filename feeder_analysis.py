"""
feeder_analysis.py — Distribution feeder and recloser-specific analysis.

Designed for events captured by feeder relays (SEL-351, GE D60, ABB REF615, etc.)
and hydraulic/electronic reclosers (Kyle, Cooper, Nulec, SEL-651R, etc.).

Key differences from transmission events
-----------------------------------------
Multi-shot reclosing
    A single COMTRADE record may contain 2–4 complete fault-trip-reclose cycles
    (e.g., FAST-FAST-SLOW-SLOW before lockout).  Each cycle is a "shot."

Fast vs slow element discrimination
    50P/50G (instantaneous, < 2–3 cycles, ~33–50 ms) = fuse-saving philosophy.
    51P/51G (time-delay, TCC curve, ≥ 2 cycles) = fuse-blowing or backup.
    Operate time alone distinguishes them when digital element bits are absent.

Fault permanence
    Temporary (tree contact, animal — burns clear on first or second reclose) vs
    permanent (broken conductor, equipment failure — results in lockout).

Fault location
    A V/I impedance estimate gives dispatchers a mileage hint even without a
    dedicated fault locator.  Accuracy is ±20–30% without line model compensation,
    which is enough to narrow a patrol zone on a feeder.

Digital channel conventions (common but not universal)
    50P / 50G   — instantaneous overcurrent pickup
    51P / 51G   — time-delayed overcurrent pickup
    79          — recloser in-sequence / reclose initiation
    52A         — breaker/recloser auxiliary, high = CLOSED
    52B         — breaker/recloser auxiliary, high = OPEN
    TRIP / TR   — trip coil energized
    LOCK / LKO  — lockout condition asserted
    CLOSE / CL  — close coil energized (some reclosers)
    27          — undervoltage (used for voltage recovery check)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import numpy as np

from data_model import EventRecord
from analysis import (
    compute_rms,
    detect_fault_inception,
    detect_digital_transitions,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RecloserShot:
    """One complete fault-operate-dead_time cycle in a recloser sequence."""
    shot_number: int
    fault_index: Optional[int]       # sample index of fault inception for this shot
    trip_index: int                  # sample index of trip assertion
    reclose_index: Optional[int]     # sample index of reclose (None = lockout)
    element: str                     # which element operated: '50P', '51P', '50G', etc. or ''
    operate_time_ms: Optional[float] # fault inception → trip, ms
    dead_time_ms: Optional[float]    # trip → reclose, ms (None = lockout)
    shot_type: str                   # 'FAST', 'SLOW', or 'UNKNOWN'
    outcome: str                     # 'RECLOSED', 'LOCKOUT', 'LAST' (no reclose observed yet)

    # Set after analysing what happened after the reclose
    post_reclose_fault: bool = False  # True = reclose into a still-present fault
    successful: bool = False          # True = reclose cleared, feeder stayed up


@dataclass
class RecloserSequence:
    """Full multi-shot sequence for one COMTRADE record."""
    shots: List[RecloserShot] = field(default_factory=list)
    total_shots: int = 0
    locked_out: bool = False
    fault_type: str = "UNKNOWN"      # temporary / permanent / indeterminate
    lockout_index: Optional[int] = None

    @property
    def final_outcome(self) -> str:
        if self.locked_out:
            return "LOCKED OUT (permanent fault)"
        if self.total_shots == 0:
            return "NO OPERATION DETECTED"
        last = self.shots[-1]
        if last.outcome == "RECLOSED" and not last.post_reclose_fault:
            return f"SUCCESSFUL RECLOSE after {self.total_shots} shot(s) (temporary fault)"
        return f"INDETERMINATE ({self.total_shots} shot(s))"


# ---------------------------------------------------------------------------
# Core recloser sequence detector
# ---------------------------------------------------------------------------

FAST_THRESHOLD_CYCLES = 2.0   # operate times ≤ this many cycles → FAST

def detect_reclose_sequence(
    record: EventRecord,
    trip_channels: Optional[List[str]] = None,
    breaker_52a_channel: Optional[str] = None,
    lockout_channels: Optional[List[str]] = None,
    fast_threshold_cycles: float = FAST_THRESHOLD_CYCLES,
) -> RecloserSequence:
    """
    Identify all trip-reclose shots in a COMTRADE record.

    Strategy
    --------
    1. Locate every rising edge on any TRIP channel → candidate trip times.
    2. Locate every falling edge on the same TRIP channels → candidate reclose times
       (the relay de-energises the trip coil after the breaker opens and before reclose).
    3. OR: if a 52A channel is present, use its falling edges as trip completions
       and rising edges as reclose completions.  52A is more reliable because it
       directly measures breaker state rather than trip coil state.
    4. Pair trips with the next reclose to form shots.
    5. Estimate fault inception for each shot by scanning backward from each trip.
    6. Classify shot type (FAST/SLOW) from operate time.
    7. Determine lockout from a dedicated LOCK channel or from absence of reclose
       after the final trip.

    Parameters
    ----------
    breaker_52a_channel
        Name of the 52A auxiliary contact channel (high = breaker CLOSED).
        When provided, breaker open/close events are read from this channel
        instead of inferring from TRIP transitions.
    lockout_channels
        Channel name(s) whose assertion indicates lockout.
    fast_threshold_cycles
        Operate times ≤ this × one cycle period are classified FAST.
    """
    seq = RecloserSequence()
    spc = record.samples_per_cycle()
    sr  = record.sample_rate

    # ── Step 1: gather trip events ────────────────────────────────────────────
    if trip_channels is None:
        trip_channels = _auto_trip_channels(record)

    # Auto-detect 52A channel if not specified
    if breaker_52a_channel is None:
        for ch in record.digital_channels:
            if ch.upper() in ("52A", "CB_52A", "AUX_A") or "52A" in ch.upper():
                breaker_52a_channel = ch
                break

    trip_edges: List[int] = []
    for ch in trip_channels:
        trans = detect_digital_transitions(record, ch)
        trip_edges.extend(trans["rising"])
    trip_edges = sorted(set(trip_edges))

    if not trip_edges:
        return seq  # no operations found

    # ── Step 2: breaker-open events ───────────────────────────────────────────
    # Prefer 52A (falling edge = breaker opened).
    # Fall back to inferring from current going below threshold.
    if breaker_52a_channel and breaker_52a_channel in record.digital_channels:
        trans_52a = detect_digital_transitions(record, breaker_52a_channel)
        breaker_open_edges  = sorted(trans_52a["falling"])
        breaker_close_edges = sorted(trans_52a["rising"])
    else:
        # Infer from current dropping to near zero after each trip
        breaker_open_edges  = []
        breaker_close_edges = []

        # Find TRIP falling edges (coil de-energised = breaker now confirmed open)
        for ch in trip_channels:
            trans = detect_digital_transitions(record, ch)
            breaker_open_edges.extend(trans["falling"])
        breaker_open_edges = sorted(set(breaker_open_edges))

        # Reclose = next significant current return after each open
        breaker_close_edges = _infer_reclose_from_current(record, breaker_open_edges)

    # ── Step 3: lockout detection ─────────────────────────────────────────────
    if lockout_channels is None:
        lockout_channels = _auto_lockout_channels(record)

    lockout_idx = _find_lockout_index(record, lockout_channels)
    if lockout_idx is not None:
        seq.locked_out     = True
        seq.lockout_index  = lockout_idx

    # ── Step 4: pair trips → reclose to form shots ───────────────────────────
    shots: List[RecloserShot] = []
    used_closes: set = set()

    for shot_num, trip_idx in enumerate(trip_edges, start=1):
        # Find the next breaker-open confirmation after this trip
        open_idx = _next_after(breaker_open_edges, trip_idx)

        # Find the next reclose after the open
        close_idx: Optional[int] = None
        for ci in breaker_close_edges:
            if ci not in used_closes and ci > (open_idx if open_idx else trip_idx):
                close_idx = ci
                used_closes.add(ci)
                break

        # Locate fault inception for this shot (scan backward from trip)
        fault_idx = _find_shot_fault_inception(
            record, trip_idx, shot_number=shot_num, prev_close_idx=shots[-1].reclose_index if shots else None
        )

        # Operate time
        operate_ms: Optional[float] = None
        if fault_idx is not None:
            operate_ms = (record.time[trip_idx] - record.time[fault_idx]) * 1000

        # Dead time = trip assertion → breaker reclose
        # Using trip_idx as the open start because breaker opens within 2–3 cycles
        # of the trip coil energising — this is the conventional "open interval".
        dead_ms: Optional[float] = None
        if close_idx is not None:
            dead_ms = (record.time[close_idx] - record.time[trip_idx]) * 1000

        # Shot classification
        shot_type = classify_shot_type(operate_ms, spc, sr, fast_threshold_cycles)

        # Element (best guess from operate time or from digital channels)
        element = _identify_element(record, trip_idx, operate_ms, spc, sr)

        # Outcome
        if seq.locked_out and (lockout_idx is not None) and trip_idx < lockout_idx:
            outcome = "RECLOSED" if close_idx is not None else "LOCKOUT"
        elif seq.locked_out and shot_num == len(trip_edges):
            outcome = "LOCKOUT"
        elif close_idx is not None:
            outcome = "RECLOSED"
        else:
            outcome = "LAST"  # no reclose observed — either lockout or end of record

        shot = RecloserShot(
            shot_number=shot_num,
            fault_index=fault_idx,
            trip_index=trip_idx,
            reclose_index=close_idx,
            element=element,
            operate_time_ms=operate_ms,
            dead_time_ms=dead_ms,
            shot_type=shot_type,
            outcome=outcome,
        )
        shots.append(shot)

    # ── Step 5: mark post-reclose fault / successful ──────────────────────────
    for i, shot in enumerate(shots):
        if shot.outcome == "RECLOSED" and shot.reclose_index is not None:
            next_shot_idx = shots[i + 1].fault_index if i + 1 < len(shots) else None
            if next_shot_idx is not None:
                gap_ms = (record.time[next_shot_idx] - record.time[shot.reclose_index]) * 1000
                shot.post_reclose_fault = True
                shot.successful = False
            else:
                shot.successful = True
        elif shot.outcome == "LAST":
            shot.successful = not seq.locked_out

    # ── Step 6: fault permanence ──────────────────────────────────────────────
    if seq.locked_out:
        seq.fault_type = "PERMANENT"
    elif shots and shots[-1].successful:
        seq.fault_type = "TEMPORARY"
    else:
        seq.fault_type = "INDETERMINATE"

    seq.shots       = shots
    seq.total_shots = len(shots)
    return seq


# ---------------------------------------------------------------------------
# Shot classification
# ---------------------------------------------------------------------------

def classify_shot_type(
    operate_ms: Optional[float],
    samples_per_cycle: int,
    sample_rate: float,
    fast_threshold_cycles: float = FAST_THRESHOLD_CYCLES,
) -> str:
    """
    Classify a relay operation as FAST (instantaneous) or SLOW (time-delay).

    Fast operations are those where operate_time ≤ fast_threshold_cycles × one cycle.
    On a 60 Hz system with a 2-cycle threshold, that is ≤ 33 ms.

    This is a heuristic: instantaneous elements (50P/50G) typically operate in
    0.5–2 cycles; time-delay elements (51P/51G) operate on their TCC curve
    which is rarely faster than 2–3 cycles at maximum current.
    """
    if operate_ms is None:
        return "UNKNOWN"
    if sample_rate > 0:
        one_cycle_ms = (samples_per_cycle / sample_rate) * 1000
    else:
        one_cycle_ms = 1000 / 60
    threshold_ms = fast_threshold_cycles * one_cycle_ms
    return "FAST" if operate_ms <= threshold_ms else "SLOW"


# ---------------------------------------------------------------------------
# Fault location estimate
# ---------------------------------------------------------------------------

def estimate_fault_location(
    record: EventRecord,
    feeder_impedance_ohm_per_mile: float,
    fault_index: Optional[int] = None,
    source_voltage_ll_kv: Optional[float] = None,
) -> Optional[Dict]:
    """
    Estimate fault distance from the substation using a simple impedance ratio.

    Method: |Z_fault| = |V_prefault| / |I_fault| (one-cycle RMS values)
    Distance = |Z_fault| / Z_line  (in miles, single-phase equivalent)

    Accuracy: ±20–30% without line model compensation for load, arc resistance,
    or mutual coupling.  Use as a patrol dispatch guide, not as a precise locator.

    Parameters
    ----------
    feeder_impedance_ohm_per_mile
        Positive-sequence impedance magnitude of the feeder in Ω/mile.
        Typical distribution: 0.3–0.6 Ω/mile (overhead) or 0.08–0.15 Ω/mile (cable).
    fault_index
        Sample index of fault inception.  Auto-detected if None.
    source_voltage_ll_kv
        Line-to-line source voltage in kV for sanity check.  Optional.

    Returns dict with keys: estimated_miles, z_fault_ohm, ia_rms_a, va_rms_v
    or None if the calculation cannot be performed.
    """
    if fault_index is None:
        fault_index = detect_fault_inception(record)
    if fault_index is None:
        return None

    spc = record.samples_per_cycle()

    # Find the faulted phase current channel (highest post-fault RMS)
    ia = _find_channel(record, ("IA", "Ia", "I_A"))
    ib = _find_channel(record, ("IB", "Ib", "I_B"))
    ic = _find_channel(record, ("IC", "Ic", "I_C"))
    va = _find_channel(record, ("VAN", "VA", "V_A", "Va"))
    vb = _find_channel(record, ("VBN", "VB", "V_B", "Vb"))
    vc = _find_channel(record, ("VCN", "VC", "V_C", "Vc"))

    if ia is None:
        return None

    # One-cycle RMS at fault inception
    end = min(fault_index + spc, len(record.time))

    def _rms(arr):
        if arr is None or end <= fault_index:
            return 0.0
        return float(np.sqrt(np.mean(arr[fault_index:end] ** 2)))

    ia_rms = _rms(ia)
    ib_rms = _rms(ib)
    ic_rms = _rms(ic)

    # Pick the faulted phase (highest fault current)
    phase_currents = [("A", ia_rms, va), ("B", ib_rms, vb), ("C", ic_rms, vc)]
    phase_currents.sort(key=lambda x: x[1], reverse=True)
    faulted_phase, i_fault_rms, v_fault = phase_currents[0]

    if i_fault_rms < 1.0:
        return None  # no meaningful fault current

    # Pre-fault voltage on faulted phase
    pre_start = max(0, fault_index - spc)
    if v_fault is not None and fault_index > 0:
        v_prefault_rms = float(np.sqrt(np.mean(v_fault[pre_start:fault_index] ** 2)))
    elif source_voltage_ll_kv is not None:
        v_prefault_rms = source_voltage_ll_kv * 1000 / np.sqrt(3)  # L-N
    else:
        v_prefault_rms = None

    # Impedance magnitude
    v_fault_rms = _rms(v_fault) if v_fault is not None else 0.0

    if v_prefault_rms and v_prefault_rms > 0:
        z_source_ohm = v_prefault_rms / i_fault_rms
        # Fault voltage drop = V_prefault - V_fault → Z_line_to_fault ≈ V_drop / I_fault
        # If we have actual fault voltage: Z_fault = V_at_fault / I_fault
        if v_fault_rms > 0:
            z_fault_ohm = v_fault_rms / i_fault_rms
        else:
            z_fault_ohm = z_source_ohm
    else:
        z_fault_ohm = None

    distance_miles: Optional[float] = None
    if z_fault_ohm is not None and feeder_impedance_ohm_per_mile > 0:
        distance_miles = z_fault_ohm / feeder_impedance_ohm_per_mile

    return {
        "faulted_phase":          faulted_phase,
        "estimated_miles":        round(distance_miles, 2) if distance_miles is not None else None,
        "z_fault_ohm":            round(z_fault_ohm, 3) if z_fault_ohm is not None else None,
        "i_fault_rms_a":          round(i_fault_rms, 1),
        "v_fault_rms_v":          round(v_fault_rms, 1),
        "v_prefault_rms_v":       round(v_prefault_rms, 1) if v_prefault_rms else None,
        "feeder_z_ohm_per_mile":  feeder_impedance_ohm_per_mile,
        "accuracy_note":          "±20–30% without arc resistance / load compensation",
    }


# ---------------------------------------------------------------------------
# Voltage recovery check (post-reclose)
# ---------------------------------------------------------------------------

def detect_voltage_recovery(
    record: EventRecord,
    reclose_index: int,
    recovery_threshold_pct: float = 90.0,
    window_cycles: float = 3.0,
) -> Dict:
    """
    Verify that voltage recovered to near-normal after a reclose.

    After a successful reclose, voltage should return to ≥ recovery_threshold_pct
    of the pre-fault level within a few cycles.  If voltage stays depressed, the
    recloser closed into a still-present (permanent) fault.

    Returns dict with: recovered (bool), post_reclose_pct, pre_fault_pct,
                        recovery_time_ms (None if not recovered within window)
    """
    spc = record.samples_per_cycle()
    window = int(window_cycles * spc)

    voltage_chs = [
        n for n in record.analog_channels
        if any(k in n.upper() for k in ("VAN", "VA", "VBN", "VB", "VCN", "VC", "VOLT"))
    ]

    if not voltage_chs:
        return {"recovered": None, "error": "No voltage channels found"}

    # Use the first voltage channel as reference
    v = record.analog_channels[voltage_chs[0]]
    n = len(v)

    # Pre-fault reference (one cycle before trigger)
    trig = record.trigger_index
    pre_start = max(0, trig - spc)
    v_pre_rms = float(np.sqrt(np.mean(v[pre_start:trig] ** 2))) if trig > pre_start else 0

    if v_pre_rms < 1.0:
        return {"recovered": None, "error": "Pre-fault voltage too small to reference"}

    # Post-reclose window
    post_end = min(reclose_index + window, n)
    if post_end <= reclose_index:
        return {"recovered": None, "error": "Not enough post-reclose samples"}

    # Scan cycle by cycle for recovery
    recovery_time_ms: Optional[float] = None
    final_pct: float = 0.0

    for i in range(reclose_index, post_end - spc, spc // 4):
        seg = v[i : i + spc]
        v_rms = float(np.sqrt(np.mean(seg ** 2)))
        pct = v_rms / v_pre_rms * 100
        if pct >= recovery_threshold_pct and recovery_time_ms is None:
            recovery_time_ms = (record.time[i] - record.time[reclose_index]) * 1000
        final_pct = pct

    recovered = recovery_time_ms is not None

    return {
        "recovered":          recovered,
        "post_reclose_pct":   round(final_pct, 1),
        "pre_fault_rms_v":    round(v_pre_rms, 1),
        "recovery_threshold_pct": recovery_threshold_pct,
        "recovery_time_ms":   round(recovery_time_ms, 1) if recovery_time_ms is not None else None,
        "channel":            voltage_chs[0],
    }


# ---------------------------------------------------------------------------
# High-impedance fault check
# ---------------------------------------------------------------------------

def screen_high_impedance_fault(
    record: EventRecord,
    fault_index: Optional[int] = None,
    hif_threshold_a: float = 50.0,
    nominal_load_a: float = 0.0,
) -> Dict:
    """
    Flag a potential high-impedance fault (HIF).

    HIFs are caused by a downed conductor on a resistive surface (dry grass,
    asphalt, sand).  They produce < 50 A of fault current — often below standard
    OC relay pickup — but are hazardous to the public.

    This is a screening flag only, not a definitive HIF detector.  True HIF
    detection requires waveform distortion analysis (odd harmonics, arcing
    signatures) beyond the scope of this tool.

    Returns dict: hif_suspect (bool), delta_current_a, threshold_a, note
    """
    if fault_index is None:
        fault_index = detect_fault_inception(record)

    spc = record.samples_per_cycle()

    current_chs = [
        n for n in record.analog_channels
        if any(k in n.upper() for k in ("IA", "IB", "IC", "IN"))
    ]
    if not current_chs or fault_index is None:
        return {"hif_suspect": False, "note": "Cannot evaluate — no current data or fault index"}

    # Pre-fault RMS
    pre_end   = max(1, fault_index)
    pre_start = max(0, fault_index - spc * 3)
    post_end  = min(fault_index + spc * 2, len(record.time))

    max_delta = 0.0
    for ch in current_chs:
        sig = record.analog_channels[ch]
        pre_rms  = float(np.sqrt(np.mean(sig[pre_start:pre_end] ** 2)))
        post_rms = float(np.sqrt(np.mean(sig[fault_index:post_end] ** 2)))
        max_delta = max(max_delta, post_rms - pre_rms)

    threshold = max(hif_threshold_a, nominal_load_a * 0.1)
    hif_suspect = 0 < max_delta < threshold

    return {
        "hif_suspect":     hif_suspect,
        "delta_current_a": round(max_delta, 1),
        "threshold_a":     round(threshold, 1),
        "note": (
            "Current increase below typical overcurrent threshold — "
            "possible high-impedance fault (downed conductor, dry soil). "
            "Standard overcurrent elements may not operate for this event."
        ) if hif_suspect else "Current increase above HIF threshold — standard fault.",
    }


# ---------------------------------------------------------------------------
# Full feeder event summary
# ---------------------------------------------------------------------------

def compute_feeder_summary(
    record: EventRecord,
    feeder_impedance_ohm_per_mile: Optional[float] = None,
    source_voltage_ll_kv: Optional[float] = None,
    hif_threshold_a: float = 50.0,
) -> Dict:
    """
    High-level feeder event summary combining recloser and location analysis.

    Returns a dict suitable for both console output and report embedding.
    """
    fault_idx = detect_fault_inception(record)

    reclose_seq = detect_reclose_sequence(record)

    loc_result: Optional[Dict] = None
    if feeder_impedance_ohm_per_mile is not None:
        loc_result = estimate_fault_location(
            record,
            feeder_impedance_ohm_per_mile,
            fault_index=fault_idx,
            source_voltage_ll_kv=source_voltage_ll_kv,
        )

    hif_result = screen_high_impedance_fault(record, fault_index=fault_idx,
                                             hif_threshold_a=hif_threshold_a)

    # Voltage recovery for each reclose
    voltage_recoveries: List[Dict] = []
    for shot in reclose_seq.shots:
        if shot.reclose_index is not None:
            vr = detect_voltage_recovery(record, shot.reclose_index)
            vr["shot_number"] = shot.shot_number
            voltage_recoveries.append(vr)

    return {
        "reclose_sequence":   reclose_seq,
        "fault_location":     loc_result,
        "hif_screen":         hif_result,
        "voltage_recoveries": voltage_recoveries,
    }


# ---------------------------------------------------------------------------
# Console printer
# ---------------------------------------------------------------------------

def print_feeder_summary(feeder: Dict) -> None:
    """Print feeder/recloser analysis to stdout."""
    sep = "─" * 62
    seq = feeder["reclose_sequence"]

    print(f"\n{sep}")
    print("  RECLOSER / FEEDER RELAY ANALYSIS")
    print(f"{sep}")
    print(f"  Operations detected : {seq.total_shots}")
    print(f"  Outcome             : {seq.final_outcome}")
    print(f"  Fault permanence    : {seq.fault_type}")

    if seq.shots:
        print()
        print(f"  {'Shot':>4}  {'Element':>8}  {'Type':>6}  {'Operate(ms)':>12}  "
              f"{'Dead time(ms)':>14}  Outcome")
        print(f"  {'─'*4}  {'─'*8}  {'─'*6}  {'─'*12}  {'─'*14}  {'─'*12}")
        for s in seq.shots:
            op_ms  = f"{s.operate_time_ms:.1f}" if s.operate_time_ms is not None else "N/A"
            dead   = f"{s.dead_time_ms:.0f}"    if s.dead_time_ms is not None    else "N/A"
            print(f"  {s.shot_number:>4}  {s.element:>8}  {s.shot_type:>6}  "
                  f"{op_ms:>12}  {dead:>14}  {s.outcome}")

    loc = feeder.get("fault_location")
    if loc and loc.get("estimated_miles") is not None:
        print()
        print(f"  Fault Location Estimate")
        print(f"    Phase      : {loc['faulted_phase']}")
        print(f"    Distance   : ~{loc['estimated_miles']:.1f} miles from substation")
        print(f"    I fault    : {loc['i_fault_rms_a']:.0f} A RMS")
        print(f"    Z fault    : {loc['z_fault_ohm']:.2f} Ω  ({loc['accuracy_note']})")

    hif = feeder.get("hif_screen", {})
    if hif.get("hif_suspect"):
        print()
        print(f"  ⚠  HIF SCREEN: {hif['note']}")
        print(f"     ΔI = {hif['delta_current_a']:.1f} A  (threshold {hif['threshold_a']:.1f} A)")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _auto_trip_channels(record: EventRecord) -> List[str]:
    return [
        n for n in record.digital_channels
        if any(k in n.upper() for k in ("TRIP", "TR", "T1", "T2"))
    ]


def _auto_lockout_channels(record: EventRecord) -> List[str]:
    return [
        n for n in record.digital_channels
        if any(k in n.upper() for k in ("LOCK", "LKO", "LO", "86"))
    ]


def _find_lockout_index(record: EventRecord, channels: List[str]) -> Optional[int]:
    earliest: Optional[int] = None
    for ch in channels:
        if ch not in record.digital_channels:
            continue
        trans = detect_digital_transitions(record, ch)
        for idx in trans["rising"]:
            if earliest is None or idx < earliest:
                earliest = idx
    return earliest


def _next_after(edges: List[int], after_idx: int) -> Optional[int]:
    """
    Return first edge index that is >= after_idx.

    >= (not >) because the breaker-open event (52A falling) and the trip
    assertion often occur at the same sample in the DAT file — both are
    commanded by the same relay output at the same scan point.
    """
    for e in edges:
        if e >= after_idx:
            return e
    return None


def _infer_reclose_from_current(
    record: EventRecord,
    open_edges: List[int],
) -> List[int]:
    """
    Infer reclose times by finding where current returns after a trip.

    Looks for the first sample after each open edge where any current channel
    exceeds 5% of its pre-event RMS — a simple proxy for breaker closing.
    """
    current_chs = [
        n for n in record.analog_channels
        if any(k in n.upper() for k in ("IA", "IB", "IC"))
    ]
    if not current_chs:
        return []

    # Build a composite current magnitude
    composite = np.zeros(len(record.time))
    for ch in current_chs:
        composite = np.maximum(composite, np.abs(record.analog_channels[ch]))

    spc     = record.samples_per_cycle()
    results = []

    for open_idx in open_edges:
        # Threshold = 5% of pre-event peak
        pre = composite[max(0, record.trigger_index - spc) : record.trigger_index]
        threshold = float(np.max(pre)) * 0.05 if len(pre) > 0 else 1.0

        # Scan forward from open, starting after a half-cycle gap
        scan_start = min(open_idx + spc // 2, len(record.time) - 1)
        for idx in range(scan_start, len(record.time)):
            if composite[idx] >= threshold:
                results.append(idx)
                break

    return results


def _find_shot_fault_inception(
    record: EventRecord,
    trip_idx: int,
    shot_number: int,
    prev_close_idx: Optional[int],
) -> Optional[int]:
    """
    Find fault inception for one specific shot.

    Shot 1 : scan backward from trigger using the RMS-threshold approach.
    Shots 2+: the circuit is open (current = 0) just before reclose, so the
              "inception" is when current first appears after the reclose —
              i.e., the reclose itself when the fault is permanent, or the
              fault inception after a brief healthy interval for temporary faults.
              We detect this by scanning forward from prev_close_idx for the
              first sample where current exceeds 5% of the first shot's peak.
    """
    spc = record.samples_per_cycle()
    sr  = record.sample_rate

    current_chs = [
        n for n in record.analog_channels
        if any(k in n.upper() for k in ("IA", "IB", "IC"))
    ]
    if not current_chs:
        return None

    # Build composite envelope
    envelope = np.zeros(len(record.time))
    for ch in current_chs:
        rms = compute_rms(record.analog_channels[ch], spc)
        envelope = np.maximum(envelope, np.nan_to_num(rms, nan=0.0))

    if shot_number == 1:
        # Standard approach: scan from a window before the trigger
        scan_start = max(0, record.trigger_index - int(sr * 0.15))
        pre_start  = max(0, scan_start - spc * 3)
        pre_end    = max(pre_start + 1, scan_start - spc)
        pre_rms    = float(np.median(envelope[pre_start:pre_end])) if pre_end > pre_start else 0.0
        threshold  = pre_rms * 2.0 if pre_rms > 1.0 else float(np.max(envelope[:record.trigger_index + spc]) * 0.15)
        if threshold <= 0:
            return None
        for i in range(scan_start, min(trip_idx + spc, len(envelope))):
            if envelope[i] >= threshold:
                return i
        return None

    else:
        # Shots 2+: scan forward from just before the reclose for any current.
        # Threshold = 10% of peak envelope over the whole record (catches both
        # healthy-load returns and immediate fault current appearances).
        if prev_close_idx is None:
            return None
        peak_ever = float(np.max(envelope))
        threshold = peak_ever * 0.10
        scan_start = max(0, prev_close_idx - spc // 2)
        for i in range(scan_start, min(trip_idx + spc, len(envelope))):
            if envelope[i] >= threshold:
                return i
        return None


def _identify_element(
    record: EventRecord,
    trip_idx: int,
    operate_ms: Optional[float],
    spc: int,
    sr: float,
) -> str:
    """
    Identify which protection element operated for a given trip.

    Preference order:
    1. Digital channel assertion just before trip (50P, 51P, 50G, 51G, 67, etc.)
    2. Fall back to operate time classification (< 2 cycles = 50; else 51)
    """
    # Check which protection pick-up digital channels were asserted before the trip
    element_keywords = {
        "50P": ("50P",), "50G": ("50G", "50N"), "50Q": ("50Q",),
        "51P": ("51P",), "51G": ("51G", "51N"), "51Q": ("51Q",),
        "67P": ("67P",), "67G": ("67G",),
        "27":  ("27",),  "59": ("59",),
    }
    look_back = int(sr * 0.1)   # 100 ms look-back
    window_start = max(0, trip_idx - look_back)

    for element, keywords in element_keywords.items():
        for ch_name in record.digital_channels:
            if any(k in ch_name.upper() for k in keywords):
                ch_data = record.digital_channels[ch_name]
                if any(ch_data[window_start : trip_idx + 1]):
                    return element

    # Fall back to timing
    if operate_ms is not None:
        one_cycle_ms = (spc / sr) * 1000 if sr > 0 else 1000 / 60
        return "50P/50G" if operate_ms <= 2 * one_cycle_ms else "51P/51G"

    return ""


def _find_channel(record: EventRecord, candidates: tuple) -> Optional[np.ndarray]:
    ch_upper = {k.upper(): v for k, v in record.analog_channels.items()}
    for name in candidates:
        if name.upper() in ch_upper:
            return ch_upper[name.upper()]
    return None
