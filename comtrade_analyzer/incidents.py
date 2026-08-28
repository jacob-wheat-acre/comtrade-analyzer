#!/usr/bin/env python3
"""
incidents.py — Group event records into the faults that caused them.

One fault leaves several records. The device that cleared it has one; every
device between there and the substation that saw the same current and did not
trip has one; and after a lockout, the tie that picks the stranded section back
up has one, on a different feeder, minutes later.

Nothing in a COMTRADE file says which records belong together — a relay has no
idea the others exist — so the grouping is rebuilt here from two things a real
SUBNET pull does have: when the fault started, and how the feeder is wired.

The point is to read events against a one-line, not to grade a restoration
scheme. FLISR runs in the ADMS. What this gives you is "these four records are
one fault, here is where each device sits, and here is what actually went
dark" — the rest is a judgement call you make looking at the picture.

Two joins, deliberately different
---------------------------------
Same fault      Records whose fault instants fall inside `window_s` AND whose
                devices share one path to the source. Both are required. Time
                alone merges unrelated faults across the fleet in a storm;
                topology alone merges every fault that feeder ever had.

Restoration     A LOAD record (cold-load inrush, no fault) on the far side of a
                tie that backs up a section someone just locked out. No time
                window finds this — it is tens of seconds later, on another
                feeder, under a different device id — so it is topology only,
                bounded by `restore_window_s`.

Clock skew is the practical limit. Relay clocks drift, and one with a missed
timezone lands an hour out, which looks exactly like a different fault. That is
why `window_s` is tunable and why `clock_suspects()` reports pairs that match
on topology but miss on time: it is a clock problem worth seeing, not a
grouping failure worth hiding.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

# Records of one fault land within this of each other. Generous next to relay
# operate times, tight next to the gap between two unrelated faults.
DEFAULT_WINDOW_S = 2.0

# A restoration follows a lockout by tens of seconds — ADMS decides, switches
# operate, and cold load picks up. Beyond this it is a separate switching job.
DEFAULT_RESTORE_WINDOW_S = 900.0


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

def fault_instant(event: dict) -> Optional[datetime]:
    """
    Absolute moment the fault started, or None if the record never picked one.

    Record start plus the inception offset — NOT the trigger time, which is
    where the relay decided to save the record and can sit anywhere in it.
    """
    ts = event.get("timestamp")
    if not ts:
        return None
    try:
        start = datetime.fromisoformat(ts)
    except ValueError:
        return None
    inc = event.get("fault_inception_s")
    return start + timedelta(seconds=float(inc)) if inc is not None else start


def _is_load_step(event: dict) -> bool:
    return event.get("fault_type") == "LOAD"


def _tripped(event: dict) -> bool:
    return bool(event.get("total_shots"))


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

class _Union:
    """Plain union-find. The groups are small; nothing here needs to be clever."""

    def __init__(self, keys: Iterable):
        self._p = {k: k for k in keys}

    def find(self, k):
        while self._p[k] != k:
            self._p[k] = self._p[self._p[k]]
            k = self._p[k]
        return k

    def join(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._p[rb] = ra

    def groups(self) -> Dict[object, list]:
        out: Dict[object, list] = {}
        for k in self._p:
            out.setdefault(self.find(k), []).append(k)
        return out


def _same_fault(a: dict, b: dict, net, window_s: float) -> bool:
    ta, tb = fault_instant(a), fault_instant(b)
    if ta is None or tb is None:
        return False
    if abs((ta - tb).total_seconds()) > window_s:
        return False
    da, db = a.get("device_id", ""), b.get("device_id", "")
    if net is not None and da in net and db in net:
        return net.on_same_path([da, db])
    # No topology to appeal to. Same feeder is the honest fallback — it will
    # over-merge two faults a second apart on one feeder, and that is stated.
    return bool(a.get("feeder")) and a.get("feeder") == b.get("feeder")


def group_events(events: List[dict], net=None,
                 window_s: float = DEFAULT_WINDOW_S,
                 restore_window_s: float = DEFAULT_RESTORE_WINDOW_S) -> List[dict]:
    """
    Partition `events` into incidents, and stamp each event with its
    `incident_id`. Returns one summary dict per incident, earliest first.
    """
    faults = [e for e in events if not _is_load_step(e)]
    loads = [e for e in events if _is_load_step(e)]

    # --- same-fault join ---------------------------------------------------
    by_id = {e["event_id"]: e for e in events}
    uf = _Union(e["event_id"] for e in events)

    ordered = sorted((e for e in faults if fault_instant(e) is not None),
                     key=fault_instant)
    for i, a in enumerate(ordered):
        ta = fault_instant(a)
        for b in ordered[i + 1:]:
            if (fault_instant(b) - ta).total_seconds() > window_s:
                break           # sorted, so nothing further can match
            if _same_fault(a, b, net, window_s):
                uf.join(a["event_id"], b["event_id"])

    # --- restoration join --------------------------------------------------
    if net is not None:
        lockouts = sorted((e for e in faults
                           if e.get("locked_out") and fault_instant(e)),
                          key=fault_instant)
        for load in loads:
            t_close = fault_instant(load)
            dev = load.get("device_id", "")
            if t_close is None or dev not in net:
                continue
            best = None
            for lk in lockouts:
                gap = (t_close - fault_instant(lk)).total_seconds()
                if gap < 0 or gap > restore_window_s:
                    continue
                if not _backs_up(net, lk.get("device_id", ""), dev):
                    continue
                if best is None or gap < best[0]:
                    best = (gap, lk)
            if best is not None:
                uf.join(best[1]["event_id"], load["event_id"])

    # --- summarise ---------------------------------------------------------
    incidents = []
    for members in uf.groups().values():
        rows = sorted((by_id[m] for m in members),
                      key=lambda e: (fault_instant(e) or datetime.max))
        incidents.append(_summarise(rows, net))
    incidents.sort(key=lambda i: i["started_at"] or "")

    for inc in incidents:
        for eid in inc["event_ids"]:
            by_id[eid]["incident_id"] = inc["incident_id"]
    return incidents


def _backs_up(net, locked_device: str, tie_far_device: str) -> bool:
    """
    True if a tie below `locked_device` reaches `tie_far_device` — i.e. this
    device is a plausible source for the section that just went dark.
    """
    for tie in net.backup_ties(locked_device):
        far = net.node(tie.tie_to)
        if far is None:
            continue
        if far.node_id == tie_far_device:
            return True
        # The tie may land below the device that recorded the pickup: a feeder
        # head sees the inrush of a tie further down its own mainline.
        if net.is_upstream_of(tie_far_device, far.node_id):
            return True
    return False


def _summarise(rows: List[dict], net) -> dict:
    """One incident, described in the terms a one-line makes sense of."""
    first = rows[0]
    t0 = fault_instant(first)
    devices = [r.get("device_id", "") for r in rows]

    tripped = [r for r in rows if _tripped(r)]
    load_steps = [r for r in rows if _is_load_step(r)]
    faulted = [r for r in rows if not _is_load_step(r)]

    # The device that cleared it is the deepest one that actually operated;
    # with nothing tripped, the deepest that saw it is still the best anchor.
    anchor_pool = [r.get("device_id", "") for r in (tripped or faulted)]
    clearing = None
    if net is not None and anchor_pool:
        node = net.deepest(anchor_pool)
        clearing = node.node_id if node is not None else anchor_pool[0]
    elif anchor_pool:
        clearing = anchor_pool[0]

    held = [r.get("device_id", "") for r in faulted if not _tripped(r)]
    locked = [r for r in rows if r.get("locked_out")]

    # Two devices on one path both operating is the thing a single record can
    # never show. Reported as an observation, not a verdict — fuse saving and
    # a genuine over-trip look the same from here.
    upstream_also_tripped = False
    if net is not None and len(tripped) > 1:
        ids = [r.get("device_id", "") for r in tripped]
        upstream_also_tripped = net.on_same_path(ids)

    restore_delay_s = None
    if locked and load_steps:
        t_lock = fault_instant(locked[0])
        t_close = fault_instant(load_steps[0])
        if t_lock and t_close:
            restore_delay_s = round((t_close - t_lock).total_seconds(), 1)

    out_device = (locked[0].get("device_id", "") if locked
                  else (tripped[0].get("device_id", "") if tripped else clearing))
    customers_out = 0
    for r in rows:
        if r.get("device_id") == out_device:
            customers_out = r.get("customers_affected", 0) or 0
            break

    stamp = (t0 or datetime.min).strftime("%Y%m%d_%H%M%S")
    return {
        "incident_id":      f"INC_{stamp}_{(clearing or 'UNKNOWN')}",
        "started_at":       t0.isoformat() if t0 else None,
        "event_ids":        [r["event_id"] for r in rows],
        "record_count":     len(rows),
        "devices":          devices,
        "feeders":          sorted({r.get("feeder", "") for r in rows if r.get("feeder")}),
        "zone":             first.get("zone", ""),
        "clearing_device":  clearing,
        "devices_held":     held,
        "upstream_also_tripped": upstream_also_tripped,
        "locked_out":       bool(locked),
        "restored":         bool(load_steps),
        "restore_delay_s":  restore_delay_s,
        "customers_out":    customers_out,
        "fault_type":       next((r["fault_type"] for r in faulted), "LOAD"),
        "priority":         min((r.get("priority", 3) for r in rows), default=3),
        "flags":            sorted({f for r in rows for f in r.get("flags", [])}),
    }


# ---------------------------------------------------------------------------
# Clock skew
# ---------------------------------------------------------------------------

def clock_suspects(events: List[dict], net, window_s: float = DEFAULT_WINDOW_S,
                   far_s: float = 7200.0) -> List[dict]:
    """
    Pairs that share a path and a fault signature but miss the time window.

    A relay whose clock is minutes — or a whole timezone — out looks exactly
    like a separate fault. Say so rather than quietly splitting the incident.
    """
    if net is None:
        return []
    out = []
    faults = [e for e in events
              if not _is_load_step(e) and fault_instant(e) is not None]
    for i, a in enumerate(faults):
        for b in faults[i + 1:]:
            da, db = a.get("device_id", ""), b.get("device_id", "")
            if da == db or da not in net or db not in net:
                continue
            if not net.on_same_path([da, db]):
                continue
            gap = abs((fault_instant(a) - fault_instant(b)).total_seconds())
            if window_s < gap <= far_s and a.get("fault_type") == b.get("fault_type"):
                out.append({
                    "events": [a["event_id"], b["event_id"]],
                    "devices": [da, db],
                    "gap_s": round(gap, 3),
                    "fault_type": a.get("fault_type"),
                })
    out.sort(key=lambda r: r["gap_s"])
    return out
