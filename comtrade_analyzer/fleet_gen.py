#!/usr/bin/env python3
"""
fleet_gen.py — Synthetic COMTRADE event fleet generator.

Builds a folder of ~100 IEEE C37.111 event files spanning the fault types,
reclose outcomes and triage flags the analyzer is meant to sort, so the batch
pipeline (fleet_analyze.py) and the dashboard have a realistic corpus to chew
on.  Everything here is synthetic: device IDs, feeder names and customer counts
are invented, and the registry it writes is a fixture, never operational data.

Waveform construction
---------------------
Each event is a timeline of segments — LOAD (balanced pre-fault), FAULT (one of
SLG / LL / LLG / 3PH) and OPEN (breaker open, all channels dead).  Segments are
filled vectorised; digital channels (TRIP, 50P, 51P, 79, 52A, LOCK) are derived
from the same timeline so the reclose-sequence detector sees a self-consistent
record.

The magnitudes are chosen to land on the correct side of the classifier
thresholds in analysis.classify_fault (see the ratio notes on _fault_profile).
fleet_analyze.py re-checks every event against the ground truth written to
fleet_truth.json, so a drift in either direction shows up as a mismatch rather
than as a silently wrong corpus.

Usage
-----
  python3 fleet_gen.py                    # 100 events → ./fleet/events/
  python3 fleet_gen.py --count 250 --seed 7
  python3 fleet_gen.py --out-dir ./big_fleet
"""

from __future__ import annotations

import argparse
import json
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

F0 = 60.0                       # power frequency, Hz — US distribution
PHASE_ANGLE = {"A": 0.0, "B": -2 * np.pi / 3, "C": 2 * np.pi / 3}
SAMPLE_RATES = [960, 1920, 1920, 1920, 3840]     # weighted toward 1920 Hz
T_PRE = 0.050                   # pre-fault run-up, s (≥3 cycles for the baseline)
ADC_FULL_SCALE = 32767

# HIF fixtures sit on a lightly loaded single-phase tap: the classifier needs
# unfaulted/faulted RMS < 0.15 to call SLG, and the HIF screen needs the RMS
# rise to stay under 50 A.  Both hold only when pre-fault load is small.
HIF_LOAD_PEAK = (4.0, 7.0)
HIF_FAULT_PEAK = (34.0, 48.0)


# ---------------------------------------------------------------------------
# Synthetic device registry
# ---------------------------------------------------------------------------

# zone, zone label, ID code, fire risk tier, feeder names.
# The ID code is explicit rather than derived from the label — "Ridgeline" and
# "Riverbend" would otherwise both abbreviate to RI and read as one substation.
_ZONES = [
    ("ZONE_A", "Cedar Hollow", "CH", 3, ["Cedar Hollow 1211", "Cedar Hollow 1212", "Sawmill Grade 1215"]),
    ("ZONE_B", "Ridgeline",    "RG", 2, ["Ridgeline 2104", "Ridgeline 2106", "Bear Gulch 2110", "Summit Tap 2112"]),
    ("ZONE_C", "Valley Oak",   "VO", 2, ["Valley Oak 3301", "Valley Oak 3305", "Almond Row 3308"]),
    ("ZONE_D", "Riverbend",    "RB", 1, ["Riverbend 4402", "Riverbend 4407", "Delta Flats 4411"]),
]


@dataclass
class Device:
    device_id: str
    station: str
    feeder: str
    zone: str
    risk_tier: int
    customers_served: int
    kind: str            # 'recloser' or 'breaker'


def build_registry(rng: random.Random) -> List[Device]:
    """Invent a small distribution fleet: 2–4 devices per zone."""
    devices: List[Device] = []
    for zone, label, code, tier, feeders in _ZONES:
        sub = f"{label} Sub"
        for i, feeder in enumerate(feeders):
            kind = "breaker" if i == 0 else "recloser"
            prefix = "BKR" if kind == "breaker" else "RCL"
            did = f"{prefix}_{code}-{feeder.split()[-1]}"
            customers = rng.randint(340, 1180) if kind == "recloser" else rng.randint(900, 2400)
            devices.append(Device(did, sub, feeder, zone, tier, customers, kind))
    return devices


def write_registry(devices: List[Device], path: str) -> None:
    lines = ["device_id,station,feeder,zone,risk_tier,customers_served"]
    for d in devices:
        lines.append(f"{d.device_id},{d.station},{d.feeder},{d.zone},{d.risk_tier},{d.customers_served}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Scenario description
# ---------------------------------------------------------------------------

@dataclass
class Shot:
    """One fault-operate(-reclose) cycle."""
    t_fault: float
    t_trip: Optional[float]        # None = fault self-cleared, no relay operation
    element: str                   # '50P', '51P', '50G', '51G'
    t_close: Optional[float]       # None = no reclose (lockout or end of record)
    t_clear: Optional[float] = None  # for the no-trip case: when the fault vanishes
    mag_scale: float = 1.0


@dataclass
class Scenario:
    event_id: str
    device: Device
    fault_type: str                # SLG / LL / LLG / 3PH
    phases: Tuple[str, ...]
    shots: List[Shot]
    t_lockout: Optional[float]
    t_total: float
    fs: int
    i_load: float                  # peak, A
    i_fault: float                 # peak, A
    v_peak: float                  # peak L-N, V
    kv_ll: float
    timestamp: datetime
    scenario: str                  # template name, for ground truth
    expect_wso: str                # PERMANENT / WSO_EXPOSED / NOT_EXPOSED
    expect_flags: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Fault profiles
# ---------------------------------------------------------------------------

def _fault_profile(ftype: str, phases: Tuple[str, ...], i_fault: float, i_load: float):
    """
    Per-phase current and voltage behaviour during a fault.

    Returns (currents, voltages) where each is {phase: (amplitude, angle_rad,
    dc_fraction)} — current = amp*sin(wt + angle) + dc_fraction*amp*exp(-t/tau),
    voltage = mult*V_PEAK*sin(wt + angle).

    Magnitude ratios matter: classify_fault compares one-cycle RMS ratios
    against 0.15 (bot/top) and 0.7, so the unfaulted phases must stay below
    0.15 x the faulted phase.  build_scenario enforces i_fault >= 8 x i_load,
    which puts the ratio near 0.12 before DC offset widens the margin further.
    """
    cur = {}
    volt = {}
    unfaulted = [p for p in "ABC" if p not in phases]

    if ftype == "SLG":
        x = phases[0]
        cur[x] = (i_fault, PHASE_ANGLE[x] + np.radians(12), 0.70)
        volt[x] = (0.10, PHASE_ANGLE[x])
        for p in unfaulted:
            cur[p] = (i_load, PHASE_ANGLE[p], 0.0)
            volt[p] = (1.08, PHASE_ANGLE[p])

    elif ftype == "LL":
        # No ground path: Ix = -Iy, so I0 stays at load/3 and the LL branch wins.
        x, y = phases
        ang = PHASE_ANGLE[x] + np.radians(30)
        cur[x] = (i_fault, ang, 0.60)
        cur[y] = (i_fault, ang + np.pi, -0.60)
        z = unfaulted[0]
        cur[z] = (i_load, PHASE_ANGLE[z], 0.0)
        # Both faulted phases collapse to a common midpoint voltage
        common = PHASE_ANGLE[x] - np.radians(60)
        volt[x] = (0.50, common)
        volt[y] = (0.50, common)
        volt[z] = (1.05, PHASE_ANGLE[z])

    elif ftype == "LLG":
        # Unequal magnitudes and a 60 deg spread give real zero sequence,
        # which is what separates LLG from LL in the classifier.
        x, y = phases
        ang = PHASE_ANGLE[x] + np.radians(15)
        cur[x] = (i_fault, ang, 0.70)
        cur[y] = (0.75 * i_fault, ang - np.radians(60), -0.40)
        z = unfaulted[0]
        cur[z] = (i_load, PHASE_ANGLE[z], 0.0)
        volt[x] = (0.08, PHASE_ANGLE[x])
        volt[y] = (0.10, PHASE_ANGLE[y])
        volt[z] = (1.15, PHASE_ANGLE[z])   # neutral shift on the healthy phase

    elif ftype == "3PH":
        for p in "ABC":
            dc = 0.70 if p == "A" else -0.35
            cur[p] = (i_fault, PHASE_ANGLE[p] + np.radians(10), dc)
            volt[p] = (0.15, PHASE_ANGLE[p])

    else:
        raise ValueError(f"unknown fault type {ftype!r}")

    return cur, volt


# ---------------------------------------------------------------------------
# Waveform synthesis
# ---------------------------------------------------------------------------

def synthesize(sc: Scenario):
    """Build analog and digital channel arrays for one scenario."""
    n = int(round(sc.fs * sc.t_total))
    t = np.arange(n) / sc.fs
    w = 2 * np.pi * F0
    tau_dc = 10.0 / (2 * np.pi * F0)

    ia = np.zeros(n); ib = np.zeros(n); ic = np.zeros(n)
    van = np.zeros(n); vbn = np.zeros(n); vcn = np.zeros(n)
    cur_arr = {"A": ia, "B": ib, "C": ic}
    volt_arr = {"A": van, "B": vbn, "C": vcn}

    def fill_load(mask):
        for p in "ABC":
            cur_arr[p][mask] = sc.i_load * np.sin(w * t[mask] + PHASE_ANGLE[p])
            volt_arr[p][mask] = sc.v_peak * np.sin(w * t[mask] + PHASE_ANGLE[p])

    def fill_fault(mask, t0, mag_scale):
        cur, volt = _fault_profile(sc.fault_type, sc.phases,
                                   sc.i_fault * mag_scale, sc.i_load)
        dt = t[mask] - t0
        decay = np.exp(-dt / tau_dc)
        for p in "ABC":
            amp, ang, dc_frac = cur[p]
            cur_arr[p][mask] = amp * np.sin(w * t[mask] + ang) + dc_frac * amp * decay
            mult, vang = volt[p]
            volt_arr[p][mask] = mult * sc.v_peak * np.sin(w * t[mask] + vang)

    # ── Walk the timeline ────────────────────────────────────────────────────
    trip_times: List[float] = []
    open_windows: List[Tuple[float, Optional[float]]] = []   # (t_trip, t_close)
    p50_windows: List[Tuple[float, float]] = []
    p51_windows: List[Tuple[float, float]] = []

    cursor = 0.0
    for shot in sc.shots:
        fill_load((t >= cursor) & (t < shot.t_fault))

        if shot.t_trip is None:
            # Self-clearing fault, no relay operation
            t_end = shot.t_clear if shot.t_clear is not None else sc.t_total
            fill_fault((t >= shot.t_fault) & (t < t_end), shot.t_fault, shot.mag_scale)
            cursor = t_end
            continue

        fill_fault((t >= shot.t_fault) & (t < shot.t_trip), shot.t_fault, shot.mag_scale)
        trip_times.append(shot.t_trip)
        (p51_windows if shot.element.startswith("51") else p50_windows).append(
            (shot.t_fault, shot.t_trip))

        t_open_end = shot.t_close if shot.t_close is not None else sc.t_total
        open_windows.append((shot.t_trip, shot.t_close))
        # Breaker open — everything dead
        cursor = t_open_end

    if cursor < sc.t_total:
        fill_load((t >= cursor) & (t < sc.t_total))

    # OPEN intervals overwrite whatever was there
    for t_trip, t_close in open_windows:
        end = t_close if t_close is not None else sc.t_total
        mask = (t >= t_trip) & (t < end)
        for p in "ABC":
            cur_arr[p][mask] = 0.0
            volt_arr[p][mask] = 0.0

    i_n = ia + ib + ic

    # ── Digital channels ─────────────────────────────────────────────────────
    # TRIP: asserted from trip until the breaker recloses (coil de-energises),
    # and latched to the end of record on lockout.
    trip_sig = np.zeros(n, dtype=int)
    for t_trip, t_close in open_windows:
        end = t_close if t_close is not None else sc.t_total + 1.0
        trip_sig[(t >= t_trip) & (t < end)] = 1

    # 52A: breaker closed = 1
    a52_sig = np.ones(n, dtype=int)
    for t_trip, t_close in open_windows:
        end = t_close if t_close is not None else sc.t_total + 1.0
        a52_sig[(t >= t_trip) & (t < end)] = 0

    p50_sig = np.zeros(n, dtype=int)
    for t0, t1 in p50_windows:
        p50_sig[(t >= t0) & (t < t1)] = 1

    p51_sig = np.zeros(n, dtype=int)
    for t0, t1 in p51_windows:
        p51_sig[(t >= t0) & (t < t1)] = 1

    # 79: reclose-initiate pulse, 10 ms at each reclose
    p79_sig = np.zeros(n, dtype=int)
    for _, t_close in open_windows:
        if t_close is not None:
            p79_sig[(t >= t_close) & (t < t_close + 0.010)] = 1

    lock_sig = np.zeros(n, dtype=int)
    if sc.t_lockout is not None:
        lock_sig[t >= sc.t_lockout] = 1

    return {
        "n": n,
        "t": t,
        "analog": [ia, ib, ic, i_n, van, vbn, vcn],
        "digital": [trip_sig, p50_sig, p51_sig, p79_sig, a52_sig, lock_sig],
    }


# ---------------------------------------------------------------------------
# COMTRADE writers
# ---------------------------------------------------------------------------

_ANALOG_META = [
    ("IA", "A", "A"), ("IB", "B", "A"), ("IC", "C", "A"), ("IN", "N", "A"),
    ("VAN", "A", "V"), ("VBN", "B", "V"), ("VCN", "C", "V"),
]
_DIGITAL_META = [("TRIP", 0), ("50P", 0), ("51P", 0), ("79", 0), ("52A", 1), ("LOCK", 0)]


def write_event(sc: Scenario, wave: dict, out_dir: str) -> str:
    """Write the .cfg/.dat pair.  Returns the .cfg path."""
    n = wave["n"]
    ia, ib, ic, i_n, van, vbn, vcn = wave["analog"]

    # One scale factor per quantity keeps the raw integers inside 16-bit range
    a_cur = max(np.max(np.abs(np.concatenate([ia, ib, ic, i_n]))), 1.0) * 1.05 / ADC_FULL_SCALE
    a_vlt = max(np.max(np.abs(np.concatenate([van, vbn, vcn]))), 1.0) * 1.05 / ADC_FULL_SCALE
    scales = [a_cur] * 4 + [a_vlt] * 3

    prim_cur = int(round(sc.i_fault * 1.5 / 100.0) * 100)
    prim_vlt = int(round(sc.v_peak))

    start_dt = sc.timestamp
    trig_dt = start_dt + timedelta(seconds=sc.shots[0].t_fault)

    def _dt(d: datetime) -> str:
        return d.strftime("%d/%m/%Y,%H:%M:%S.%f")

    lines = [
        f"{sc.device.feeder},{sc.device.device_id},1999",
        f"{len(_ANALOG_META) + len(_DIGITAL_META)},{len(_ANALOG_META)}A,{len(_DIGITAL_META)}D",
    ]
    for i, ((name, phase, unit), scale) in enumerate(zip(_ANALOG_META, scales), start=1):
        prim = prim_cur if unit == "A" else prim_vlt
        sec = 5 if unit == "A" else 1
        lines.append(f"{i},{name},{phase},,{unit},{scale:.8f},0.0,0,"
                     f"-{ADC_FULL_SCALE},{ADC_FULL_SCALE},{prim},{sec},P")
    for i, (name, normal) in enumerate(_DIGITAL_META, start=1):
        lines.append(f"{i},{name},A,,{normal}")
    lines += [
        f"{int(F0)}",
        "1",
        f"{sc.fs},{n}",
        _dt(start_dt),
        _dt(trig_dt),
        "ASCII",
        "1",
    ]

    cfg_path = os.path.join(out_dir, f"{sc.event_id}.cfg")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    # ---- DAT ----
    cols = [np.arange(1, n + 1), (wave["t"] * 1e6).astype(np.int64)]
    for sig, scale in zip(wave["analog"], scales):
        cols.append(np.clip(np.round(sig / scale), -ADC_FULL_SCALE, ADC_FULL_SCALE).astype(np.int64))
    for sig in wave["digital"]:
        cols.append(sig.astype(np.int64))

    table = np.column_stack(cols)
    dat_path = os.path.join(out_dir, f"{sc.event_id}.dat")
    np.savetxt(dat_path, table, fmt="%d", delimiter=",", newline="\r\n")

    return cfg_path


# ---------------------------------------------------------------------------
# Scenario templates
# ---------------------------------------------------------------------------

# name → (weight, builder key).  Weights approximate a normal-day mix on a
# 12 kV distribution system: most faults are transient SLG cleared by one
# fast trip and a successful first reclose.
_TEMPLATE_WEIGHTS = {
    "single_trip":        14,   # cleared, no reclose in the record
    "reclose_1_success":  30,   # one shot then successful reclose
    "reclose_2_success":  12,   # two shots then successful reclose
    "lockout_2shot":       9,
    "lockout_3shot":       9,
    "slow_trip":           8,
    "no_trip":             5,
    "hif":                 6,
    "reclose_1_perm":      7,   # reclosed, faulted again, record ends mid-sequence
}

# expect_wso mirrors wso_impact.classify_event. A single trip whose record ends
# a few hundred ms later cannot show whether a reclose followed, so it is
# INDETERMINATE; a fault the device rode through is an EPSS_CANDIDATE. Keep these
# in step with that function or the detector-agreement panel goes red.
_FAULT_WEIGHTS = [("SLG", 66), ("LL", 13), ("LLG", 13), ("3PH", 8)]
_SLG_PHASES = [("A",), ("B",), ("C",)]
_PAIR_PHASES = [("A", "B"), ("B", "C"), ("A", "C")]


def _pick_fault(rng: random.Random) -> Tuple[str, Tuple[str, ...]]:
    types, weights = zip(*_FAULT_WEIGHTS)
    ftype = rng.choices(types, weights=weights)[0]
    if ftype == "SLG":
        return ftype, rng.choice(_SLG_PHASES)
    if ftype == "3PH":
        return ftype, ("A", "B", "C")
    return ftype, rng.choice(_PAIR_PHASES)


def _ground_element(ftype: str, fast: bool) -> str:
    ground = ftype in ("SLG", "LLG")
    return ("50G" if fast else "51G") if ground else ("50P" if fast else "51P")


def build_scenario(idx: int, rng: random.Random, devices: List[Device],
                   base_date: datetime) -> Scenario:
    template = rng.choices(list(_TEMPLATE_WEIGHTS), weights=list(_TEMPLATE_WEIGHTS.values()))[0]
    device = rng.choice(devices)
    ftype, phases = _pick_fault(rng)
    fs = rng.choice(SAMPLE_RATES)

    kv_ll = rng.choice([12.47, 12.47, 12.47, 21.0, 34.5])
    v_peak = kv_ll * 1000 / np.sqrt(3) * np.sqrt(2)

    i_load = rng.uniform(55.0, 165.0)
    # >= 8x load keeps the unfaulted/faulted RMS ratio under the 0.15 threshold
    i_fault = rng.uniform(max(9.0 * i_load, 320.0), 2600.0)

    if template == "hif":
        ftype, phases = "SLG", rng.choice(_SLG_PHASES)
        i_load = rng.uniform(*HIF_LOAD_PEAK)
        i_fault = rng.uniform(*HIF_FAULT_PEAK)

    fast_ms = lambda: rng.uniform(0.016, 0.040)
    slow_ms = lambda: rng.uniform(0.200, 0.420)
    dead1 = lambda: rng.uniform(0.45, 1.10)
    dead2 = lambda: rng.uniform(1.20, 2.20)
    tail = lambda: rng.uniform(0.30, 0.55)

    t_f1 = T_PRE
    shots: List[Shot] = []
    t_lockout: Optional[float] = None
    expect_flags: List[str] = []

    if template == "single_trip":
        op = fast_ms()
        shots.append(Shot(t_f1, t_f1 + op, _ground_element(ftype, True), None))
        t_total = t_f1 + op + rng.uniform(0.12, 0.25)
        expect_wso = "INDETERMINATE"

    elif template == "slow_trip":
        op = slow_ms()
        shots.append(Shot(t_f1, t_f1 + op, _ground_element(ftype, False), None))
        t_total = t_f1 + op + rng.uniform(0.12, 0.25)
        expect_wso = "INDETERMINATE"
        expect_flags.append("slow_trip")

    elif template == "no_trip":
        # Fault appears and self-extinguishes; no TRIP bit ever asserts.
        clear = t_f1 + rng.uniform(0.05, 0.12)
        shots.append(Shot(t_f1, None, "", None, t_clear=clear))
        t_total = clear + rng.uniform(0.15, 0.30)
        expect_wso = "EPSS_CANDIDATE"
        expect_flags.append("no_trip")

    elif template == "hif":
        op = slow_ms()
        shots.append(Shot(t_f1, t_f1 + op, "51G", None))
        t_total = t_f1 + op + rng.uniform(0.15, 0.30)
        expect_wso = "INDETERMINATE"
        expect_flags.append("hif_suspect")
        if op * 1000 > 10 * (1000 / F0):
            expect_flags.append("slow_trip")

    elif template == "reclose_1_success":
        op, dt1 = fast_ms(), dead1()
        t_close = t_f1 + op + dt1
        shots.append(Shot(t_f1, t_f1 + op, _ground_element(ftype, True), t_close))
        t_total = t_close + tail()
        expect_wso = "WSO_EXPOSED"

    elif template == "reclose_2_success":
        op1, dt1 = fast_ms(), dead1()
        c1 = t_f1 + op1 + dt1
        t_f2 = c1 + rng.uniform(0.008, 0.035)      # reclose into a still-present fault
        op2 = slow_ms()
        c2 = t_f2 + op2 + dead2()
        shots.append(Shot(t_f1, t_f1 + op1, _ground_element(ftype, True), c1))
        shots.append(Shot(t_f2, t_f2 + op2, _ground_element(ftype, False), c2, mag_scale=rng.uniform(0.82, 0.98)))
        t_total = c2 + tail()
        expect_wso = "WSO_EXPOSED"
        expect_flags.append("multiple_shots")

    elif template == "reclose_1_perm":
        # Reclosed once into the fault; the record ends before the next reclose.
        op1, dt1 = fast_ms(), dead1()
        c1 = t_f1 + op1 + dt1
        t_f2 = c1 + rng.uniform(0.006, 0.020)
        op2 = slow_ms()
        shots.append(Shot(t_f1, t_f1 + op1, _ground_element(ftype, True), c1))
        shots.append(Shot(t_f2, t_f2 + op2, _ground_element(ftype, False), None,
                          mag_scale=rng.uniform(0.85, 1.0)))
        t_total = t_f2 + op2 + rng.uniform(0.20, 0.35)
        expect_wso = "WSO_EXPOSED"

    elif template == "lockout_2shot":
        op1, dt1 = fast_ms(), dead1()
        c1 = t_f1 + op1 + dt1
        t_f2 = c1 + rng.uniform(0.005, 0.020)
        op2 = slow_ms()
        t_lockout = t_f2 + op2
        shots.append(Shot(t_f1, t_f1 + op1, _ground_element(ftype, True), c1))
        shots.append(Shot(t_f2, t_lockout, _ground_element(ftype, False), None,
                          mag_scale=rng.uniform(0.85, 1.0)))
        t_total = t_lockout + rng.uniform(0.20, 0.35)
        expect_wso = "PERMANENT"
        expect_flags.append("lockout")

    elif template == "lockout_3shot":
        op1, dt1 = fast_ms(), dead1()
        c1 = t_f1 + op1 + dt1
        t_f2 = c1 + rng.uniform(0.005, 0.020)
        op2 = slow_ms()
        c2 = t_f2 + op2 + dead2()
        t_f3 = c2 + rng.uniform(0.004, 0.015)
        op3 = fast_ms()
        t_lockout = t_f3 + op3
        shots.append(Shot(t_f1, t_f1 + op1, _ground_element(ftype, True), c1))
        shots.append(Shot(t_f2, t_f2 + op2, _ground_element(ftype, False), c2,
                          mag_scale=rng.uniform(0.85, 1.0)))
        shots.append(Shot(t_f3, t_lockout, _ground_element(ftype, True), None,
                          mag_scale=rng.uniform(0.85, 1.0)))
        t_total = t_lockout + rng.uniform(0.20, 0.35)
        expect_wso = "PERMANENT"
        expect_flags.append("lockout")

    else:
        raise ValueError(template)

    if ftype == "3PH":
        expect_flags.append("3ph_fault")
    elif ftype == "LLG":
        expect_flags.append("llg_fault")

    # Slow first-shot operate times trip the coordination flag too
    first = shots[0]
    if first.t_trip is not None and template not in ("slow_trip", "hif"):
        if (first.t_trip - first.t_fault) * 1000 > 10 * (1000 / F0):
            expect_flags.append("slow_trip")

    ts = base_date + timedelta(
        days=rng.randint(0, 80),
        hours=rng.randint(0, 23),
        minutes=rng.randint(0, 59),
        seconds=rng.randint(0, 59),
    )
    event_id = f"{device.device_id}_{ts.strftime('%Y%m%d_%H%M%S')}_{idx:03d}"

    return Scenario(
        event_id=event_id, device=device, fault_type=ftype, phases=phases,
        shots=shots, t_lockout=t_lockout, t_total=t_total, fs=fs,
        i_load=i_load, i_fault=i_fault, v_peak=v_peak, kv_ll=kv_ll,
        timestamp=ts, scenario=template, expect_wso=expect_wso,
        expect_flags=sorted(set(expect_flags)),
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def generate_fleet(out_dir: str, count: int, seed: int) -> dict:
    rng = random.Random(seed)
    events_dir = os.path.join(out_dir, "events")
    os.makedirs(events_dir, exist_ok=True)

    devices = build_registry(rng)
    registry_path = os.path.join(out_dir, "fleet_devices.csv")
    write_registry(devices, registry_path)

    base_date = datetime(2026, 6, 1, 0, 0, 0)
    truth = []

    for i in range(1, count + 1):
        sc = build_scenario(i, rng, devices, base_date)
        wave = synthesize(sc)
        write_event(sc, wave, events_dir)
        truth.append({
            "event_id":        sc.event_id,
            "file":            f"{sc.event_id}.cfg",
            "scenario":        sc.scenario,
            "device_id":       sc.device.device_id,
            "feeder":          sc.device.feeder,
            "station":         sc.device.station,
            "zone":            sc.device.zone,
            "risk_tier":       sc.device.risk_tier,
            "customers":       sc.device.customers_served,
            "timestamp":       sc.timestamp.isoformat(),
            "sample_rate":     sc.fs,
            "kv_ll":           sc.kv_ll,
            "duration_s":      round(sc.t_total, 4),
            "expect_fault":    sc.fault_type,
            "faulted_phases":  "".join(sc.phases),
            "expect_shots":    sum(1 for s in sc.shots if s.t_trip is not None),
            "expect_wso":      sc.expect_wso,
            "expect_flags":    sc.expect_flags,
            "i_load_peak_a":   round(sc.i_load, 1),
            "i_fault_peak_a":  round(sc.i_fault, 1),
        })
        if i % 10 == 0 or i == count:
            print(f"  {i}/{count} events written")

    truth_path = os.path.join(out_dir, "fleet_truth.json")
    with open(truth_path, "w", encoding="utf-8") as fh:
        json.dump({"seed": seed, "count": count, "events": truth}, fh, indent=2)

    return {"events_dir": events_dir, "registry": registry_path,
            "truth": truth_path, "devices": len(devices)}


def main():
    p = argparse.ArgumentParser(
        prog="fleet-gen",
        description="Generate a synthetic COMTRADE event fleet for batch analysis")
    p.add_argument("--count", type=int, default=100, help="number of events (default 100)")
    p.add_argument("--seed", type=int, default=20260601, help="RNG seed for reproducibility")
    # Relative to the working directory, not the package: an installed copy
    # must never try to write fixtures into site-packages.
    p.add_argument("--out-dir", default=os.path.join(os.getcwd(), "fleet"),
                   help="output folder (default ./fleet)")
    args = p.parse_args()

    print(f"Generating {args.count} synthetic events (seed {args.seed}) ...")
    info = generate_fleet(args.out_dir, args.count, args.seed)
    print()
    print(f"  Events   → {info['events_dir']}")
    print(f"  Registry → {info['registry']}  ({info['devices']} devices)")
    print(f"  Truth    → {info['truth']}")
    print()
    print("Next:")
    print(f"  python3 fleet_analyze.py {args.out_dir}")


if __name__ == "__main__":
    main()
