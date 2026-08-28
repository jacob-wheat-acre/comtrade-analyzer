#!/usr/bin/env python3
"""
fleet_dashboard.py — Render the batch analysis as a self-contained dashboard.

Takes the fleet_analysis.json produced by fleet_analyze.py and injects it into
dashboard_template.html, producing one HTML file with no external dependencies
beyond the Google Fonts stylesheet.  Open it directly in a browser, or publish
it as a Claude Artifact — the template is deliberately body-only (no <html> /
<head> / <body> wrapper) so the same file serves both.

Usage
-----
  python3 fleet_dashboard.py ./fleet/analysis/fleet_analysis.json
  python3 fleet_dashboard.py ./fleet/analysis/fleet_analysis.json -o /tmp/review.html
  python3 fleet_dashboard.py ./fleet                 # finds analysis/ itself
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

_HERE = Path(__file__).parent
TEMPLATE = _HERE / "dashboard_template.html"
PLACEHOLDER = "__FLEET_DATA__"

# Per-event keys the page never reads — dropped to keep the payload small.
_DROP_KEYS = ("path", "ok", "error", "analog_channels", "digital_channels",
              "triage_line_raw", "trigger_time_abs")


def _js_safe(payload: dict) -> str:
    """
    Serialise for embedding inside a <script> block.

    Two hazards: a literal '</script>' anywhere in the data would close the
    block early, and U+2028/U+2029 are line terminators in JavaScript source
    even though JSON treats them as ordinary characters.
    """
    text = json.dumps(payload, separators=(",", ":"), default=str)
    return (text.replace("</", "<\\/")
                .replace(" ", "\\u2028")
                .replace(" ", "\\u2029"))


def build_payload(data: dict) -> dict:
    events = []
    for e in data.get("events", []):
        events.append({k: v for k, v in e.items() if k not in _DROP_KEYS})
    return {
        "generated_at":  data.get("generated_at"),
        "folder":        data.get("folder"),
        "settings":      data.get("settings", {}),
        "triage_rules":  data.get("triage_rules", []),
        "epss_classes":  data.get("epss_classes", []),
        "data_quality":  data.get("data_quality", []),
        "files_found":   data.get("files_found"),
        "parse_errors":  data.get("parse_errors", []),
        "elapsed_s":     data.get("elapsed_s"),
        "events":        events,
        "aggregates":    data.get("aggregates", {}),
        "topology":      data.get("topology", []),
        "incidents":     data.get("incidents", []),
        "validation":    data.get("validation"),
    }


DOCTYPE = "<!doctype html>\n"


def render(analysis_path: str, out_path: str, artifact: bool = False) -> str:
    with open(analysis_path, encoding="utf-8") as fh:
        data = json.load(fh)

    if not data.get("events"):
        raise SystemExit(f"No events in {analysis_path} — run fleet_analyze.py first.")

    # Explicit UTF-8: the template carries °, →, ±, Ω, ∠ and box drawing, and
    # Windows would otherwise decode it as cp1252 and raise.
    template = TEMPLATE.read_text(encoding="utf-8")
    if PLACEHOLDER not in template:
        raise SystemExit(f"{TEMPLATE} is missing the {PLACEHOLDER} placeholder.")

    html = template.replace(PLACEHOLDER, _js_safe(build_payload(data)))
    # A standalone file needs the doctype or the browser drops into quirks mode
    # (where document.body is the scroller and smooth scrolling silently fails).
    # The Artifact publisher supplies its own wrapper, so --artifact omits it.
    if not artifact:
        html = DOCTYPE + html
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out_path


def resolve_analysis(arg: str) -> str:
    """Accept the JSON itself, a fleet folder, or an analysis folder."""
    if os.path.isfile(arg):
        return arg
    for cand in (os.path.join(arg, "analysis", "fleet_analysis.json"),
                 os.path.join(arg, "fleet_analysis.json")):
        if os.path.isfile(cand):
            return cand
    raise SystemExit(f"No fleet_analysis.json found at or under: {arg}")


def main():
    p = argparse.ArgumentParser(
        prog="fleet-dashboard",
        description="Render fleet_analysis.json as a self-contained HTML dashboard")
    p.add_argument("analysis", help="fleet_analysis.json, or the fleet folder containing it")
    p.add_argument("-o", "--out", metavar="HTML",
                   help="Output path (default: alongside the JSON as fleet_dashboard.html)")
    p.add_argument("--open", action="store_true", help="Open the result in a browser")
    p.add_argument("--artifact", action="store_true",
                   help="Omit the doctype for publishing as a Claude Artifact "
                        "(the publisher adds its own document wrapper)")
    args = p.parse_args()

    analysis_path = resolve_analysis(args.analysis)
    out_path = args.out or os.path.join(os.path.dirname(os.path.abspath(analysis_path)),
                                        "fleet_dashboard.html")
    render(analysis_path, out_path, artifact=args.artifact)
    size_kb = os.path.getsize(out_path) / 1024

    print(f"Dashboard → {out_path}  ({size_kb:,.0f} KB)")
    print("  Publish-ready variant: add --artifact"
          if not args.artifact else "  Artifact variant (no doctype) — publish this one.")
    if args.open:
        webbrowser.open(f"file://{os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
