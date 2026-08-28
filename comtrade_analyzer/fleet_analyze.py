#!/usr/bin/env python3
"""
fleet_analyze.py — Batch analysis across a whole folder of COMTRADE events.

Runs the full single-event pipeline (parse → event summary → feeder/recloser
analysis → triage → WSO/EPSS classification) over every .cfg in a folder and
flattens the result into one per-event table plus the aggregates the dashboard
needs.  Where wso_impact.py answers "what is the EPSS exposure for this zone",
this answers "what is in this folder" — fault mix, triage backlog, timing
distribution, per-device hot spots — with the WSO numbers alongside.

The WSO classification itself is imported from wso_impact.py rather than
re-derived, so the PERMANENT / NOT_EXPOSED / WSO_EXPOSED boundaries stay in
exactly one place.

Outputs (in <folder>/analysis/):
  fleet_analysis.json   full per-event records + aggregates  (dashboard input)
  fleet_events.csv      one row per event, for spreadsheets

Usage
-----
  python3 fleet_analyze.py ./fleet
  python3 fleet_analyze.py ./events --devices devices.csv --feeder-z 0.45
  python3 fleet_analyze.py ./fleet --jobs 8
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

_HERE = Path(__file__).parent

from .comtrade_parser import COMTRADEParser
from .diagnostics import check_record, explain_parse_error, worst_level
from .relay_settings import load_settings
from .analysis import (
    compute_event_summary,
    compute_phasors_at,
    detect_digital_transitions,
    estimate_dc_offset,
)
from .feeder_analysis import compute_feeder_summary
from .triage import rule_table, triage_event
from .fleet_gen import EVENTS_DIRNAME
from .incidents import clock_suspects, group_events
from .wso_impact import (
    EPSS_CANDIDATE, NOT_EXPOSED,
    _normalize, class_table, classify_event, load_registry, lookup_device,
)

DEFAULT_FEEDER_Z = 0.4          # Ω/mile, typical overhead distribution
HIF_THRESHOLD_A = 50.0

# Longest distribution feeder the impedance-ratio locator is credible over.
# Beyond this the apparent impedance is not line impedance — see _location_valid.
MAX_PLAUSIBLE_MILES = 30.0


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    path = _HERE / "config.json"
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Per-event analysis
# ---------------------------------------------------------------------------

def _find_cfg(folder: str) -> list:
    found = []
    for pat in ("*.cfg", "*.CFG", "*.cff", "*.CFF"):
        found.extend(glob.glob(os.path.join(folder, pat)))
        found.extend(glob.glob(os.path.join(folder, "**", pat), recursive=True))
    return sorted(set(found))


def _location_valid(est_miles: Optional[float], hif_suspect: bool) -> tuple:
    """
    Decide whether a fault-location estimate is worth showing.

    estimate_fault_location() divides residual fault voltage by fault current,
    which only reads as line impedance when the fault is bolted.  On a
    high-impedance fault the arc and ground resistance dominate, and the
    estimate runs off to tens of miles on a feeder that is only a few miles
    long.  Flag those rather than charting them as if they were distances.
    """
    if est_miles is None:
        return False, "No fault-location estimate (no fault current or no voltage channel)"
    if hif_suspect:
        return False, "Suppressed — high-impedance fault; arc resistance dominates the impedance ratio"
    if est_miles > MAX_PLAUSIBLE_MILES:
        return False, f"Suppressed — {est_miles:.0f} mi exceeds plausible feeder length ({MAX_PLAUSIBLE_MILES:.0f} mi)"
    return True, "±20–30% without arc resistance / load compensation"


# ---------------------------------------------------------------------------
# Waveform extraction for the dashboard's inline viewer
# ---------------------------------------------------------------------------

def _minmax_decimate(sig, lo: int, hi: int, buckets: int, quantum: float) -> list:
    """
    Reduce a slice of samples to a flat [min0, max0, min1, max1, ...] envelope.

    Min/max per bucket rather than plain sub-sampling: a fault waveform's peaks
    are the whole point, and stride-sampling drops them.  Zoomed in far enough
    that a bucket holds one sample, min == max and the envelope collapses back
    to the actual trace, so one representation serves both views.

    `quantum` is the value per stored integer unit — voltages are kept in tens
    of volts so the JSON stays small.
    """
    n = max(1, hi - lo)
    buckets = max(1, min(buckets, n))
    edges = np.linspace(lo, hi, buckets + 1).astype(int)
    out = []
    for b in range(buckets):
        a, z = edges[b], max(edges[b + 1], edges[b] + 1)
        chunk = sig[a:z]
        if len(chunk) == 0:
            out.extend([0, 0])
            continue
        out.append(int(round(float(chunk.min()) / quantum)))
        out.append(int(round(float(chunk.max()) / quantum)))
    return out


def _digital_edges(sig, time, lo: int, hi: int) -> list:
    """[[ms, value], ...] — the opening value plus every transition."""
    seg = sig[lo:hi].astype(np.int16)
    if len(seg) == 0:
        return []
    out = [[round(float(time[lo]) * 1000, 3), int(seg[0])]]
    idx = np.where(np.diff(seg) != 0)[0] + 1
    for i in idx:
        out.append([round(float(time[lo + i]) * 1000, 3), int(seg[i])])
    return out


def _window_for_fault(record, fault_idx, trip_idx) -> tuple:
    """A few cycles either side of inception, stretched to include the trip."""
    spc = record.samples_per_cycle()
    n = len(record.time)
    if fault_idx is None:
        fault_idx = record.trigger_index
    lo = max(0, fault_idx - 2 * spc)
    hi = fault_idx + 10 * spc
    if trip_idx is not None:
        hi = max(hi, trip_idx + 3 * spc)
    return lo, min(n, hi)


def extract_waveform(record, summary, feeder, buckets_full: int, buckets_zoom: int) -> dict:
    """Decimate the record into the payload the dashboard's viewer draws."""
    n = len(record.time)
    if n == 0:
        return {}

    currents, voltages = [], []
    for name in record.analog_channels:
        units = record.analog_info[name].units.upper()
        if any(u in units for u in ("A", "AMP")) and len(currents) < 4:
            currents.append(name)
        elif any(u in units for u in ("V", "KV", "VOLT")) and len(voltages) < 3:
            voltages.append(name)

    def peak(names):
        return max((float(np.max(np.abs(record.analog_channels[c]))) for c in names), default=1.0)

    i_peak, v_peak = peak(currents) if currents else 1.0, peak(voltages) if voltages else 1.0
    # One stored unit = 1 A for currents, 10 V for voltages
    i_q, v_q = 1.0, 10.0

    t_ms = lambda i: round(float(record.time[i]) * 1000, 3)

    fault_idx = None
    if summary.get("fault_inception_s") is not None:
        fault_idx = int(np.searchsorted(record.time, summary["fault_inception_s"]))
    trip_idx = None
    if summary.get("trip_time_s") is not None:
        trip_idx = int(np.searchsorted(record.time, summary["trip_time_s"]))

    def view(lo, hi, buckets):
        return {
            "t0": t_ms(lo), "t1": t_ms(min(hi, n) - 1),
            "i": {c: _minmax_decimate(record.analog_channels[c], lo, hi, buckets, i_q) for c in currents},
            "v": {c: _minmax_decimate(record.analog_channels[c], lo, hi, buckets, v_q) for c in voltages},
            "d": {c: _digital_edges(record.digital_channels[c], record.time, lo, hi)
                  for c in record.digital_channels},
        }

    zlo, zhi = _window_for_fault(record, fault_idx, trip_idx)
    seq = (feeder or {}).get("reclose_sequence")

    return {
        "i_names": currents,
        "v_names": voltages,
        "d_names": list(record.digital_channels),
        "i_unit": "A", "v_unit": "V",
        "i_q": i_q, "v_q": v_q,
        "i_peak": round(i_peak, 1), "v_peak": round(v_peak, 1),
        "full": view(0, n, buckets_full),
        "zoom": view(zlo, zhi, buckets_zoom),
        "marks": {
            "trigger": t_ms(record.trigger_index),
            "fault":   t_ms(fault_idx) if fault_idx is not None and fault_idx < n else None,
            "trip":    t_ms(trip_idx) if trip_idx is not None and trip_idx < n else None,
            "recloses": [t_ms(s.reclose_index) for s in (seq.shots if seq else [])
                         if s.reclose_index is not None],
            "trips":    [t_ms(s.trip_index) for s in (seq.shots if seq else [])],
            "lockout": t_ms(seq.lockout_index) if seq and seq.lockout_index is not None else None,
        },
    }


def extract_report_detail(record, summary, feeder) -> dict:
    """
    Everything the Word report used to present, as data.

    The .docx was the original way to *look at* an event, so the dashboard has
    to carry the same content: provenance, per-channel peaks, the phasor
    diagram, the digital operations log, and the DC-offset notes.
    """
    spc = record.samples_per_cycle()
    trig_ms = float(record.trigger_time) * 1000.0

    # ── Provenance ───────────────────────────────────────────────────────────
    start = record.metadata.get("start_time")
    trig_abs = record.metadata.get("trigger_time_abs")
    provenance = {
        "station":       record.metadata.get("station_name", ""),
        "device_id":     record.metadata.get("rec_dev_id", ""),
        "revision":      record.metadata.get("rev_year", ""),
        "file_type":     record.metadata.get("file_type", ""),
        "line_freq_hz":  record.line_freq(),
        "sample_rate_hz": record.sample_rate,
        "n_analog":      len(record.analog_channels),
        "n_digital":     len(record.digital_channels),
        "n_samples":     len(record.time),
        "recording_start": start.isoformat() if isinstance(start, datetime) else None,
        "trigger_abs":     trig_abs.isoformat() if isinstance(trig_abs, datetime) else None,
        "trigger_ms":      round(trig_ms, 3),
    }

    # ── Peak measured quantities, every channel ──────────────────────────────
    peaks = []
    for name, data in record.analog_channels.items():
        units = record.analog_info[name].units
        peak = float(np.max(np.abs(data)))
        rms = float(np.sqrt(np.mean(data ** 2)))
        peaks.append({"channel": name, "units": units,
                      "peak": round(peak, 1), "rms": round(rms, 1)})

    # ── Digital operations log ───────────────────────────────────────────────
    ops = []
    for ch in record.digital_channels:
        tr = detect_digital_transitions(record, ch)
        for idx in tr["rising"]:
            t = float(record.time[idx]) * 1000.0
            ops.append({"channel": ch, "event": "ASSERT", "t_ms": round(t, 3),
                        "rel_trigger_ms": round(t - trig_ms, 3)})
        for idx in tr["falling"]:
            t = float(record.time[idx]) * 1000.0
            ops.append({"channel": ch, "event": "DEASSERT", "t_ms": round(t, 3),
                        "rel_trigger_ms": round(t - trig_ms, 3)})
    ops.sort(key=lambda o: (o["t_ms"], o["channel"]))

    # ── DC offset and decay time constant, per current channel ───────────────
    dc_notes = []
    fault_idx = None
    if summary.get("fault_inception_s") is not None:
        fault_idx = int(np.searchsorted(record.time, summary["fault_inception_s"]))
    if fault_idx is not None:
        for name, data in record.analog_channels.items():
            if not any(k in name.upper() for k in ("IA", "IB", "IC", "IN")):
                continue
            seg = data[fault_idx:fault_idx + spc]
            if len(seg) < spc:
                continue
            dc, tau = estimate_dc_offset(seg, spc)
            if abs(dc) < 1.0:
                continue
            dc_notes.append({"channel": name, "dc_a": round(dc, 1),
                             "tau_ms": round(tau * 1000.0, 1) if tau > 0 else None})

    # ── Phasors at the fault window ──────────────────────────────────────────
    phasors = None
    try:
        ph = compute_phasors_at(record, t_fault_s=summary.get("fault_inception_s"))
    except Exception:                                   # noqa: BLE001
        ph = None
    if ph:
        def _pol(z):
            return {"mag": round(float(abs(z)), 1),
                    "ang": round(float(np.degrees(np.angle(z))), 1)}
        phasors = {
            "ref_channel": ph.get("ref_channel"),
            "t_fault_ms":  round(ph["t_fault_s"] * 1000.0, 3),
            "t_pre_ms":    round(ph["t_pre_s"] * 1000.0, 3),
            "fault": {k: {"mag": round(v["mag"], 1), "ang": round(v["ang_deg"], 1)}
                      for k, v in ph["fault"].items()},
            "pre":   {k: {"mag": round(v["mag"], 1), "ang": round(v["ang_deg"], 1)}
                      for k, v in ph["pre"].items()},
            "seq_i": {k: _pol(v) for k, v in (ph.get("seq_i") or {}).items()},
            "seq_v": {k: _pol(v) for k, v in (ph.get("seq_v") or {}).items()},
            "units": {n: record.analog_info[n].units for n in record.analog_channels},
        }

    # ── Elements that operated, consolidated ─────────────────────────────────
    seq = (feeder or {}).get("reclose_sequence")
    elements = sorted({s.element for s in (seq.shots if seq else []) if s.element})

    return {
        "provenance":  provenance,
        "peaks":       peaks,
        "digital_log": ops,
        "dc_offset":   dc_notes,
        "phasors":     phasors,
        "elements":    elements,
    }


_SETTINGS_CACHE: dict = {}


def _catalog(path, primary):
    """Load the settings catalog once per worker process."""
    if not path:
        return None
    key = (path, primary)
    if key not in _SETTINGS_CACHE:
        try:
            _SETTINGS_CACHE[key] = load_settings(path, pickups_are_primary=primary)
        except Exception:                          # noqa: BLE001 — sanity_check reports it
            _SETTINGS_CACHE[key] = None
    return _SETTINGS_CACHE[key]


def analyze_one(args: tuple) -> dict:
    """
    Analyze a single COMTRADE file.  Returns a flat dict (JSON-serialisable).

    Runs in a worker process, so it takes a plain tuple and returns plain data.
    """
    filepath, feeder_z, slow_trip_cycles, wave_opts, settings_opts = args
    basename = os.path.basename(filepath)
    out = {"file": basename, "path": filepath, "ok": False, "error": None}

    try:
        record = COMTRADEParser().parse(filepath)
    except Exception as exc:                       # noqa: BLE001 — report, don't crash the batch
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["diagnosis"] = explain_parse_error(filepath, exc)
        return out

    # Everything below assumes channels were recognised and scaled sensibly.
    # Say so plainly when they were not, rather than emitting confident numbers.
    findings = check_record(record, basename)

    summary = compute_event_summary(record)
    feeder = compute_feeder_summary(
        record,
        feeder_impedance_ohm_per_mile=feeder_z,
        hif_threshold_a=HIF_THRESHOLD_A,
    )
    tri = triage_event(summary, feeder, slow_trip_cycles=slow_trip_cycles)
    wso = classify_event(feeder, summary,
                         record_end_ms=float(record.duration_s()) * 1000.0)

    seq = feeder["reclose_sequence"]
    hif = feeder["hif_screen"]
    loc = feeder["fault_location"]

    start_dt = record.metadata.get("start_time")
    trig_dt = record.metadata.get("trigger_time_abs")

    shots = [{
        "shot_number":     s.shot_number,
        "element":         s.element,
        "operate_time_ms": round(s.operate_time_ms, 2) if s.operate_time_ms is not None else None,
        "dead_time_ms":    round(s.dead_time_ms, 1) if s.dead_time_ms is not None else None,
        "shot_type":       s.shot_type,
        "outcome":         s.outcome,
        "successful":      s.successful,
    } for s in seq.shots]

    est_miles = (loc or {}).get("estimated_miles")
    loc_valid, loc_note = _location_valid(est_miles, bool(hif.get("hif_suspect")))

    # Pickup settings are compared against RMS current over the fault window,
    # not the peak — a peak includes DC offset and would overstate the multiple.
    fault_rms = 0.0
    if summary.get("fault_inception_s") is not None:
        fi = int(np.searchsorted(record.time, summary["fault_inception_s"]))
        end = min(fi + record.samples_per_cycle(), len(record.time))
        for name, data in record.analog_channels.items():
            if name.upper() in ("IA", "IB", "IC") and end > fi:
                fault_rms = max(fault_rms, float(np.sqrt(np.mean(data[fi:end] ** 2))))

    peak_current = max(summary["max_currents"].values(), default=0.0)
    peak_phase_current = max(
        (v for k, v in summary["max_currents"].items() if k.upper() in ("IA", "IB", "IC")),
        default=0.0,
    )

    out.update({
        "ok":                True,
        "event_id":          os.path.splitext(basename)[0],
        "device_id":         summary["device_id"],
        "feeder":            summary["station"],      # CFG station field carries the feeder
        "timestamp":         start_dt.isoformat() if isinstance(start_dt, datetime) else None,
        "trigger_time_abs":  trig_dt.isoformat() if isinstance(trig_dt, datetime) else None,
        "sample_rate_hz":    record.sample_rate,
        "line_freq_hz":      record.line_freq(),
        "duration_s":        round(record.duration_s(), 4),
        "n_analog":          summary["n_analog"],
        "n_digital":         summary["n_digital"],

        # Fault characterisation
        "fault_type":        summary["fault_type"],
        "fault_inception_s": summary["fault_inception_s"],
        "trip_time_s":       summary["trip_time_s"],
        "trip_channel":      summary["trip_channel"],
        "trip_delay_ms":     round(summary["trip_delay_ms"], 2) if summary["trip_delay_ms"] is not None else None,
        "trip_delay_cycles": (round(summary["trip_delay_ms"] * record.line_freq() / 1000.0, 2)
                              if summary["trip_delay_ms"] is not None else None),
        "fault_current_rms_a":  round(fault_rms, 1),
        "peak_current_a":       round(peak_current, 1),
        "peak_phase_current_a": round(peak_phase_current, 1),
        "peak_residual_a":      round(summary["max_currents"].get("IN", 0.0), 1),

        # Recloser sequence
        "total_shots":       seq.total_shots,
        "locked_out":        bool(seq.locked_out),
        "sequence_outcome":  seq.final_outcome,
        "permanence":        seq.fault_type,
        "shots":             shots,
        "first_operate_ms":  shots[0]["operate_time_ms"] if shots else None,
        "max_dead_time_ms":  max((s["dead_time_ms"] for s in shots
                                  if s["dead_time_ms"] is not None), default=None),

        # Screens
        "hif_suspect":       bool(hif.get("hif_suspect")),
        "hif_delta_a":       hif.get("delta_current_a"),
        "faulted_phase":     (loc or {}).get("faulted_phase"),
        "est_miles":         est_miles,
        "est_miles_valid":   loc_valid,
        "est_miles_note":    loc_note,
        "z_fault_ohm":       (loc or {}).get("z_fault_ohm"),

        # Triage + WSO
        "priority":          tri["priority"],
        "flags":             tri["flags"],
        "flag_labels":       tri["labels"],
        "flag_reasons":      tri["reasons"],
        "triage_line":       tri["summary_line"],
        "wso_class":         wso,
        "findings":          findings,
        "worst_finding":     worst_level(findings),
    })

    if wave_opts:
        try:
            out["wave"] = extract_waveform(record, summary, feeder, *wave_opts)
        except Exception as exc:                   # noqa: BLE001 — a plot is optional
            out["wave"] = {}
            out["wave_error"] = f"{type(exc).__name__}: {exc}"

    cat = _catalog(*settings_opts) if settings_opts else None
    if cat is not None and fault_rms > 0:
        relay = cat.lookup(summary.get("device_id", ""))
        if relay is not None:
            ev = relay.evaluate(fault_rms, kind="phase")
            ev["relay_name"] = relay.relay_name
            ev["template"] = relay.template.raw if relay.template else None
            out["settings_eval"] = ev

            # Settings turn a guess into an answer. A ride-through whose current
            # cannot reach the EPSS pickup either is not a candidate at all.
            if out["wso_class"] == EPSS_CANDIDATE and ev["resolved"]:
                if ev["converts_under_epss"]:
                    out["settings_verdict"] = "confirmed"
                elif ev["epss"]["picks_up"] is False:
                    out["wso_class"] = NOT_EXPOSED
                    out["settings_verdict"] = "ruled_out"
                else:
                    out["settings_verdict"] = "trips_either_way"

    try:
        out["detail"] = extract_report_detail(record, summary, feeder)
    except Exception as exc:                       # noqa: BLE001 — detail is additive
        out["detail"] = {}
        out["detail_error"] = f"{type(exc).__name__}: {exc}"
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _counter(events, key) -> dict:
    return dict(Counter(e[key] for e in events if e.get(key) is not None))


ALL_STATIONS = "All substations"


def aggregate_by_station(events: list, registry: dict, epss_tiers: list,
                         response_hours: float, net=None) -> dict:
    """
    One aggregate per substation, plus one for the whole fleet.

    The dashboard's tiles, hero and charts render an aggregate; scoping them to
    a substation means handing them a different one, not re-deriving the maths
    in JavaScript. Run `aggregate` over the full set first — it is what stamps
    zone and customers_affected onto each event.
    """
    out = {ALL_STATIONS: aggregate(events, registry, epss_tiers,
                                   response_hours, net)}
    for station in sorted({e.get("station", "") for e in events if e.get("station")}):
        subset = [e for e in events if e.get("station") == station]
        out[station] = aggregate(subset, registry, epss_tiers, response_hours, net)
    return out


def aggregate_by_feeder(events: list, registry: dict, epss_tiers: list,
                        response_hours: float, net=None) -> dict:
    """
    The same, one per feeder — the scope a protection engineer actually works
    in. Reclose shots, clearing time, fault mix and the triage backlog only
    mean something against a circuit; across the fleet they are an average of
    thirteen unrelated ones.
    """
    out = {}
    for feeder in sorted({e.get("feeder", "") for e in events if e.get("feeder")}):
        subset = [e for e in events if e.get("feeder") == feeder]
        out[feeder] = aggregate(subset, registry, epss_tiers, response_hours, net)
    return out


def aggregate(events: list, registry: dict, epss_tiers: list,
              response_hours: float, net=None) -> dict:
    """
    Roll the per-event table into the summaries the dashboard renders.

    With a topology loaded, `customers_served` on a device row is that device's
    own section, and the customers an event actually drops is the whole subtree
    below it — a feeder-head trip takes out every recloser under it too. Without
    one there is no tree to walk, so the two are the same number.
    """
    total = len(events)

    by_zone = defaultdict(lambda: {
        "zone": "", "risk_tiers": set(), "devices": set(), "customers_served": 0,
        "events": 0, "permanent": 0, "wso_exposed": 0, "not_exposed": 0,
        "epss_candidate": 0, "indeterminate": 0,
        "priority_1": 0, "priority_2": 0, "priority_3": 0,
    })
    by_device = defaultdict(lambda: {
        "device_id": "", "feeder": "", "zone": "UNREGISTERED", "risk_tier": None,
        "customers_served": 0, "customers_affected": 0,
        "events": 0, "wso_exposed": 0, "permanent": 0,
        "priority_1": 0, "flags": Counter(),
    })
    by_day = Counter()
    unmatched_ids = set()

    for e in events:
        dev = lookup_device(registry, e.get("device_id", ""), e.get("feeder", ""))
        zone = dev["zone"] if dev else "UNREGISTERED"
        e["zone"] = zone
        e["risk_tier"] = dev["risk_tier"] if dev else None
        e["customers_served"] = dev["customers_served"] if dev else 0
        did = dev["device_id"] if dev else e.get("device_id", "")
        e["customers_affected"] = (net.customers_below(did, registry)
                                   if net is not None and did in net
                                   else e["customers_served"])
        e["station"] = dev["station"] if dev else ""
        e["epss_zone"] = bool(dev and dev["risk_tier"] in epss_tiers)
        if dev is None and e.get("device_id"):
            unmatched_ids.add(e["device_id"])

        z = by_zone[zone]
        z["zone"] = zone
        z["events"] += 1
        if dev:
            if dev["device_id"] not in z["devices"]:
                z["devices"].add(dev["device_id"])
                z["customers_served"] += dev["customers_served"]
            z["risk_tiers"].add(dev["risk_tier"])
        z[e["wso_class"].lower()] += 1
        z[f"priority_{e['priority']}"] += 1

        d = by_device[e.get("device_id") or e["file"]]
        d["device_id"] = e.get("device_id", "")
        d["feeder"] = e.get("feeder", "")
        d["zone"] = zone
        d["risk_tier"] = e["risk_tier"]
        d["customers_served"] = e["customers_served"]
        d["customers_affected"] = e["customers_affected"]
        d["events"] += 1
        if e["wso_class"] == "WSO_EXPOSED":
            d["wso_exposed"] += 1
        if e["wso_class"] == "PERMANENT":
            d["permanent"] += 1
        if e["priority"] == 1:
            d["priority_1"] += 1
        d["flags"].update(e["flags"])

        if e.get("timestamp"):
            by_day[e["timestamp"][:10]] += 1

    # Finalise zone rows — customer-hours follow wso_impact.py: exposure only
    # counts in zones whose risk tier actually receives EPSS treatment.
    zones = {}
    sys_cust_hours = 0.0
    for name, z in sorted(by_zone.items()):
        tiers = sorted(z["risk_tiers"])
        epss_active = any(t in epss_tiers for t in tiers)
        exposed = z["wso_exposed"] if epss_active else 0
        cust_hours = exposed * z["customers_served"] * response_hours
        sys_cust_hours += cust_hours
        zones[name] = {
            "zone": name,
            "risk_tiers": tiers,
            "epss_active": epss_active,
            "device_count": len(z["devices"]),
            "customers_served": z["customers_served"],
            "events": z["events"],
            "permanent": z["permanent"],
            "wso_exposed": z["wso_exposed"],
            "wso_exposed_counted": exposed,
            "not_exposed": z["not_exposed"],
            "epss_candidate": z["epss_candidate"],
            "indeterminate": z["indeterminate"],
            "priority_1": z["priority_1"],
            "priority_2": z["priority_2"],
            "priority_3": z["priority_3"],
            "est_customer_hours_per_wso_day": round(cust_hours, 1),
        }

    devices = []
    for d in by_device.values():
        d = dict(d)
        d["flags"] = dict(d["flags"])
        devices.append(d)
    devices.sort(key=lambda x: (-x["wso_exposed"], -x["events"], x["device_id"]))

    wso_counts = _counter(events, "wso_class")
    exposed_total = wso_counts.get("WSO_EXPOSED", 0)

    trip_delays = [e["trip_delay_ms"] for e in events if e.get("trip_delay_ms") is not None]
    miles = [e["est_miles"] for e in events if e.get("est_miles_valid")]

    return {
        "totals": {
            "events": total,
            "priority_1": sum(1 for e in events if e["priority"] == 1),
            "priority_2": sum(1 for e in events if e["priority"] == 2),
            "priority_3": sum(1 for e in events if e["priority"] == 3),
            "permanent": wso_counts.get("PERMANENT", 0),
            "wso_exposed": exposed_total,
            "not_exposed": wso_counts.get("NOT_EXPOSED", 0),
            "epss_candidate": wso_counts.get("EPSS_CANDIDATE", 0),
            "indeterminate": wso_counts.get("INDETERMINATE", 0),
            "wso_exposed_pct": round(100.0 * exposed_total / total, 1) if total else 0.0,
            "locked_out": sum(1 for e in events if e["locked_out"]),
            "hif_suspect": sum(1 for e in events if e["hif_suspect"]),
            "no_trip": sum(1 for e in events if "no_trip" in e["flags"]),
            "customers_covered": sum(z["customers_served"] for z in zones.values()),
            "est_customer_hours_per_wso_day": round(sys_cust_hours, 1),
            "median_trip_delay_ms": round(float(sorted(trip_delays)[len(trip_delays) // 2]), 1)
                                    if trip_delays else None,
            "median_est_miles": round(float(sorted(miles)[len(miles) // 2]), 2) if miles else None,
            "located_events": len(miles),
            "location_suppressed": sum(1 for e in events
                                       if e.get("est_miles") is not None and not e.get("est_miles_valid")),
        },
        "by_fault_type": _counter(events, "fault_type"),
        "by_wso_class": wso_counts,
        "by_priority": {str(k): v for k, v in sorted(_counter(events, "priority").items())},
        "by_shots": {str(k): v for k, v in sorted(_counter(events, "total_shots").items())},
        "by_flag": dict(Counter(f for e in events for f in e["flags"]).most_common()),
        "by_faulted_phase": _counter(events, "faulted_phase"),
        "by_day": dict(sorted(by_day.items())),
        "zones": zones,
        "devices": devices,
        "unmatched_device_ids": sorted(unmatched_ids),
        "trip_delays_ms": trip_delays,
        "est_miles": miles,
    }


# ---------------------------------------------------------------------------
# Ground-truth validation (only when fleet_truth.json is present)
# ---------------------------------------------------------------------------

def _grouping_accuracy(events: list, truth: dict) -> Optional[dict]:
    """
    How well the rebuilt grouping matches the sets the generator actually
    produced. Only meaningful on generated data — real files carry no answer —
    so this is a regression guard, not evidence the algorithm is right.
    """
    want = defaultdict(set)
    for t in truth.values():
        if t.get("incident_id"):
            want[t["incident_id"]].add(t["event_id"])
    if not want:
        return None

    got = defaultdict(set)
    for e in events:
        if e.get("incident_id"):
            got[e["incident_id"]].add(e["event_id"])

    exact = checked = 0
    for e in events:
        t = truth.get(e["event_id"])
        if t is None or not t.get("incident_id"):
            continue
        checked += 1
        if got.get(e.get("incident_id"), set()) == want[t["incident_id"]]:
            exact += 1
    return {
        "checked": checked,
        "incidents_expected": len(want),
        "incidents_found": len(got),
        "events_grouped_correctly_pct": (round(100.0 * exact / checked, 1)
                                         if checked else 0.0),
    }


def validate(events: list, truth_path: str) -> Optional[dict]:
    try:
        with open(truth_path, encoding="utf-8") as fh:
            truth = {t["event_id"]: t for t in json.load(fh)["events"]}
    except (OSError, json.JSONDecodeError, KeyError):
        return None

    checked = fault_ok = wso_ok = shots_ok = flags_ok = 0
    mismatches = []
    for e in events:
        t = truth.get(e["event_id"])
        if t is None:
            continue
        checked += 1
        f_ok = e["fault_type"] == t["expect_fault"]
        w_ok = e["wso_class"] == t["expect_wso"]
        s_ok = e["total_shots"] == t["expect_shots"]
        missing = sorted(set(t["expect_flags"]) - set(e["flags"]))
        fault_ok += f_ok
        wso_ok += w_ok
        shots_ok += s_ok
        flags_ok += not missing
        e["expected_fault_type"] = t["expect_fault"]
        e["expected_wso_class"] = t["expect_wso"]
        e["scenario"] = t["scenario"]
        if not (f_ok and w_ok and s_ok and not missing):
            mismatches.append({
                "event_id": e["event_id"], "scenario": t["scenario"],
                "expected_fault": t["expect_fault"], "got_fault": e["fault_type"],
                "expected_wso": t["expect_wso"], "got_wso": e["wso_class"],
                "expected_shots": t["expect_shots"], "got_shots": e["total_shots"],
                "missing_flags": missing,
            })

    pct = lambda n: round(100.0 * n / checked, 1) if checked else 0.0
    return {
        "grouping": _grouping_accuracy(events, truth),
        "checked": checked,
        "fault_type_match_pct": pct(fault_ok),
        "wso_class_match_pct": pct(wso_ok),
        "shot_count_match_pct": pct(shots_ok),
        "triage_flag_recall_pct": pct(flags_ok),
        "mismatches": mismatches,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

_CSV_COLUMNS = [
    "event_id", "timestamp", "device_id", "feeder", "zone", "risk_tier",
    "customers_served", "customers_affected", "fault_type", "faulted_phase",
    "priority", "wso_class",
    "total_shots", "locked_out", "trip_delay_ms", "trip_delay_cycles",
    "peak_phase_current_a", "peak_residual_a", "est_miles", "est_miles_valid", "hif_suspect",
    "sample_rate_hz", "duration_s", "sequence_outcome", "flags",
]


def write_csv(events: list, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for e in events:
            row = dict(e)
            row["flags"] = "|".join(e["flags"])
            w.writerow(row)


def print_summary(result: dict) -> None:
    a = result["aggregates"]
    t = a["totals"]
    W = 68
    print()
    print("=" * W)
    print("  FLEET EVENT ANALYSIS")
    print("=" * W)
    print(f"  Folder        : {result['folder']}")
    print(f"  Events parsed : {t['events']}"
          + (f"   ({len(result['parse_errors'])} parse errors)" if result["parse_errors"] else ""))
    print(f"  Elapsed       : {result['elapsed_s']:.1f} s")

    incs = result.get("incidents") or []
    if incs:
        multi = sum(1 for i in incs if i["record_count"] > 1)
        print(f"  Incidents     : {len(incs)}   "
              f"({multi} seen by more than one device)")

    print()
    print("  FAULT MIX")
    for k, v in sorted(a["by_fault_type"].items(), key=lambda kv: -kv[1]):
        print(f"    {k:<8s} {v:4d}   {100*v/t['events']:5.1f}%")

    print()
    print("  TRIAGE BACKLOG")
    print(f"    Priority 1 (immediate) : {t['priority_1']:4d}")
    print(f"    Priority 2 (weekly)    : {t['priority_2']:4d}")
    print(f"    Priority 3 (archive)   : {t['priority_3']:4d}")
    if a["by_flag"]:
        print("    Flags: " + "  ".join(f"{k}={v}" for k, v in a["by_flag"].items()))

    print()
    print("  WHAT EACH EVENT BECOMES ON AN EPSS DAY")
    print(f"    Rode through, may trip under EPSS : {t.get('epss_candidate', 0):4d}"
          "  ← NEW outage, needs pickup settings to confirm")
    print(f"    Momentary → sustained             : {t['wso_exposed']:4d}  ({t['wso_exposed_pct']}%)")
    print(f"    Already sustained (lockout)       : {t['permanent']:4d}")
    print(f"    Unknown (record ends too early)   : {t.get('indeterminate', 0):4d}")
    print(f"    No change                         : {t['not_exposed']:4d}")
    if t["est_customer_hours_per_wso_day"]:
        print(f"    Est. cust-hrs/day : {t['est_customer_hours_per_wso_day']:,.0f}"
              f"  ({t['customers_covered']:,} customers covered)")

    print()
    print("  BY ZONE")
    for name, z in a["zones"].items():
        tiers = "/".join(str(x) for x in z["risk_tiers"]) or "—"
        epss = "EPSS active" if z["epss_active"] else "EPSS inactive"
        print(f"    {name:<14s} tier {tiers:<4s} {epss:<14s} "
              f"events={z['events']:3d}  exposed={z['wso_exposed']:3d}  P1={z['priority_1']:3d}")

    incs = result.get("incidents") or []
    if incs:
        restored = [i for i in incs if i["restored"]]
        both = [i for i in incs if i["upstream_also_tripped"]]
        print()
        print("  INCIDENTS")
        print(f"    Seen by one device only     : "
              f"{sum(1 for i in incs if i['record_count'] == 1):4d}")
        print(f"    Upstream device held        : "
              f"{sum(1 for i in incs if i['devices_held']):4d}"
              "   coordination worked")
        print(f"    Two devices on one path     : {len(both):4d}"
              "   review — over-trip or fuse saving")
        print(f"    Locked out                  : "
              f"{sum(1 for i in incs if i['locked_out']):4d}")
        if restored:
            med = sorted(i["restore_delay_s"] for i in restored
                         if i["restore_delay_s"] is not None)
            tail = f"   median {med[len(med) // 2]:.0f} s to a tie" if med else ""
            print(f"    Restored through a tie      : {len(restored):4d}{tail}")

    skew = result.get("clock_suspects") or []
    if skew:
        print()
        print(f"  CLOCK SKEW — {len(skew)} pair(s) share a path and a fault type "
              "but miss the window")
        for r in skew[:3]:
            print(f"    {r['devices'][0]} / {r['devices'][1]}: "
                  f"{r['gap_s']:.1f} s apart, both {r['fault_type']}")
        print("    Widen --incident-window-s, or check the relay clocks.")

    v = result.get("validation")
    if v:
        print()
        print("  DETECTOR AGREEMENT vs GROUND TRUTH")
        print(f"    Fault type   : {v['fault_type_match_pct']}%")
        print(f"    WSO class    : {v['wso_class_match_pct']}%")
        print(f"    Shot count   : {v['shot_count_match_pct']}%")
        print(f"    Triage flags : {v['triage_flag_recall_pct']}% recall")
        g = v.get("grouping")
        if g:
            print(f"    Incident grouping : {g['events_grouped_correctly_pct']}%  "
                  f"({g['incidents_found']} found / {g['incidents_expected']} real)")
        if v["mismatches"]:
            print(f"    {len(v['mismatches'])} mismatch(es) — see fleet_analysis.json")
    print("=" * W)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def find_sidecar(folder: str, names: tuple) -> Optional[str]:
    """
    Find a file that belongs to a set of events but does not live among them.

    Pointing straight at the events folder is what the docs tell people to do,
    and the registry and topology sit beside it, not in it — so look one level
    up when this folder IS an events folder. Matching on the folder name keeps
    that from dragging in an unrelated parent's files.
    """
    search = [folder]
    if os.path.basename(os.path.normpath(folder)) in (EVENTS_DIRNAME, "events"):
        search.append(os.path.dirname(os.path.abspath(folder)))
    for base in search:
        for cand in names:
            c = os.path.join(base, cand)
            if os.path.isfile(c):
                return c
    return None


def load_network(folder: str, path: Optional[str] = None):
    """The topology beside a folder of events, or None. Never raises."""
    p = path or find_sidecar(folder, ("topology.csv",))
    if not p:
        return None, None
    try:
        from .topology import load_topology
        return load_topology(p), p
    except (OSError, ValueError):
        return None, None


def resolve_inputs(folder: str, devices_arg: Optional[str]):
    """
    A fleet dir holds its records in a subfolder and carries a registry beside
    them; a plain folder of COMTRADE files — which is what a real SUBNET pull
    looks like — does not. `events/` is still accepted for folders built before
    the corpus was organised into incidents.
    """
    events_dir = folder
    for cand in (EVENTS_DIRNAME, "events"):
        p = os.path.join(folder, cand)
        if os.path.isdir(p):
            events_dir = p
            break

    devices_path = devices_arg or find_sidecar(folder, ("fleet_devices.csv",
                                                        "devices.csv"))
    topo_path = find_sidecar(folder, ("topology.csv",))

    truth_path = find_sidecar(folder, ("fleet_truth.json",))
    return events_dir, devices_path, topo_path, truth_path


def main():
    cfg = load_config()
    p = argparse.ArgumentParser(
        prog="fleet-analyze",
        description="Batch-analyze a folder of COMTRADE events into one dataset")
    p.add_argument("folder", help="Fleet folder (with events/) or a folder of COMTRADE files")
    p.add_argument("--devices", metavar="CSV", help="Device registry CSV (auto-detected if absent)")
    p.add_argument("--feeder-z", type=float, default=DEFAULT_FEEDER_Z, metavar="OHM/MI",
                   help=f"Feeder Z1 magnitude for fault location (default {DEFAULT_FEEDER_Z})")
    p.add_argument("--response-hours", type=float,
                   default=cfg.get("wso", {}).get("avg_response_hours", 2.0), metavar="HRS",
                   help="Crew response time for customer-hour estimates")
    p.add_argument("--incident-window-s", type=float,
                   default=cfg.get("incidents", {}).get("window_s", 2.0),
                   metavar="SEC",
                   help="Records this far apart on one path are the same fault. "
                        "Widen it when relay clocks are known to drift.")
    p.add_argument("--epss-tiers", type=int, nargs="+",
                   default=cfg.get("wso", {}).get("epss_tiers", [2, 3]), metavar="N",
                   help="Risk tiers receiving EPSS treatment")
    p.add_argument("--slow-trip-cycles", type=float,
                   default=cfg.get("triage", {}).get("slow_trip_cycles", 10.0), metavar="CYC",
                   help="Trip-delay threshold for the slow_trip flag")
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 1),
                   help="Parallel worker processes")
    p.add_argument("--settings", metavar="FILE",
                   help="SUBNET relay settings export (.csv or .xlsx). Enables the "
                        "normal-vs-EPSS pickup comparison that confirms ride-throughs.")
    p.add_argument("--settings-primary", action="store_true",
                   help="Pickups in the settings export are already primary amps "
                        "(default: secondary, converted using CTR)")
    p.add_argument("--no-waveforms", action="store_true",
                   help="Skip waveform extraction (smaller JSON; the dashboard's "
                        "inline oscillography viewer is then unavailable)")
    p.add_argument("--waveform-buckets", type=int, nargs=2, default=[180, 280],
                   metavar=("FULL", "ZOOM"),
                   help="Envelope resolution for the full-record and fault-window "
                        "views (default 180 280)")
    p.add_argument("--out-dir", metavar="DIR", help="Output directory (default <folder>/analysis)")
    args = p.parse_args()

    if not os.path.isdir(args.folder):
        print(f"Error: folder not found — {args.folder}", file=sys.stderr)
        sys.exit(1)

    events_dir, devices_path, topo_path, truth_path = resolve_inputs(
        args.folder, args.devices)
    files = _find_cfg(events_dir)
    if not files:
        print(f"No COMTRADE files found under: {events_dir}", file=sys.stderr)
        sys.exit(1)

    registry = load_registry(devices_path) if devices_path else {}
    net = None
    if topo_path:
        from .topology import load_topology
        net = load_topology(topo_path)
    print(f"Events   : {len(files)} file(s) under {events_dir}")
    print(f"Registry : {len(registry)} device(s)"
          + (f" from {devices_path}" if devices_path else " — none, zones will be UNREGISTERED"))
    if net is not None:
        print(f"Topology : {len(net.feeders())} feeder(s), {len(net.ties())} tie(s) "
              f"from {topo_path}")

    t0 = time.time()
    wave_opts = None if args.no_waveforms else tuple(args.waveform_buckets)
    settings_opts = ((args.settings, args.settings_primary)
                     if getattr(args, 'settings', None) else None)
    payload = [(f, args.feeder_z, args.slow_trip_cycles, wave_opts, settings_opts)
               for f in files]
    if args.jobs > 1 and len(files) > 4:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            results = list(pool.map(analyze_one, payload, chunksize=4))
    else:
        results = [analyze_one(x) for x in payload]
    elapsed = time.time() - t0

    events = [r for r in results if r["ok"]]
    parse_errors = [{"file": r["file"], "error": r["error"]} for r in results if not r["ok"]]
    for r in parse_errors:
        print(f"  ERROR {r['file']}: {r['error']}", file=sys.stderr)

    events.sort(key=lambda e: (e.get("timestamp") or "", e["event_id"]))
    incidents = group_events(events, net, args.incident_window_s)
    skew = clock_suspects(events, net, args.incident_window_s)
    by_station = aggregate_by_station(events, registry, args.epss_tiers,
                                      args.response_hours, net)
    by_feeder = aggregate_by_feeder(events, registry, args.epss_tiers,
                                    args.response_hours, net)
    aggregates = by_station[ALL_STATIONS]
    validation = validate(events, truth_path) if truth_path else None

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "folder": os.path.abspath(args.folder),
        "events_dir": os.path.abspath(events_dir),
        "registry_path": devices_path,
        "topology_path": topo_path,
        # The nodes themselves, not just the path — the dashboard is a single
        # self-contained file and cannot go back to the CSV to draw a one-line.
        "topology": ([{"node_id": n.node_id, "feeder": n.feeder, "kind": n.kind,
                       "parent": n.parent, "tie_to": n.tie_to,
                       "cabinet": n.cabinet, "model": n.model,
                       # for the contingency view: what this section serves.
                       # Devices with no events are still part of an outage.
                       "customers": ((registry.get(_normalize(n.node_id)) or {})
                                     .get("customers_served", 0))}
                      for n in net.nodes()] if net is not None else []),
        "incidents": incidents,
        "clock_suspects": skew,
        "aggregates_by_station": by_station,
        "aggregates_by_feeder": by_feeder,
        "settings": {
            "feeder_z_ohm_per_mile": args.feeder_z,
            "epss_tiers": args.epss_tiers,
            "epss_max_shots": 0,
            "response_hours": args.response_hours,
            "slow_trip_cycles": args.slow_trip_cycles,
            "hif_threshold_a": HIF_THRESHOLD_A,
            "waveforms": bool(wave_opts),
        },
        "triage_rules":  rule_table(args.slow_trip_cycles),
        "epss_classes":  class_table(),
        "files_found": len(files),
        "parse_errors": parse_errors,
        "elapsed_s": round(elapsed, 2),
        "events": events,
        "aggregates": aggregates,
        "validation": validation,
    }

    out_dir = args.out_dir or os.path.join(args.folder, "analysis")
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "fleet_analysis.json")
    csv_path = os.path.join(out_dir, "fleet_events.csv")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)
    write_csv(events, csv_path)

    print_summary(result)
    print()
    print(f"  JSON → {json_path}")
    print(f"  CSV  → {csv_path}")
    print()
    print("Next:")
    print(f"  python3 fleet_dashboard.py {json_path}")


if __name__ == "__main__":
    main()
