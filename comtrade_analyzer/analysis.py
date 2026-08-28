"""
analysis.py — Waveform and event analysis for COMTRADE relay data.

All functions operate on EventRecord objects or plain numpy arrays.
Units follow the channel's engineering-unit scaling from the CFG file.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple
import numpy as np

from .data_model import EventRecord


# ---------------------------------------------------------------------------
# RMS / envelope
# ---------------------------------------------------------------------------

def compute_rms(signal: np.ndarray, window_samples: int) -> np.ndarray:
    """
    Sliding-window RMS over a 1-D signal.

    Returns an array of the same length as signal.  The first
    (window_samples - 1) values are NaN because the window isn't full yet.

    Uses cumulative-sum trick for O(n) performance.
    """
    n = len(signal)
    out = np.full(n, np.nan)
    if window_samples < 1 or n < window_samples:
        return out

    sq = signal ** 2
    cs = np.zeros(n + 1)
    np.cumsum(sq, out=cs[1:])
    window_sum = cs[window_samples:] - cs[:n - window_samples + 1]
    out[window_samples - 1:] = np.sqrt(window_sum / window_samples)
    return out


def compute_peak(signal: np.ndarray, window_samples: int) -> np.ndarray:
    """
    Sliding-window peak (absolute maximum) over a signal.

    Returns NaN for the first (window_samples - 1) samples.
    """
    n = len(signal)
    out = np.full(n, np.nan)
    abs_sig = np.abs(signal)
    for i in range(window_samples - 1, n):
        out[i] = np.max(abs_sig[i - window_samples + 1 : i + 1])
    return out


# ---------------------------------------------------------------------------
# Fault inception detection
# ---------------------------------------------------------------------------

def detect_fault_inception(
    record: EventRecord,
    current_channels: Optional[List[str]] = None,
    threshold_multiplier: float = 2.0,
    pre_window_cycles: float = 3.0,
    search_window_s: float = 0.15,
) -> Optional[int]:
    """
    Detect the fault inception sample by looking for a sustained increase
    in per-phase current RMS relative to the pre-fault steady-state.

    Strategy:
    1. Auto-select current channels if not specified.
    2. Compute one-cycle sliding RMS for each channel; take point-wise max.
    3. Estimate pre-fault RMS from a quiet window several cycles before trigger.
    4. Scan backward from the trigger to find where RMS first exceeds
       threshold_multiplier × pre-fault RMS.

    Returns the sample index of fault inception, or None if undetectable.

    Parameters
    ----------
    threshold_multiplier : float
        Ratio of faulted to pre-fault RMS that signals fault inception.
        Typical values: 1.5–3.0.
    pre_window_cycles : float
        How many cycles before the trigger to use for pre-fault RMS baseline.
    search_window_s : float
        How far before the trigger (seconds) to search for fault inception.
    """
    if current_channels is None:
        current_channels = _auto_select_currents(record)

    if not current_channels:
        return None

    spc = record.samples_per_cycle()
    sr  = record.sample_rate
    trig = record.trigger_index

    # Compute envelope across all current channels
    envelope = np.zeros(len(record.time))
    for name in current_channels:
        sig = record.analog_channels.get(name)
        if sig is None:
            continue
        rms = compute_rms(sig, spc)
        rms = np.nan_to_num(rms, nan=0.0)
        envelope = np.maximum(envelope, rms)

    if envelope.max() == 0:
        return None

    # Pre-fault baseline: median RMS from 'pre_window_cycles' before trigger
    pre_start = max(0, trig - int(pre_window_cycles * spc))
    pre_end   = max(1, trig - spc)
    pre_rms   = float(np.median(envelope[pre_start:pre_end])) if pre_end > pre_start else 0.0
    threshold = pre_rms * threshold_multiplier

    if threshold <= 0:
        return None

    # Scan backward from trigger to find where RMS last crossed the threshold
    # going upward; i.e., the first sample in the forward direction where
    # the envelope stays above threshold.
    search_start = max(0, trig - int(search_window_s * sr))
    for i in range(search_start, min(trig + spc, len(envelope))):
        if envelope[i] >= threshold:
            return i

    return None


# ---------------------------------------------------------------------------
# Trip detection
# ---------------------------------------------------------------------------

def detect_trip_time(
    record: EventRecord,
    trip_channels: Optional[List[str]] = None,
) -> Optional[Tuple[int, str]]:
    """
    Find the first rising edge (0→1 transition) on any TRIP digital channel.

    Returns (sample_index, channel_name) of the earliest trip, or None.

    trip_channels defaults to any digital channel whose name contains
    'TRIP' or starts with 'TR'.
    """
    if trip_channels is None:
        trip_channels = [
            n for n in record.digital_channels
            if "TRIP" in n.upper() or n.upper().startswith("TR")
        ]

    earliest_idx: Optional[int] = None
    earliest_ch:  Optional[str] = None

    for name in trip_channels:
        sig = record.digital_channels.get(name)
        if sig is None:
            continue
        edges = np.where(np.diff(sig.astype(np.int16)) > 0)[0] + 1
        if len(edges) > 0:
            if earliest_idx is None or int(edges[0]) < earliest_idx:
                earliest_idx = int(edges[0])
                earliest_ch  = name

    return (earliest_idx, earliest_ch) if earliest_idx is not None else None


def detect_digital_transitions(
    record: EventRecord,
    channel: str,
) -> Dict[str, list]:
    """
    Return rising and falling edge indices for a named digital channel.

    Returns {'rising': [...], 'falling': [...]} sample index lists.
    """
    sig = record.digital_channels.get(channel)
    if sig is None:
        return {"rising": [], "falling": []}

    diff = np.diff(sig.astype(np.int16))
    return {
        "rising":  list(np.where(diff > 0)[0] + 1),
        "falling": list(np.where(diff < 0)[0] + 1),
    }


# ---------------------------------------------------------------------------
# Symmetrical components
# ---------------------------------------------------------------------------

def compute_sequence_components(
    ia: np.ndarray,
    ib: np.ndarray,
    ic: np.ndarray,
    window_samples: int,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Compute symmetrical sequence component magnitudes using a sliding DFT phasor.

    For each sample position, a one-cycle DFT phasor is computed for each phase.
    The Fortescue transformation is then applied.

    Returns (|I0|, |I1|, |I2|) arrays — all the same length as the inputs,
    with the first (window_samples - 1) elements set to NaN.

    This is the standard relay-grade approach (c.f. EMTP / SEL literature).
    """
    n = len(ia)
    i0_mag = np.full(n, np.nan)
    i1_mag = np.full(n, np.nan)
    i2_mag = np.full(n, np.nan)

    a  = np.exp(1j * 2 * np.pi / 3)
    a2 = a ** 2

    # Pre-build DFT kernel for the fundamental
    k_range = np.arange(window_samples)
    kernel  = np.exp(-1j * 2 * np.pi * k_range / window_samples)

    for idx in range(window_samples - 1, n):
        sl = slice(idx - window_samples + 1, idx + 1)
        Ia = (2 / window_samples) * np.dot(ia[sl], kernel)
        Ib = (2 / window_samples) * np.dot(ib[sl], kernel)
        Ic = (2 / window_samples) * np.dot(ic[sl], kernel)

        I0 = (Ia + Ib + Ic) / 3
        I1 = (Ia + a * Ib + a2 * Ic) / 3
        I2 = (Ia + a2 * Ib + a * Ic) / 3

        i0_mag[idx] = abs(I0)
        i1_mag[idx] = abs(I1)
        i2_mag[idx] = abs(I2)

    return i0_mag, i1_mag, i2_mag


# ---------------------------------------------------------------------------
# Fault classification
# ---------------------------------------------------------------------------

def classify_fault(
    record: EventRecord,
    fault_index: Optional[int] = None,
) -> str:
    """
    Basic fault-type classification based on post-fault phase current magnitudes.

    Classification rules (simplified — not a replacement for a relay algorithm):
    - SLG  : one phase >> other two
    - LL   : two phases elevated, third near pre-fault level, |I0| small
    - LLG  : two phases elevated + measurable ground (I0) contribution
    - 3PH  : all three phases elevated approximately equally, voltage collapsed
    - LOAD : all three phases elevated approximately equally, voltage intact —
             a balanced load step, not a fault
    - UNKNOWN : insufficient channels or no clear pattern

    Returns one of: 'SLG', 'LL', 'LLG', '3PH', 'LOAD', 'UNKNOWN'
    """
    ia = _find_channel(record, ("IA", "Ia", "I_A", "I-A", "IA1"))
    ib = _find_channel(record, ("IB", "Ib", "I_B", "I-B", "IB1"))
    ic = _find_channel(record, ("IC", "Ic", "I_C", "I-C", "IC1"))

    if ia is None or ib is None or ic is None:
        return "UNKNOWN"
    if fault_index is None:
        return "UNKNOWN"

    spc = record.samples_per_cycle()
    end = min(fault_index + spc, len(ia))
    if end <= fault_index:
        return "UNKNOWN"

    ia_rms = float(np.sqrt(np.mean(ia[fault_index:end] ** 2)))
    ib_rms = float(np.sqrt(np.mean(ib[fault_index:end] ** 2)))
    ic_rms = float(np.sqrt(np.mean(ic[fault_index:end] ** 2)))

    currents = sorted([ia_rms, ib_rms, ic_rms], reverse=True)
    top, mid, bot = currents

    if top < 1e-6:
        return "UNKNOWN"

    ratio_bot = bot / top
    ratio_mid = mid / top

    if ratio_bot < 0.15:
        if ratio_mid < 0.15:
            return "SLG"
        else:
            # Check for zero-sequence contribution
            i0, _, _ = compute_sequence_components(ia, ib, ic, spc)
            i0_post = float(np.nanmedian(i0[fault_index:end]))
            return "LLG" if i0_post > 0.1 * top else "LL"

    if ratio_bot > 0.7:
        # Balanced rise on all three phases. That is a three-phase fault only
        # if the voltage went with it; if it held up, this is load being
        # switched on — cold-load pickup when a tie closes onto a section
        # looks exactly like a 3PH fault in current alone. Voltage is the only
        # thing that separates them, so with no voltage channels we cannot
        # tell, and the fault reading stands.
        if _voltage_held_up(record, fault_index, spc):
            return "LOAD"
        return "3PH"

    return "LLG"


# A three-phase fault drags every phase voltage down hard — even a remote one
# sags well past this. A load step barely moves it. Anything above this ratio
# of pre-fault voltage is load, not a fault.
_LOAD_STEP_V_RATIO = 0.85


def _voltage_held_up(record: EventRecord, fault_index: int, spc: int) -> bool:
    """
    True when every phase voltage during the fault window is still close to its
    pre-fault level. False when voltage collapsed, or when there is nothing to
    measure — an unknown is not evidence of a load step.
    """
    va = _find_channel(record, ("VAN", "VA", "Van", "V_A", "VA1"))
    vb = _find_channel(record, ("VBN", "VB", "Vbn", "V_B", "VB1"))
    vc = _find_channel(record, ("VCN", "VC", "Vcn", "V_C", "VC1"))
    if va is None or vb is None or vc is None:
        return False

    pre_start = max(0, fault_index - spc)
    if fault_index - pre_start < spc // 2:
        return False        # not enough pre-fault record to compare against

    end = min(fault_index + spc, len(va))
    for v in (va, vb, vc):
        pre = float(np.sqrt(np.mean(v[pre_start:fault_index] ** 2)))
        post = float(np.sqrt(np.mean(v[fault_index:end] ** 2)))
        if pre < 1e-6 or post < _LOAD_STEP_V_RATIO * pre:
            return False
    return True


# ---------------------------------------------------------------------------
# DC offset estimation
# ---------------------------------------------------------------------------

def estimate_dc_offset(
    signal: np.ndarray,
    window_samples: int,
) -> Tuple[float, float]:
    """
    Estimate DC offset magnitude and approximate decay time constant (tau).

    Method: the DC offset is the mean of the first post-fault cycle.
    Tau is estimated by fitting a simple two-half-cycle exponential.

    Returns (dc_amplitude_peak_units, tau_seconds_or_0_if_undetermined).
    The tau estimate is rough; use it only as a qualitative indicator.
    """
    if len(signal) < window_samples:
        return 0.0, 0.0

    first_cycle = signal[:window_samples]
    dc = float(np.mean(first_cycle))

    half = window_samples // 2
    m1 = float(np.mean(np.abs(first_cycle[:half])))
    m2 = float(np.mean(np.abs(first_cycle[half:])))

    if m2 > 1e-9 and m1 > 1e-9 and (m1 / m2) > 1.0:
        tau = (half / (window_samples / 2)) / np.log(m1 / m2 + 1e-12)
    else:
        tau = 0.0

    return dc, float(tau)


# ---------------------------------------------------------------------------
# High-level summary
# ---------------------------------------------------------------------------

def compute_event_summary(record: EventRecord) -> dict:
    """
    Compute a comprehensive summary dict for one EventRecord.

    Keys returned:
      station, device_id, sample_rate, duration_s, n_analog, n_digital,
      analog_channels, digital_channels, trigger_time, fault_inception_s,
      trip_time_s, trip_channel, fault_duration_s, trip_delay_ms,
      fault_type, max_currents, max_voltages
    """
    summary: dict = {
        "station":         record.metadata.get("station_name", ""),
        "device_id":       record.metadata.get("rec_dev_id", ""),
        "sample_rate":     record.sample_rate,
        "duration_s":      record.duration_s(),
        "n_analog":        len(record.analog_channels),
        "n_digital":       len(record.digital_channels),
        "analog_channels": list(record.analog_channels.keys()),
        "digital_channels": list(record.digital_channels.keys()),
        "trigger_time":    record.trigger_time,
        "fault_type":      "UNKNOWN",
        "fault_inception_s": None,
        "trip_time_s":     None,
        "trip_channel":    None,
        "fault_duration_s": None,
        "trip_delay_ms":   None,
        "max_currents":    {},
        "max_voltages":    {},
    }

    # Peak absolute values per channel
    for name, data in record.analog_channels.items():
        peak = float(np.max(np.abs(data)))
        units = record.analog_info[name].units.upper()
        if any(u in units for u in ("A", "AMP")):
            summary["max_currents"][name] = peak
        elif any(u in units for u in ("V", "KV", "VOLT")):
            summary["max_voltages"][name] = peak

    # Fault inception
    fault_idx = detect_fault_inception(record)
    if fault_idx is not None:
        summary["fault_inception_s"] = float(record.time[fault_idx])

    # Trip
    trip = detect_trip_time(record)
    if trip is not None:
        trip_idx, trip_ch = trip
        summary["trip_time_s"] = float(record.time[trip_idx])
        summary["trip_channel"] = trip_ch

    # Derived timing
    if summary["fault_inception_s"] is not None and summary["trip_time_s"] is not None:
        dur = summary["trip_time_s"] - summary["fault_inception_s"]
        summary["fault_duration_s"] = dur
        summary["trip_delay_ms"]    = dur * 1000.0

    # Fault type
    summary["fault_type"] = classify_fault(record, fault_idx)

    return summary


# ---------------------------------------------------------------------------
# Phasor extraction
# ---------------------------------------------------------------------------

def compute_phasors_at(
    record: EventRecord,
    t_fault_s: Optional[float] = None,
) -> Optional[dict]:
    """
    Compute DFT phasors for every analog channel at two instants:
    the fault window (one cycle starting at fault inception) and the
    pre-fault window (one cycle ending at fault inception).

    Parameters
    ----------
    t_fault_s : float, optional
        Fault inception time in seconds.  Auto-detected if not supplied.

    Returns
    -------
    dict with keys
        'fault'       : {ch_name: {'mag': float, 'ang_deg': float}}
        'pre'         : {ch_name: {'mag': float, 'ang_deg': float}}
        'ref_channel' : str    — voltage channel at 0° (Va or first V channel)
        't_fault_s'   : float
        't_pre_s'     : float
        'seq_i'       : {'I0': complex, 'I1': complex, 'I2': complex}
        'seq_v'       : {'V0': complex, 'V1': complex, 'V2': complex}
    or None if the record is too short.
    """
    spc = record.samples_per_cycle()
    t   = record.time
    n   = len(t)

    if n < spc * 2:
        return None

    # Fault inception sample
    if t_fault_s is not None:
        fi = int(np.searchsorted(t, t_fault_s))
    else:
        fi = detect_fault_inception(record)
        if fi is None:
            fi = record.trigger_index
    fi = int(np.clip(fi, spc, n - spc - 1))

    # Windows: one cycle each, butted against the inception point
    f_start = fi
    f_end   = min(fi + spc, n)
    p_start = max(0, fi - spc)
    p_end   = fi

    def _phasor(data: np.ndarray, start: int, end: int) -> complex:
        seg = data[start:end].astype(float)
        k   = np.arange(len(seg))
        return complex((2 / len(seg)) * np.dot(seg, np.exp(-1j * 2 * np.pi * k / len(seg))))

    # Raw phasors for every channel
    fault_raw: dict = {}
    pre_raw:   dict = {}
    for name, data in record.analog_channels.items():
        if f_end > f_start:
            fault_raw[name] = _phasor(data, f_start, f_end)
        if p_end > p_start:
            pre_raw[name]   = _phasor(data, p_start, p_end)

    # Choose voltage reference channel (Va preferred)
    v_names = [n for n, info in record.analog_info.items()
               if "V" in info.units.upper()]
    ref_ch: Optional[str] = None
    for cand in ("VAN", "VA", "Van", "V_A", "V-A"):
        if cand in fault_raw:
            ref_ch = cand
            break
    if ref_ch is None and v_names:
        ref_ch = next((n for n in v_names if n in fault_raw), None)
    if ref_ch is None and fault_raw:
        ref_ch = next(iter(fault_raw))

    # Reference rotation: Va fault phasor → 0°
    ref_angle = np.angle(fault_raw[ref_ch]) if ref_ch and ref_ch in fault_raw else 0.0

    def _rotate(raw_dict: dict) -> dict:
        out = {}
        for name, c in raw_dict.items():
            rotated = c * np.exp(-1j * ref_angle)
            out[name] = {
                "mag":     float(abs(rotated)),
                "ang_deg": float(np.degrees(np.angle(rotated))),
            }
        return out

    fault_ph = _rotate(fault_raw)
    pre_ph   = _rotate(pre_raw)

    # Symmetrical sequence components from fault phasors
    a  = np.exp(1j * 2 * np.pi / 3)
    a2 = a ** 2

    def _seq(raw, a_name, b_name, c_name):
        Fa = raw.get(a_name, 0+0j)
        Fb = raw.get(b_name, 0+0j)
        Fc = raw.get(c_name, 0+0j)
        if not (Fa or Fb or Fc):
            return None
        rot = np.exp(-1j * ref_angle)
        Fa *= rot; Fb *= rot; Fc *= rot
        I0 = (Fa + Fb + Fc) / 3
        I1 = (Fa + a * Fb + a2 * Fc) / 3
        I2 = (Fa + a2 * Fb + a * Fc) / 3
        return {"I0": I0, "I1": I1, "I2": I2}

    def _find_raw(raw, *candidates):
        upper = {k.upper(): v for k, v in raw.items()}
        for c in candidates:
            if c.upper() in upper:
                return c, upper[c.upper()]
        return None, 0+0j

    ia_name, _ = _find_raw(fault_raw, "IA", "Ia", "I_A")
    ib_name, _ = _find_raw(fault_raw, "IB", "Ib", "I_B")
    ic_name, _ = _find_raw(fault_raw, "IC", "Ic", "I_C")
    va_name, _ = _find_raw(fault_raw, "VAN", "VA", "Van", "V_A")
    vb_name, _ = _find_raw(fault_raw, "VBN", "VB", "Vbn", "V_B")
    vc_name, _ = _find_raw(fault_raw, "VCN", "VC", "Vcn", "V_C")

    seq_i = _seq(fault_raw, ia_name or "IA", ib_name or "IB", ic_name or "IC") or {}
    seq_v = _seq(fault_raw, va_name or "VAN", vb_name or "VBN", vc_name or "VCN") or {}

    return {
        "fault":       fault_ph,
        "pre":         pre_ph,
        "ref_channel": ref_ch,
        "t_fault_s":   float(t[f_start]),
        "t_pre_s":     float(t[p_start]) if p_end > p_start else float(t[0]),
        "seq_i":       seq_i,
        "seq_v":       seq_v,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _auto_select_currents(record: EventRecord) -> List[str]:
    """Return current channel names based on common naming patterns."""
    keywords = ("IA", "IB", "IC", "IN", "IG", "I_A", "I_B", "I_C",
                "I-A", "I-B", "I-C", "CURR", "PHASE")
    return [
        n for n in record.analog_channels
        if any(k in n.upper() for k in keywords)
    ]


def _find_channel(record: EventRecord, candidates: tuple) -> Optional[np.ndarray]:
    """Return the first matching analog channel array, case-insensitive."""
    ch_upper = {k.upper(): v for k, v in record.analog_channels.items()}
    for name in candidates:
        if name.upper() in ch_upper:
            return ch_upper[name.upper()]
    return None
