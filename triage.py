"""
triage.py — Automated event triage for COMTRADE relay event analysis.

Each event is assigned a review priority and a list of flags explaining why.

Priority 1 — Immediate review (same day):  rare or safety-critical events
Priority 2 — Routine review (weekly batch): elevated risk, less urgent
Priority 3 — Archive only:                  routine SLG / successful reclose
"""

from __future__ import annotations

from typing import Optional

_LINE_FREQ_HZ = 60.0

# flag_key → (priority, short label, one-line description)
_FLAGS = {
    "hif_suspect":    (1, "HIF Suspect",
                       "High-impedance fault suspected — potential downed conductor (public safety)"),
    "lockout":        (1, "Lockout",
                       "Recloser locked out — permanent fault, likely equipment damage"),
    "3ph_fault":      (1, "Three-Phase Fault",
                       "Three-phase fault — rare on distribution, usually equipment failure not weather"),
    "no_trip":        (1, "No Trip Detected",
                       "Fault inception found but no TRIP bit ever asserted — possible relay misoperation "
                       "or upstream device cleared instead"),
    "slow_trip":      (1, "Slow Trip",
                       "Trip time exceeds configured threshold — possible coordination failure, "
                       "CT saturation, or relay setting drift"),
    "llg_fault":      (2, "LLG Fault",
                       "Double line-to-ground fault — two conductors involved, elevated damage potential"),
    "multiple_shots": (2, "Multiple Reclose Shots",
                       "Recloser operated 2+ times before clearing — semi-permanent fault "
                       "(tree contact, damaged insulator, failing equipment)"),
}


def triage_event(
    summary: dict,
    feeder_data: Optional[dict] = None,
    slow_trip_cycles: float = 10.0,
) -> dict:
    """
    Assign a review priority and flag list to an event.

    Parameters
    ----------
    summary          : dict from compute_event_summary()
    feeder_data      : dict from compute_feeder_summary(), or None if feeder
                       analysis was not run (flags that require it are skipped)
    slow_trip_cycles : trip-delay threshold in cycles; events slower than this
                       receive the 'slow_trip' Priority-1 flag (default 10 = 167 ms)

    Returns
    -------
    dict with:
        priority     int           1, 2, or 3
        flags        list[str]     flag keys from _FLAGS
        labels       list[str]     short human-readable labels
        notes        list[str]     one descriptive sentence per flag
        summary_line str           single line for report headers / log output
    """
    flags: list[str] = []
    slow_trip_ms = slow_trip_cycles * (1000.0 / _LINE_FREQ_HZ)

    # ── Priority 1 checks ────────────────────────────────────────────────────

    hif = (feeder_data or {}).get("hif_screen", {})
    if hif.get("hif_suspect"):
        flags.append("hif_suspect")

    seq = (feeder_data or {}).get("reclose_sequence")
    if seq is not None and seq.locked_out:
        flags.append("lockout")

    if summary.get("fault_type") == "3PH":
        flags.append("3ph_fault")

    if (summary.get("fault_inception_s") is not None
            and summary.get("trip_time_s") is None):
        flags.append("no_trip")

    trip_ms = summary.get("trip_delay_ms")
    if trip_ms is not None and trip_ms > slow_trip_ms:
        flags.append("slow_trip")

    # ── Priority 2 checks ────────────────────────────────────────────────────

    if summary.get("fault_type") == "LLG":
        flags.append("llg_fault")

    if seq is not None and not seq.locked_out and seq.total_shots >= 2:
        flags.append("multiple_shots")

    # ── Assign priority ──────────────────────────────────────────────────────

    priority_of = {f: _FLAGS[f][0] for f in flags}
    if any(p == 1 for p in priority_of.values()):
        priority = 1
    elif any(p == 2 for p in priority_of.values()):
        priority = 2
    else:
        priority = 3

    labels = [_FLAGS[f][1] for f in flags]
    notes  = [_FLAGS[f][2] for f in flags]

    if priority == 1:
        summary_line = "PRIORITY 1 — IMMEDIATE REVIEW: " + " | ".join(labels)
    elif priority == 2:
        summary_line = "PRIORITY 2 — ROUTINE REVIEW: " + " | ".join(labels)
    else:
        summary_line = "PRIORITY 3 — ARCHIVE (no engineer review required)"

    return {
        "priority":     priority,
        "flags":        flags,
        "labels":       labels,
        "notes":        notes,
        "summary_line": summary_line,
    }
