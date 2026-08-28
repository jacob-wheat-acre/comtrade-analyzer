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

from .topology import PMH_WAYS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The corpus is organised around incidents — one fault, several records — so
# the folder says so. fleet_analyze.resolve_inputs looks for this name first
# and falls back to a plain events/ dump, which is what a real SUBNET pull is.
EVENTS_DIRNAME = "incident_events"

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

# The fleet is described as a mainline topology, not a flat device list: the
# tree is what makes an incident's records consistent with each other. One
# substation bus per zone, a head device per feeder, and a couple of mid-line
# reclosers below it. build_registry() and write_topology() both read this, so
# devices.csv and topology.csv cannot disagree.
#
# The ID code is explicit rather than derived from the label — "Ridgeline" and
# "Riverbend" would otherwise both abbreviate to RI and read as one substation.

# code, station label, zone, fire risk tier, distribution kV (L-L)
_SUBSTATIONS = [
    ("CH", "Cedar Hollow",  "ZONE_A", 3, 12.47),
    ("SG", "Sawmill Grade", "ZONE_A", 3, 12.47),
    ("RG", "Ridgeline",     "ZONE_B", 2, 12.47),
    ("BG", "Bear Gulch",    "ZONE_B", 2, 12.47),
    ("ST", "Summit Tap",    "ZONE_B", 2, 12.47),
    ("VO", "Valley Oak",    "ZONE_C", 2, 21.0),
    ("AR", "Almond Row",    "ZONE_C", 2, 21.0),
    ("RB", "Riverbend",     "ZONE_D", 1, 34.5),
    ("DF", "Delta Flats",   "ZONE_D", 1, 34.5),
]

# station code, feeder name, head kind, trunk reclosers, branches, customers.
#
# `branches` is a tuple of (tap_point, count): a limb of `count` reclosers
# hanging off trunk device `tap_point`, where 0 is the head, 1 is R1 and so on.
# A real mainline is not always a chain — it forks, and the fork matters,
# because opening the device above it drops both limbs while opening the branch
# recloser drops only one. Branch devices are lettered from B so they never
# collide with the trunk's R numbering.
#
# Customers are explicit rather than drawn from the RNG: the registry is a
# fixture, and an inserted feeder must not silently renumber every other one.
# A feeder is named for the substation it comes out of. Where the two differ
# the feeder has a bus of its own — so it starts at its own breaker rather than
# a mid-line recloser. A test enforces the rule outright; there are no
# exceptions to it.
_FEEDERS = [
    ("CH", "Cedar Hollow 1211",  "breaker",  2, (),          1368),
    ("CH", "Cedar Hollow 1212",  "recloser", 1, (),           872),
    ("SG", "Sawmill Grade 1215", "breaker",  1, (),           435),
    ("RG", "Ridgeline 2104",     "breaker",  2, ((1, 2),),    927),
    ("RG", "Ridgeline 2106",     "recloser", 1, (),           885),
    ("BG", "Bear Gulch 2110",    "breaker",  1, (),           574),
    ("ST", "Summit Tap 2112",    "breaker",  0, (),           882),
    ("VO", "Valley Oak 3301",    "breaker",  2, ((1, 1), (2, 1)), 2200),
    ("VO", "Valley Oak 3305",    "recloser", 1, (),           358),
    ("AR", "Almond Row 3308",    "breaker",  1, (),           801),
    ("RB", "Riverbend 4402",     "breaker",  2, ((0, 1),),   1064),
    ("RB", "Riverbend 4407",     "recloser", 1, (),           674),
    ("DF", "Delta Flats 4411",   "breaker",  1, (),           498),
]

# Automatic PMH cabinets: (feeder, tap position on the trunk, model).
#
# EVERY WAY IS ITS OWN SWITCH — any one of them can be the one that opens, and
# opening way 2 drops only what is below way 2. So a cabinet is a group of way
# nodes sharing a `cabinet` id: way 1 is the source way, sitting under the trunk
# device, and the rest hang off it, which is what being on one bus means.
#
# A PMH way switch is a load-interrupter, not a protective device: it does not
# clear faults, so it never appears as the origin of an event.
_PMH = [
    ("Cedar Hollow 1212", 1, "PMH-10"),
    ("Valley Oak 3305",   1, "PMH-11"),
    ("Riverbend 4407",    1, "PMH-9"),
]


# Cabinets are numbered in their own band so a way never collides with a
# trunk, branch or tie number on the same circuit.
_PMH_OFFSET = 70


def _pmh_id(feeder: str) -> str:
    """
    The enclosure's grid reference: PMH_121-270. Same convention as a
    recloser, since it is the same kind of thing — gear at a location. Its
    ways are this plus a way number.
    """
    return f"PMH_{_grid_num(_feeder_number(feeder), _PMH_OFFSET)}"


def _pmh_way_id(feeder: str, way: int) -> str:
    return f"{_pmh_id(feeder)}_W{way}"


# Normally-open ties, as (feeder, position) on each side. Position is the same
# offset build_registry() numbers devices with: 0 the head, 1..n the trunk, and
# 20+ the branch limbs. Naming them by position rather than by device id means a
# change to the ID convention does not silently break every tie.
#
# Four of these cross substations, which is what makes a tie interesting.
_TIES = [
    (("Cedar Hollow 1211", 2),  ("Cedar Hollow 1212", 1)),
    (("Sawmill Grade 1215", 1), ("Bear Gulch 2110", 1)),
    (("Cedar Hollow 1212", 1),  ("Valley Oak 3305", 1)),
    (("Ridgeline 2104", 2),     ("Ridgeline 2106", 1)),
    (("Ridgeline 2106", 1),     ("Summit Tap 2112", 0)),
    (("Bear Gulch 2110", 1),    ("Almond Row 3308", 1)),
    (("Valley Oak 3301", 2),    ("Valley Oak 3305", 1)),
    (("Almond Row 3308", 1),    ("Riverbend 4407", 1)),
    (("Riverbend 4402", 2),     ("Riverbend 4407", 1)),
    (("Riverbend 4402", 1),     ("Delta Flats 4411", 1)),
    (("Ridgeline 2104", 22),    ("Bear Gulch 2110", 1)),
    (("Valley Oak 3301", 21),   ("Almond Row 3308", 1)),
    # a normally-open way on a cabinet, rather than on a mid-line recloser
    (("Valley Oak 3305", "W3"), ("Almond Row 3308", 1)),
    (("Riverbend 4407", "W2"),  ("Delta Flats 4411", 1)),
]


def _device_id(feeder: str, offset) -> str:
    """
    The id build_registry() gives the device at `offset` on `feeder`.
    `offset` is a position on the trunk, or "PMH" for that feeder's cabinet.
    """
    if isinstance(offset, str) and offset.startswith("W"):
        return _pmh_way_id(feeder, int(offset[1:]))
    head_kind = next(k for _c, f, k, _t, _b, _cu in _FEEDERS if f == feeder)
    if offset == 0 and head_kind == "breaker":
        return _breaker_id(feeder)
    return _grid_id(_feeder_number(feeder), offset)


@dataclass
class Device:
    device_id: str
    station: str
    feeder: str
    zone: str
    risk_tier: int
    customers_served: int      # THIS device's section, not the whole feeder
    kind: str                  # breaker | recloser | pmh
    model: str = ""            # PMH-9 / PMH-11 / PMH-10 on a cabinet
    cabinet: str = ""          # the enclosure a way switch belongs to
    parent: str = ""           # upstream device id, '' at the bus
    bus: str = ""              # substation bus node id
    kv_ll: float = 12.47       # from the substation — one voltage per bus
    fs: int = 1920             # this relay's record rate, not the event's


def _bus_id(code: str) -> str:
    return f"BUS_{code}"


def _feeder_number(feeder: str) -> str:
    """'Cedar Hollow 1211' -> '1211'. The circuit number, not the name."""
    return feeder.split()[-1]


def _abbr(feeder: str) -> str:
    """'Valley Oak 3301' -> 'VALL'. First four letters of the feeder name."""
    letters = "".join(c for c in feeder if c.isalpha())
    return letters[:4].upper()


def _breaker_id(feeder: str) -> str:
    """A substation breaker is named for its feeder: BKR_VALL3301."""
    return f"BKR_{_abbr(feeder)}{_feeder_number(feeder)}"


def _grid_num(circuit: str, offset: int) -> str:
    """
    A device's six-digit grid reference, formatted 123-456.

    Derived from the circuit number so the numbers are stable and unique
    without a global counter — insert a device and nothing else renumbers.
    """
    n = int(circuit) * 100 + offset
    return f"{n // 1000:03d}-{n % 1000:03d}"


def _grid_id(circuit: str, offset: int) -> str:
    """A recloser carries its grid reference: RCL_330-101."""
    return f"RCL_{_grid_num(circuit, offset)}"


def _split_customers(total: int, n_sections: int) -> List[int]:
    """
    Spread a feeder's customers along its mainline. The section closest to the
    substation carries about half — the trunk is denser than the tail — and the
    rounding remainder goes there too, so the parts always sum to the whole.
    """
    if n_sections <= 1:
        return [total]
    head = int(round(total * 0.5))
    each = (total - head) // (n_sections - 1)
    rest = [each] * (n_sections - 1)
    return [total - sum(rest)] + rest


def build_registry(rng: random.Random) -> List[Device]:
    """
    The fleet as a flat device list, derived from _FEEDERS so it always matches
    the topology. `rng` is unused now that customers are explicit; it stays in
    the signature because the caller threads one RNG through the whole build.
    """
    stations = {code: (label, zone, tier, kv)
                for code, label, zone, tier, kv in _SUBSTATIONS}
    devices: List[Device] = []
    for code, feeder, head_kind, n_trunk, branches, customers in _FEEDERS:
        label, zone, tier, kv = stations[code]
        num = _feeder_number(feeder)
        sub = f"{label} Sub"
        bus = _bus_id(code)
        cabinets = [c for c in _PMH if c[0] == feeder]
        total = (1 + n_trunk + sum(c for _, c in branches)
                 + sum(PMH_WAYS[m] for _f, _t, m in cabinets))
        sections = _split_customers(customers, total)
        nxt = iter(sections)

        def _add(did, parent, kind="recloser", model="", cabinet=""):
            devices.append(Device(did, sub, feeder, zone, tier, next(nxt), kind,
                                  model=model, cabinet=cabinet, parent=parent,
                                  bus=bus, kv_ll=kv, fs=rng.choice(SAMPLE_RATES)))
            return did

        head_id = (_breaker_id(feeder) if head_kind == "breaker"
                   else _grid_id(num, 0))
        _add(head_id, bus, head_kind)

        trunk = [head_id]
        parent = head_id
        for i in range(1, n_trunk + 1):
            parent = _add(_grid_id(num, i), parent)
            trunk.append(parent)

        # Branch limbs continue the same grid numbering, offset clear of the
        # trunk so a limb device never collides with a trunk one.
        off = 20
        for tap, count in branches:
            parent = trunk[min(tap, len(trunk) - 1)]
            for _ in range(count):
                off += 1
                parent = _add(_grid_id(num, off), parent)

        # One node per way. Way 1 is the source way under the trunk; the rest
        # sit on the same bus, so they hang off it.
        for _f, tap, model in cabinets:
            cab = _pmh_id(feeder)
            src = _add(_pmh_way_id(feeder, 1), trunk[min(tap, len(trunk) - 1)],
                       "pmh", model, cab)
            for w in range(2, PMH_WAYS[model] + 1):
                _add(_pmh_way_id(feeder, w), src, "pmh", model, cab)
    return devices


def write_registry(devices: List[Device], path: str) -> None:
    lines = ["device_id,station,feeder,zone,risk_tier,customers_served"]
    for d in devices:
        lines.append(f"{d.device_id},{d.station},{d.feeder},{d.zone},"
                     f"{d.risk_tier},{d.customers_served}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_topology(devices: List[Device], path: str) -> None:
    """
    Write the connectivity file topology.py reads: a source row per substation,
    a row per device, then the ties. Generated from the same _FEEDERS/_TIES
    tables as the registry so the two can never drift.
    """
    by_id = {d.device_id: d for d in devices}
    lines = ["feeder,node_id,kind,parent,tie_to,cabinet,model"]
    for code, label, _zone, _tier, _kv in _SUBSTATIONS:
        lines.append(f"{label} Sub,{_bus_id(code)},source,,,,")
        for d in devices:
            if d.bus == _bus_id(code):
                lines.append(f"{d.feeder},{d.device_id},{d.kind},{d.parent},,"
                             f"{d.cabinet},{d.model}")
    # A tie is a recloser, so it is named like one — numbered off the feeder it
    # hangs from, in a band clear of that feeder's own devices.
    tie_off = {}
    for (nf, no), (ff, fo) in _TIES:
        near, far = _device_id(nf, no), _device_id(ff, fo)
        num = _feeder_number(nf)
        tie_off[num] = tie_off.get(num, 50) + 1
        lines.append(f"{nf},{_grid_id(num, tie_off[num])},tie,{near},{far},,")
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
    # Load current after this shot's fault clears. Set on a witness record:
    # when the device below opens, the load it was feeding disappears from
    # everything upstream, so the post-fault load steps DOWN. Leaving it at the
    # pre-fault level is the detail a protection engineer spots first.
    load_after: Optional[float] = None


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

    # --- incident context ---------------------------------------------------
    # One fault produces several records: the device that cleared it, every
    # device upstream that saw the same current and correctly did not trip,
    # and — after a lockout — the tie that picked the section back up. They
    # share an incident_id in the ground truth ONLY; a relay has no idea the
    # others exist, so the pipeline has to re-derive the grouping from
    # timestamps and topology the way it must on a real SUBNET pull.
    incident_id: str = ""
    role: str = "origin"           # origin | witness | tie_pickup
    # Cold-load inrush on a tie_pickup record:
    # (t_close, peak A, settled A, decay tau s). Before t_close the record
    # carries i_load — the far feeder's own load; after it, the inrush decays
    # toward `settled`, which is that load plus the section just picked up.
    cold_load: Optional[Tuple[float, float, float, float]] = None

    @property
    def t_trigger(self) -> float:
        """Seconds into the record at which the relay triggered."""
        if self.shots:
            return self.shots[0].t_fault
        return self.cold_load[0] if self.cold_load else 0.0


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

    def fill_load(mask, amp=None):
        amp = sc.i_load if amp is None else amp
        for p in "ABC":
            cur_arr[p][mask] = amp * np.sin(w * t[mask] + PHASE_ANGLE[p])
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

    # A tie pickup record has no fault at all: the far feeder's own load, then
    # a cold-load inrush when the tie closes and it picks up the stranded
    # section. Decays toward the combined load, which is where it stays.
    if sc.cold_load is not None and not sc.shots:
        t_close, peak, settled, tau = sc.cold_load
        fill_load(t < t_close)
        mask = t >= t_close
        envelope = settled + (peak - settled) * np.exp(-(t[mask] - t_close) / tau)
        for p in "ABC":
            cur_arr[p][mask] = envelope * np.sin(w * t[mask] + PHASE_ANGLE[p])
            volt_arr[p][mask] = sc.v_peak * np.sin(w * t[mask] + PHASE_ANGLE[p])
        i_n_cold = cur_arr["A"] + cur_arr["B"] + cur_arr["C"]
        return {
            "n": n, "t": t,
            "analog": [ia, ib, ic, i_n_cold, van, vbn, vcn],
            "digital": [np.zeros(n, dtype=int), np.zeros(n, dtype=int),
                        np.zeros(n, dtype=int), np.zeros(n, dtype=int),
                        np.ones(n, dtype=int), np.zeros(n, dtype=int)],
        }

    cursor = 0.0
    load_now = sc.i_load
    for shot in sc.shots:
        fill_load((t >= cursor) & (t < shot.t_fault), load_now)

        if shot.t_trip is None:
            # Self-clearing fault, or a fault cleared by a device further down
            # the feeder — either way this relay never trips.
            t_end = shot.t_clear if shot.t_clear is not None else sc.t_total
            fill_fault((t >= shot.t_fault) & (t < t_end), shot.t_fault, shot.mag_scale)
            cursor = t_end
            if shot.load_after is not None:
                load_now = shot.load_after
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
        fill_load((t >= cursor) & (t < sc.t_total), load_now)

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

    prim_cur = int(round(max(sc.i_fault, sc.i_load * 2) * 1.5 / 100.0) * 100)
    prim_vlt = int(round(sc.v_peak))

    start_dt = sc.timestamp
    trig_dt = start_dt + timedelta(seconds=sc.t_trigger)

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


# Load current scales with the customers a device feeds, so an upstream
# record and the downstream record of the same fault differ in exactly the way
# the topology says they should.
_AMPS_PER_CUSTOMER = (0.055, 0.085)


def _load_current(rng: random.Random, customers: int) -> float:
    """Peak load current for a device feeding `customers`, clamped to sane rails."""
    return float(np.clip(customers * rng.uniform(*_AMPS_PER_CUSTOMER), 12.0, 260.0))


def _fault_current(rng: random.Random, i_load: float, depth: int) -> float:
    """
    Available fault current at a section `depth` hops from the substation.

    Line impedance is NOT in the topology model, and it stays out of it — this
    is the generator choosing a plausible level for a location, so that a fault
    out at the end of a feeder is weaker than one at the head. Every device
    upstream of the fault sees this SAME current: on a radial mainline there is
    no branch between them to divide it.
    """
    base = rng.uniform(max(9.0 * i_load, 320.0), 2600.0)
    return max(base * (0.72 ** max(0, depth - 1)), 9.0 * i_load, 260.0)


def build_scenario(idx: int, rng: random.Random, device: Device,
                   base_date: datetime, depth: int = 1,
                   customers_below: int = 0, ratio_customers: int = 0,
                   template: Optional[str] = None) -> Scenario:
    if template is None:
        template = rng.choices(list(_TEMPLATE_WEIGHTS),
                               weights=list(_TEMPLATE_WEIGHTS.values()))[0]
    ftype, phases = _pick_fault(rng)
    fs = device.fs

    kv_ll = device.kv_ll
    v_peak = kv_ll * 1000 / np.sqrt(3) * np.sqrt(2)

    i_load = _load_current(rng, customers_below or device.customers_served)
    # Every device up the path sees this same fault current against its own,
    # larger, load. The classifier needs unfaulted/faulted RMS under 0.15 on
    # all of them, so size the fault against the heaviest-loaded device on the
    # path — the feeder head — not against the one that cleared it.
    heaviest = i_load
    if ratio_customers and customers_below:
        heaviest = i_load * (ratio_customers / customers_below)
    i_fault = _fault_current(rng, heaviest, depth)

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
# Incidents — one fault, several records
# ---------------------------------------------------------------------------

# How likely an upstream device is to have recorded the same fault. The device
# immediately above usually does; a substation breaker two sections up often
# never picks up at all. Nothing here is a claim about real relay settings —
# it is a mix that keeps both cases in the corpus.
_WITNESS_P = (0.80, 0.45, 0.25)

# How often the device above the fault operates as well. Both devices on one
# path tripping for one fault is a coordination problem you cannot see in
# either record alone — it takes the tree to notice. Kept rare, the way it is
# on a feeder that is mostly coordinated correctly.
_OVERTRIP_P = 0.09

# ADMS runs the restoration, not this tool. Its tie close lands tens of seconds
# after the lockout, which is far outside any same-fault time window — so
# grouping it with the rest has to come from the topology, not the clock.
_RESTORE_DELAY_S = (20.0, 120.0)


def _incident_id(idx: int, origin: Scenario) -> str:
    return f"INC{idx:04d}_{origin.timestamp.strftime('%Y%m%d_%H%M%S')}"


def _witness_scenario(origin: Scenario, device: Device, rng: random.Random,
                      customers_below: int, share_lost: float,
                      idx: int, seq: int) -> Scenario:
    """
    A record from a device upstream of the one that cleared the fault.

    It sees the same fault, at the same instant, at the same magnitude — and it
    does not trip, because something below it did. Afterwards its load steps
    down by whatever the device below was feeding.
    """
    first = origin.shots[0]
    t_clear = T_PRE + ((first.t_trip - first.t_fault) if first.t_trip is not None
                       else (first.t_clear - first.t_fault))
    i_load = _load_current(rng, customers_below)
    shots = [Shot(T_PRE, None, "", None, t_clear=t_clear,
                  load_after=i_load * (1.0 - share_lost))]
    t_total = t_clear + rng.uniform(0.15, 0.35)

    expect_flags = ["no_trip"]
    if origin.fault_type == "3PH":
        expect_flags.append("3ph_fault")
    elif origin.fault_type == "LLG":
        expect_flags.append("llg_fault")

    ts = origin.timestamp + timedelta(milliseconds=rng.uniform(-30, 30))
    return Scenario(
        event_id=f"{device.device_id}_{ts.strftime('%Y%m%d_%H%M%S')}_{idx:03d}{seq}",
        device=device, fault_type=origin.fault_type, phases=origin.phases,
        shots=shots, t_lockout=None, t_total=t_total, fs=device.fs,
        i_load=i_load, i_fault=origin.i_fault, v_peak=origin.v_peak,
        kv_ll=device.kv_ll, timestamp=ts, scenario="witness_no_trip",
        expect_wso="EPSS_CANDIDATE", expect_flags=sorted(set(expect_flags)),
        role="witness",
    )


def _overtrip_scenario(origin: Scenario, device: Device, rng: random.Random,
                       customers_below: int, base_date: datetime,
                       idx: int, seq: int) -> Scenario:
    """
    An upstream device that operated for the same fault as the one below it.

    Both devices open, so the outage is everything under the *upper* one when
    it should have been everything under the lower. Neither record shows this:
    each one on its own is a device that saw a fault and tripped, which is what
    a device is for. It only reads as wrong once you know they are on one path.
    """
    sc = build_scenario(idx, rng, device, base_date, depth=1,
                        customers_below=customers_below,
                        template=rng.choice(["single_trip", "reclose_1_success"]))
    # Same fault, same instant, same current — only the load differs.
    sc.fault_type = origin.fault_type
    sc.phases = origin.phases
    sc.i_fault = origin.i_fault
    sc.scenario = "overtrip"
    sc.role = "overtrip"
    sc.expect_flags = sorted({f for f in sc.expect_flags
                              if f not in ("3ph_fault", "llg_fault")}
                             | ({"3ph_fault"} if origin.fault_type == "3PH" else set())
                             | ({"llg_fault"} if origin.fault_type == "LLG" else set()))
    sc.timestamp = origin.timestamp + timedelta(milliseconds=rng.uniform(-30, 30))
    sc.event_id = (f"{device.device_id}_"
                   f"{sc.timestamp.strftime('%Y%m%d_%H%M%S')}_{idx:03d}{seq}")
    return sc


def _tie_pickup_scenario(origin: Scenario, device: Device, rng: random.Random,
                         own_customers: int, restored_customers: int,
                         idx: int, seq: int) -> Scenario:
    """
    The far side of a tie, closing onto the section stranded by a lockout.

    No fault, no trip — a step up in load with a cold-load inrush on top. This
    is what a FLISR restoration actually looks like in an event file, and it
    lands on a different feeder from the fault that caused it.
    """
    own = _load_current(rng, own_customers)
    restored = _load_current(rng, restored_customers)
    settled = own + restored
    peak = settled * rng.uniform(1.6, 2.4)
    t_close = rng.uniform(0.10, 0.20)
    tau = rng.uniform(0.8, 2.5)
    t_total = t_close + rng.uniform(2.0, 4.0)

    ts = (origin.timestamp
          + timedelta(seconds=(origin.t_lockout or origin.t_total))
          + timedelta(seconds=rng.uniform(*_RESTORE_DELAY_S))
          - timedelta(seconds=t_close))
    return Scenario(
        event_id=f"{device.device_id}_{ts.strftime('%Y%m%d_%H%M%S')}_{idx:03d}{seq}",
        # "LOAD" is what this is and what the classifier should call it: a
        # balanced current step with the voltage still up.
        device=device, fault_type="LOAD", phases=(), shots=[],
        t_lockout=None, t_total=t_total, fs=device.fs,
        i_load=own, i_fault=0.0, v_peak=origin.v_peak, kv_ll=device.kv_ll,
        timestamp=ts, scenario="tie_pickup", expect_wso="NOT_EXPOSED",
        expect_flags=[], role="tie_pickup",
        cold_load=(t_close, peak, settled, tau),
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def build_incident(idx: int, rng: random.Random, net, devices: List[Device],
                   by_id: dict, base_date: datetime) -> List[Scenario]:
    """
    One fault and every record it produced, origin first.

    The fault happens on some device's section. That device clears it; every
    device between it and the substation saw the same current and correctly
    stayed put; and if it locked out, a tie may have picked the section back
    up minutes later on a neighbouring feeder.
    """
    # Faults are weighted toward the mid-line: there are more line miles out
    # there than in the first section out of the substation.
    # A PMH way switch does not clear faults, so it is never the origin of an
    # event. It is still in `devices` — it carries customers and it matters for
    # N-1 — but nothing records there.
    recording = [d for d in devices if d.kind in ("breaker", "recloser")]
    weights = [1.0 + 0.6 * max(0, net.depth(d.device_id) - 1) for d in recording]
    origin_dev = rng.choices(recording, weights=weights)[0]
    depth = net.depth(origin_dev.device_id)
    below = net.customers_below(origin_dev.device_id, _as_registry(devices))

    path = net.path_to_source(origin_dev.device_id)
    head = next((n for n in reversed(path) if n.kind in ("breaker", "recloser")),
                None)
    head_below = (net.customers_below(head.node_id, _as_registry(devices))
                  if head is not None else below)

    origin = build_scenario(idx, rng, origin_dev, base_date, depth=depth,
                            customers_below=below, ratio_customers=head_below)
    inc = _incident_id(idx, origin)
    origin.incident_id = inc
    records = [origin]

    # A high-impedance fault is tens of amps: nothing upstream picks it up.
    if origin.scenario != "hif":
        upstream = [n for n in path[1:] if n.kind in ("breaker", "recloser")]
        # Occasionally the device above operates too, instead of holding.
        overtrip_at = 0 if (upstream and origin.shots[0].t_trip is not None
                            and rng.random() < _OVERTRIP_P) else None
        for i, node in enumerate(upstream):
            dev = by_id[node.node_id]
            up_below = net.customers_below(node.node_id, _as_registry(devices))
            if i == overtrip_at:
                r = _overtrip_scenario(origin, dev, rng, up_below, base_date,
                                       idx, i + 1)
            else:
                p_rec = _WITNESS_P[i] if i < len(_WITNESS_P) else _WITNESS_P[-1]
                if rng.random() >= p_rec:
                    continue
                share = below / up_below if up_below else 0.0
                r = _witness_scenario(origin, dev, rng, up_below, share, idx, i + 1)
            r.incident_id = inc
            records.append(r)

    # After a lockout the section is stranded until something ties it back in.
    if origin.t_lockout is not None:
        ties = net.backup_ties(origin_dev.device_id)
        if ties and rng.random() < 0.65:
            tie = rng.choice(ties)
            far = net.node(tie.tie_to)
            if far is not None and far.node_id in by_id:
                dev = by_id[far.node_id]
                own = net.customers_below(far.node_id, _as_registry(devices))
                t = _tie_pickup_scenario(origin, dev, rng, own, below,
                                         idx, len(records) + 1)
                t.incident_id = inc
                records.append(t)

    return records


def _as_registry(devices: List[Device]) -> dict:
    """Devices in the shape topology.customers_below expects."""
    from .wso_impact import _normalize
    return {_normalize(d.device_id): {"customers_served": d.customers_served}
            for d in devices}


def _truth_row(sc: Scenario) -> dict:
    return {
        "event_id":        sc.event_id,
        "file":            f"{sc.event_id}.cfg",
        "incident_id":     sc.incident_id,
        "role":            sc.role,
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
    }


def generate_fleet(out_dir: str, count: int, seed: int) -> dict:
    """
    Build `count` incidents. Each yields several event files, so the number of
    records written is larger — and unlike the old one-event-per-fault corpus,
    the records of one fault are consistent with each other.
    """
    from .topology import load_topology

    rng = random.Random(seed)
    events_dir = os.path.join(out_dir, EVENTS_DIRNAME)
    os.makedirs(events_dir, exist_ok=True)

    devices = build_registry(rng)
    by_id = {d.device_id: d for d in devices}
    registry_path = os.path.join(out_dir, "fleet_devices.csv")
    topology_path = os.path.join(out_dir, "topology.csv")
    write_registry(devices, registry_path)
    write_topology(devices, topology_path)
    net = load_topology(topology_path)

    base_date = datetime(2026, 6, 1, 0, 0, 0)
    truth = []
    written = 0

    for i in range(1, count + 1):
        for sc in build_incident(i, rng, net, devices, by_id, base_date):
            write_event(sc, synthesize(sc), events_dir)
            truth.append(_truth_row(sc))
            written += 1
        if i % 10 == 0 or i == count:
            print(f"  {i}/{count} incidents, {written} event files written")

    truth_path = os.path.join(out_dir, "fleet_truth.json")
    with open(truth_path, "w", encoding="utf-8") as fh:
        json.dump({"seed": seed, "incidents": count, "count": len(truth),
                   "events": truth}, fh, indent=2)

    return {"events_dir": events_dir, "registry": registry_path,
            "topology": topology_path, "truth": truth_path,
            "devices": len(devices), "incidents": count, "events": len(truth)}


def main():
    p = argparse.ArgumentParser(
        prog="fleet-gen",
        description="Generate a synthetic COMTRADE event fleet for batch analysis")
    p.add_argument("--count", type=int, default=70,
                   help="number of INCIDENTS (default 70). Each yields several "
                        "event files, so expect roughly 3x this many records.")
    p.add_argument("--seed", type=int, default=20260601, help="RNG seed for reproducibility")
    # Relative to the working directory, not the package: an installed copy
    # must never try to write fixtures into site-packages.
    p.add_argument("--out-dir", default=os.path.join(os.getcwd(), "fleet"),
                   help="output folder (default ./fleet)")
    args = p.parse_args()

    print(f"Generating {args.count} synthetic incidents (seed {args.seed}) ...")
    info = generate_fleet(args.out_dir, args.count, args.seed)
    print()
    print(f"  Events   → {info['events_dir']}  "
          f"({info['events']} records from {info['incidents']} incidents)")
    print(f"  Registry → {info['registry']}  ({info['devices']} devices)")
    print(f"  Topology → {info['topology']}")
    print(f"  Truth    → {info['truth']}")
    print()
    print("Next:")
    print(f"  python3 fleet_analyze.py {args.out_dir}")


if __name__ == "__main__":
    main()
