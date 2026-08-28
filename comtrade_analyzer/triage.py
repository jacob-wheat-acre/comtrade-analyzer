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
    "no_trip":        (2, "Rode Through",
                       "Fault current recorded but this device did not trip — normally a downstream "
                       "fuse cleared it, so only the lateral went out. Under more sensitive EPSS "
                       "settings the same fault may trip this device instead, taking out everything "
                       "downstream of it with no reclose. Check coordination before a WSO day."),
    "slow_trip":      (1, "Slow Trip",
                       "Trip time exceeds configured threshold — possible coordination failure, "
                       "CT saturation, or relay setting drift"),
    "llg_fault":      (2, "LLG Fault",
                       "Double line-to-ground fault — two conductors involved, elevated damage potential"),
    "multiple_shots": (2, "Multiple Reclose Shots",
                       "Recloser operated 2+ times before clearing — semi-permanent fault "
                       "(tree contact, damaged insulator, failing equipment)"),
}


# flag_key → how the rule is evaluated, and where to change it. Kept beside
# _FLAGS so the two cannot drift; the dashboard renders this rather than
# carrying its own copy.
_TRIGGERS = {
    "hif_suspect":    ("Post-fault current rise stays below the HIF screen threshold",
                       "hif_threshold_a (feeder analysis, default 50 A)"),
    "lockout":        ("Recloser reached lockout — a LOCK/86 channel asserted, or the "
                       "final trip had no reclose", "—"),
    "3ph_fault":      ("Fault classified 3PH — all three phase currents elevated "
                       "(smallest > 0.7 x largest)", "—"),
    "no_trip":        ("Fault inception detected but no TRIP channel rising edge",
                       "device pickup settings (normal vs EPSS)"),
    "slow_trip":      ("Fault inception → trip exceeds the slow-trip threshold",
                       "config.json → triage.slow_trip_cycles"),
    "llg_fault":      ("Fault classified LLG — two phases elevated with measurable "
                       "zero-sequence current", "—"),
    "multiple_shots": ("Two or more reclose shots before clearing, without lockout", "—"),
}


def rule_table(slow_trip_cycles: float = 10.0, line_freq_hz: float = _LINE_FREQ_HZ) -> list:
    """
    The triage rules as data, for display and for export to the dashboard.

    Returns one dict per flag: key, priority, label, description, trigger and
    the setting that tunes it (or '—' when the rule has no threshold).
    """
    out = []
    for key, (priority, label, note) in _FLAGS.items():
        trigger, setting = _TRIGGERS.get(key, ("", "—"))
        if key == "slow_trip":
            ms = slow_trip_cycles * (1000.0 / line_freq_hz)
            trigger += f" ({slow_trip_cycles:g} cycles = {ms:.0f} ms at {line_freq_hz:g} Hz)"
        out.append({
            "key": key, "priority": priority, "label": label,
            "description": note, "trigger": trigger, "setting": setting,
        })
    out.sort(key=lambda r: (r["priority"], r["label"]))
    return out


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
    evidence: dict = {}          # flag_key → what actually triggered it
    slow_trip_ms = slow_trip_cycles * (1000.0 / _LINE_FREQ_HZ)

    # ── Priority 1 checks ────────────────────────────────────────────────────

    hif = (feeder_data or {}).get("hif_screen", {})
    if hif.get("hif_suspect"):
        flags.append("hif_suspect")
        evidence["hif_suspect"] = (
            f"current rise {hif.get('delta_current_a', '?')} A is below the "
            f"{hif.get('threshold_a', '?')} A screen threshold")

    seq = (feeder_data or {}).get("reclose_sequence")
    if seq is not None and seq.locked_out:
        flags.append("lockout")
        evidence["lockout"] = f"locked out after {seq.total_shots} shot(s)"

    if summary.get("fault_type") == "3PH":
        flags.append("3ph_fault")
        evidence["3ph_fault"] = "classified 3PH"

    # A balanced current step with the voltage still up is load being picked
    # up, not a fault — a tie closing onto a restored section is the usual
    # cause. Flagging it "rode through" would call every FLISR restoration a
    # coordination problem.
    is_load_step = summary.get("fault_type") == "LOAD"

    if (summary.get("fault_inception_s") is not None
            and summary.get("trip_time_s") is None
            and not is_load_step):
        flags.append("no_trip")
        evidence["no_trip"] = (
            f"inception at {summary['fault_inception_s'] * 1000:.1f} ms, "
            f"no TRIP edge in the record")

    trip_ms = summary.get("trip_delay_ms")
    if trip_ms is not None and trip_ms > slow_trip_ms:
        flags.append("slow_trip")
        evidence["slow_trip"] = (
            f"{trip_ms:.1f} ms to trip, threshold {slow_trip_ms:.1f} ms "
            f"({slow_trip_cycles:g} cycles)")

    # ── Priority 2 checks ────────────────────────────────────────────────────

    if summary.get("fault_type") == "LLG":
        flags.append("llg_fault")
        evidence["llg_fault"] = "classified LLG"

    if seq is not None and not seq.locked_out and seq.total_shots >= 2:
        flags.append("multiple_shots")
        evidence["multiple_shots"] = f"{seq.total_shots} shots before clearing"

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

    reasons = [{
        "key":      f,
        "priority": _FLAGS[f][0],
        "label":    _FLAGS[f][1],
        "note":     _FLAGS[f][2],
        "evidence": evidence.get(f, ""),
        "decisive": _FLAGS[f][0] == priority,
    } for f in flags]

    return {
        "priority":     priority,
        "flags":        flags,
        "labels":       labels,
        "notes":        notes,
        "reasons":      reasons,
        "summary_line": summary_line,
    }
