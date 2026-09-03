#!/usr/bin/env python3
"""
diagnostics.py — Say plainly what is wrong with a COMTRADE file.

The synthetic fixtures are perfect; real relay exports are not. Channel names
vary by vendor and by engineer, CT ratios turn up applied or not applied, and a
record that parses cleanly can still be useless because it has no TRIP bit or
no pre-fault baseline. A batch that reports "0 events analyzed" with no reason
is the worst outcome, so every check below names the symptom, the evidence and
the fix.

Two entry points:

    check_record(record)  → list of findings for a parsed record
    explain_parse_error(path, exc) → a finding for a file that would not parse
"""

from __future__ import annotations

import os
from typing import List, Optional

import numpy as np

# Severity levels, worst first.
ERROR = "error"        # analysis cannot proceed or its output is meaningless
WARN = "warn"          # analysis runs, but part of it is unavailable or suspect
INFO = "info"          # worth knowing, not a problem

# Plausible ranges for distribution feeders. Outside these the scaling in the
# CFG is more likely wrong than the power system.
_MAX_PLAUSIBLE_AMPS = 100_000.0
_MIN_PLAUSIBLE_FAULT_AMPS = 20.0       # below this, values look like secondary
_MAX_PLAUSIBLE_VOLTS = 200_000.0


def _finding(level, code, message, detail="", fix=""):
    return {"level": level, "code": code, "message": message,
            "detail": detail, "fix": fix}


# ---------------------------------------------------------------------------
# Parse failures
# ---------------------------------------------------------------------------

def explain_parse_error(filepath: str, exc: Exception) -> dict:
    """Turn an exception from the parser into something actionable."""
    name = os.path.basename(filepath)
    kind = type(exc).__name__
    text = str(exc)
    base = os.path.splitext(filepath)[0]

    if isinstance(exc, FileNotFoundError) or "Cannot find" in text:
        missing = ".dat" if ".dat" in text.lower() else ".cfg"
        return _finding(
            ERROR, "missing_pair", f"{name}: the matching {missing} file is not there",
            f"A COMTRADE event is a pair. Looked beside {base}.",
            "Export both halves. If your tool wrote .DAT in capitals on a "
            "case-sensitive share, rename it to match the .cfg.")

    if isinstance(exc, UnicodeDecodeError):
        return _finding(
            ERROR, "encoding", f"{name}: the file is not UTF-8 text",
            text,
            "Usually a vendor export in a regional code page. Send this file "
            "to the maintainer — the parser can be taught the encoding.")

    if isinstance(exc, (ValueError, IndexError)):
        return _finding(
            ERROR, "cfg_format", f"{name}: the .cfg does not match the expected layout",
            f"{kind}: {text}",
            "Open the .cfg in a text editor and check line 2 — it should read "
            "like '13,7A,6D' (total, analog count, digital count). A mismatch "
            "between those counts and the channel lines that follow is the "
            "usual cause. Send the first 15 lines to the maintainer.")

    return _finding(
        ERROR, "parse_failed", f"{name}: could not be read", f"{kind}: {text}",
        "Send this file, or just its .cfg, to the maintainer.")


# ---------------------------------------------------------------------------
# Record checks
# ---------------------------------------------------------------------------

def _classify_channels(record):
    cur, volt, other = [], [], []
    for name in record.analog_channels:
        units = (record.analog_info[name].units or "").strip().upper()
        upper = name.upper()
        if units in ("A", "AMP", "AMPS", "KA") or any(
                k in upper for k in ("IA", "IB", "IC", "IN", "IG", "CURR")):
            cur.append(name)
        elif units in ("V", "KV", "VOLT", "VOLTS") or any(
                k in upper for k in ("VA", "VB", "VC", "VN", "VOLT")):
            volt.append(name)
        else:
            other.append(name)
    return cur, volt, other


def check_record(record, filename: str = "") -> List[dict]:
    """Findings for one parsed record, worst first."""
    out: List[dict] = []
    an = list(record.analog_channels)
    dg = list(record.digital_channels)
    cur, volt, other = _classify_channels(record)

    # ── The exact lookups the analysis performs ──────────────────────────────
    #
    # analysis._find_channel matches channel NAMES against fixed candidate
    # tuples; it does not consult units. A file whose units say "A" but whose
    # channels are called CH1_ANLG therefore parses, passes a units-based
    # check, and then silently classifies every event UNKNOWN. Mirror the real
    # lookup so the diagnosis agrees with the analysis.
    names_upper = {n.upper() for n in an}
    PHASE_CANDIDATES = {
        "A": ("IA", "Ia", "I_A", "I-A", "IA1"),
        "B": ("IB", "Ib", "I_B", "I-B", "IB1"),
        "C": ("IC", "Ic", "I_C", "I-C", "IC1"),
    }
    missing_phases = [ph for ph, cands in PHASE_CANDIDATES.items()
                      if not any(c.upper() in names_upper for c in cands)]
    if an and missing_phases:
        out.append(_finding(
            ERROR, "phase_currents_unnamed",
            f"Phase current channel(s) {', '.join(missing_phases)} not found by name",
            f"Channels present: {', '.join(an)}. "
            f"Fault classification looks for exactly: "
            f"{', '.join(PHASE_CANDIDATES['A'])} (and the B/C equivalents).",
            "Every event will classify as UNKNOWN until these resolve. The "
            "names come from the relay's export, so either set the channel "
            "names there, or add your naming to the candidate lists in "
            "analysis.py. Units alone are not enough — the lookup is by name."))

    # ── Channels the analysis depends on ─────────────────────────────────────
    if not an:
        out.append(_finding(
            ERROR, "no_analog", "No analog channels at all",
            "The record parsed but carries no analog data.",
            "Check the .cfg analog count on line 2."))
    elif not cur:
        out.append(_finding(
            ERROR, "no_current", "No current channels recognised",
            f"Channels present: {', '.join(an)}. "
            f"Units seen: {', '.join(sorted({(record.analog_info[c].units or '?') for c in an}))}",
            "Every fault calculation needs current. Names are matched on the "
            "keywords in config.json → channel_keywords.current (IA, IB, IC, "
            "IN, IG, CURR ...). Add your naming there — do not rename the "
            "relay's channels."))
    elif len(cur) < 3:
        out.append(_finding(
            WARN, "few_currents", f"Only {len(cur)} current channel(s) found: {', '.join(cur)}",
            "Fault classification compares the three phase currents.",
            "Expected IA, IB and IC. Extend config.json → channel_keywords."))

    if not volt:
        out.append(_finding(
            WARN, "no_voltage", "No voltage channels recognised",
            f"Channels present: {', '.join(an)}",
            "Phasor diagrams and the fault-location estimate need voltage. "
            "Fault classification and triage still work without it."))

    trip_like = [d for d in dg if "TRIP" in d.upper() or d.upper().startswith("TR")]
    if not dg:
        out.append(_finding(
            WARN, "no_digitals", "No digital channels",
            "Trip timing and the whole reclose sequence come from digitals.",
            "Without them every event looks like a ride-through, which will "
            "overstate the EPSS candidates. Enable digital channels in the export."))
    elif not trip_like:
        out.append(_finding(
            WARN, "no_trip_channel", "No TRIP channel recognised",
            f"Digitals present: {', '.join(dg)}",
            "Trip time, reclose sequence and EPSS classification all key off "
            "it. Add your naming to config.json → channel_keywords.trip."))

    if other:
        out.append(_finding(
            INFO, "unclassified_channels",
            f"{len(other)} channel(s) not recognised as current or voltage",
            ", ".join(other),
            "Harmless if they are frequency, power or spare channels."))

    # ── Scaling sanity ───────────────────────────────────────────────────────
    # A real export can carry a channel with no samples at all — np.max on an
    # empty array raises rather than returning zero, which took down the whole
    # folder run on one file.
    peaks = [float(np.max(np.abs(record.analog_channels[c])))
             for c in cur if len(record.analog_channels[c])]
    if peaks:
        peak = max(peaks)
        if peak > _MAX_PLAUSIBLE_AMPS:
            out.append(_finding(
                WARN, "current_too_large", f"Peak current {peak:,.0f} A is implausibly high",
                "Distribution fault current rarely exceeds 20 kA.",
                "Check the 'a' multiplier on the current channels in the .cfg, "
                "and whether a CT ratio has been applied twice."))
        elif 0 < peak < _MIN_PLAUSIBLE_FAULT_AMPS:
            out.append(_finding(
                WARN, "current_looks_secondary", f"Peak current is only {peak:.1f} A",
                "That is the range of CT *secondary* amps, not primary.",
                "If the export is in secondary, multiply by the CT ratio — "
                "either in the .cfg 'a' multiplier or in the relay's export "
                "settings. Fault-location and HIF thresholds assume primary amps."))

    if volt:
        vpeak = max(float(np.max(np.abs(record.analog_channels[v]))) for v in volt)
        if vpeak > _MAX_PLAUSIBLE_VOLTS:
            out.append(_finding(
                WARN, "voltage_too_large", f"Peak voltage {vpeak:,.0f} V is implausibly high",
                "Above transmission levels for a distribution record.",
                "Check the voltage channel multipliers and the PT ratio."))
        elif 0 < vpeak < 500:
            out.append(_finding(
                WARN, "voltage_looks_secondary", f"Peak voltage is only {vpeak:.0f} V",
                "That is PT secondary, not primary.",
                "Apply the PT ratio, or the fault-location estimate will be "
                "wrong by that factor."))

    # ── Record geometry ──────────────────────────────────────────────────────
    if record.sample_rate <= 0:
        out.append(_finding(
            ERROR, "no_sample_rate", "Sample rate is zero or missing",
            "Every per-cycle window depends on it.",
            "Check the sample-rate line of the .cfg (rate,end_sample)."))
    else:
        spc = record.samples_per_cycle()
        if spc < 8:
            out.append(_finding(
                WARN, "low_sample_rate",
                f"Only {spc} samples per cycle ({record.sample_rate:g} Hz)",
                "DFT phasors and RMS get coarse below about 8.",
                "Export at a higher sample rate if the relay allows it."))
        cycles = record.duration_s() * record.line_freq()
        if cycles < 3:
            out.append(_finding(
                WARN, "record_too_short", f"Record is only {cycles:.1f} cycles long",
                f"{record.duration_s() * 1000:.0f} ms total.",
                "Fault detection needs a pre-fault baseline of about three "
                "cycles. Lengthen the record window in the relay."))
        if record.trigger_index < 3 * spc:
            out.append(_finding(
                WARN, "little_prefault",
                f"Only {record.trigger_index / max(spc, 1):.1f} cycles before the trigger",
                "The pre-fault baseline is measured here.",
                "Increase the relay's pre-trigger (pre-fault) length to at "
                "least 3 cycles, ideally 5."))

    lf = record.line_freq()
    if lf not in (50.0, 60.0):
        out.append(_finding(
            WARN, "odd_line_freq", f"Line frequency reads {lf:g} Hz",
            "Expected 50 or 60.",
            "Check the line-frequency line of the .cfg."))

    order = {ERROR: 0, WARN: 1, INFO: 2}
    out.sort(key=lambda f: order[f["level"]])
    for f in out:
        f["file"] = filename or ""
    return out


def worst_level(findings: List[dict]) -> Optional[str]:
    for lvl in (ERROR, WARN, INFO):
        if any(f["level"] == lvl for f in findings):
            return lvl
    return None


def format_findings(findings: List[dict], indent: str = "  ") -> str:
    """Console rendering — the same text the dashboard shows."""
    mark = {ERROR: "[FAIL]", WARN: "[WARN]", INFO: "[ note]"}
    lines = []
    for f in findings:
        lines.append(f"{indent}{mark[f['level']]} {f['message']}")
        if f["detail"]:
            lines.append(f"{indent}         {f['detail']}")
        if f["fix"]:
            lines.append(f"{indent}         → {f['fix']}")
    return "\n".join(lines)
