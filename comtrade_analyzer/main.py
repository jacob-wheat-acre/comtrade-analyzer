#!/usr/bin/env python3
"""
main.py — COMTRADE Relay Event Analyzer CLI

Usage
-----
  python main.py event.cfg
  python main.py ./events/
  python main.py event.cfg --window 0.1 --save-plots
  python main.py event.cfg --csv --json --no-plot
  python main.py ./events/ --list

Folder mode processes every .cfg / .cff found (one level deep).
"""

import argparse
import csv
import glob
import json
import os
import sys
from typing import Optional

import numpy as np

from .comtrade_parser import COMTRADEParser
from .data_model import EventRecord
from .analysis import compute_event_summary
from .plotting import plot_event, plot_rms_currents, plot_sequence_components, plot_phasors
from .report import generate_report, generate_word_report
from .feeder_analysis import compute_feeder_summary, print_feeder_summary
from .triage import triage_event


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def find_comtrade_files(path: str) -> list:
    """
    Return a sorted list of .cfg / .cff file paths under path.

    If path is a file, returns [path].  If a directory, searches one level
    deep (plus the directory itself).
    """
    if os.path.isfile(path):
        return [path]

    found = []
    for pattern in ("*.cfg", "*.CFG", "*.cff", "*.CFF"):
        found.extend(glob.glob(os.path.join(path, pattern)))
        found.extend(glob.glob(os.path.join(path, "**", pattern), recursive=True))

    # Deduplicate and sort
    return sorted(set(found))


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def export_csv(record: EventRecord, output_path: str):
    """
    Write all channel data to a flat CSV file.

    Columns: time, <analog_channel_names...>, <digital_channel_names...>
    """
    analog_names  = list(record.analog_channels.keys())
    digital_names = list(record.digital_channels.keys())
    headers = ["time"] + analog_names + digital_names

    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        n = len(record.time)
        for i in range(n):
            row = [f"{record.time[i]:.9f}"]
            for name in analog_names:
                row.append(f"{record.analog_channels[name][i]:.6g}")
            for name in digital_names:
                row.append(str(int(record.digital_channels[name][i])))
            writer.writerow(row)

    print(f"  CSV  → {output_path}")


def export_json(summary: dict, output_path: str):
    """Write the event summary dict to JSON."""
    def _serial(v):
        if isinstance(v, (np.floating, np.integer)):
            return float(v)
        return str(v)

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=_serial)

    print(f"  JSON → {output_path}")


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def print_summary(summary: dict):
    """Print the event summary in a protection-engineer-friendly format."""
    W = 62
    print()
    print("=" * W)
    print("  RELAY EVENT SUMMARY")
    print("=" * W)
    print(f"  Station   : {summary.get('station', '—')}")
    print(f"  Device ID : {summary.get('device_id', '—')}")
    print(f"  Sample rate: {summary.get('sample_rate', 0):.0f} Hz")
    print(f"  Duration  : {summary.get('duration_s', 0) * 1000:.1f} ms")
    print(f"  Channels  : {summary.get('n_analog', 0)} analog / "
          f"{summary.get('n_digital', 0)} digital")
    print()

    ftype = summary.get("fault_type", "UNKNOWN")
    if ftype != "UNKNOWN":
        labels = {
            "SLG": "Single Line-to-Ground (SLG)",
            "LL":  "Line-to-Line (LL)",
            "LLG": "Double Line-to-Ground (LLG)",
            "3PH": "Three-Phase (3PH)",
            "LOAD": "Load step (no fault)",
        }
        print(f"  Fault type : {labels.get(ftype, ftype)}")

    trig = summary.get("trigger_time", 0.0)

    fi = summary.get("fault_inception_s")
    if fi is not None:
        rel = (fi - trig) * 1000
        print(f"  Fault inception : {fi * 1000:.2f} ms  ({rel:+.1f} ms from trigger)")

    ti = summary.get("trip_time_s")
    if ti is not None:
        rel = (ti - trig) * 1000
        ch  = summary.get("trip_channel", "")
        print(f"  Trip time       : {ti * 1000:.2f} ms  ({rel:+.1f} ms from trigger)"
              + (f"  [{ch}]" if ch else ""))

    fd = summary.get("fault_duration_s")
    if fd is not None:
        print(f"  Fault duration  : {fd * 1000:.1f} ms")
        print(f"  Trip delay      : {summary.get('trip_delay_ms', 0):.1f} ms")

    mc = summary.get("max_currents", {})
    mv = summary.get("max_voltages", {})
    if mc:
        print()
        print("  Peak currents:")
        for name, val in mc.items():
            print(f"    {name:10s}  {val:>9.2f} A")
    if mv:
        print()
        print("  Peak voltages:")
        for name, val in mv.items():
            units = "V"
            print(f"    {name:10s}  {val:>9.2f} {units}")

    print("=" * W)


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def process_file(filepath: str, args: argparse.Namespace) -> Optional[EventRecord]:
    """Parse one COMTRADE file, print summary, and produce outputs."""
    print(f"\n{'─' * 60}")
    print(f"  File: {filepath}")
    print(f"{'─' * 60}")

    parser = COMTRADEParser()
    try:
        record = parser.parse(filepath)
    except Exception as exc:
        print(f"  ERROR: {exc}")
        return None

    print(f"  Loaded: {len(record.analog_channels)} analog, "
          f"{len(record.digital_channels)} digital, "
          f"{len(record.time)} samples @ {record.sample_rate:.0f} Hz")

    summary = compute_event_summary(record)
    summary["trigger_time"] = record.trigger_time
    print_summary(summary)

    base = os.path.splitext(filepath)[0]

    # CSV export
    if args.csv:
        csv_path = (base + "_output.csv") if args.csv == "__auto__" else args.csv
        export_csv(record, csv_path)

    # JSON export
    if args.json:
        json_path = (base + "_summary.json") if args.json == "__auto__" else args.json
        export_json(summary, json_path)

    # Plots — always save PNGs when --report is requested so they can be embedded
    waveform_png: Optional[str] = None
    phasor_png:   Optional[str] = None
    if not args.no_plot or args.report:
        save_base = base if (args.save_plots or args.report) else None

        waveform_png_path = (save_base + "_waveform.png") if save_base else None
        plot_event(
            record,
            window_s=args.window,
            save_path=waveform_png_path,
        )
        if waveform_png_path and os.path.isfile(waveform_png_path):
            waveform_png = waveform_png_path

        if args.rms_plot:
            plot_rms_currents(
                record,
                save_path=(save_base + "_rms.png") if save_base else None,
            )

        if args.seq_plot:
            plot_sequence_components(
                record,
                save_path=(save_base + "_sequence.png") if save_base else None,
            )

        if args.phasor_plot:
            phasor_path = (save_base + "_phasors.png") if save_base else None
            plot_phasors(record, save_path=phasor_path)
            if phasor_path and os.path.isfile(phasor_path):
                phasor_png = phasor_path

    # Feeder / recloser analysis
    feeder_data: Optional[dict] = None
    if args.feeder:
        feeder_data = compute_feeder_summary(
            record,
            feeder_impedance_ohm_per_mile=args.feeder_z,
            source_voltage_ll_kv=args.source_kv,
            hif_threshold_a=args.hif_threshold,
        )
        print_feeder_summary(feeder_data)

    # Triage
    triage = triage_event(summary, feeder_data=feeder_data)
    W = 62
    print()
    print("─" * W)
    priority_labels = {1: "PRIORITY 1 — IMMEDIATE REVIEW", 2: "PRIORITY 2 — ROUTINE REVIEW", 3: "PRIORITY 3 — ARCHIVE"}
    print(f"  {priority_labels[triage['priority']]}")
    for lbl, note in zip(triage["labels"], triage["notes"]):
        print(f"    [{lbl}] {note}")
    print("─" * W)

    # Word report
    if args.report:
        from pathlib import Path
        report_data = generate_report(record)
        out_path = generate_word_report(
            record,
            report_data,
            outdir=Path(os.path.dirname(filepath) or "."),
            stem=os.path.basename(base),
            location=getattr(args, "location", "") or "",
            device_type=getattr(args, "device_type", "") or "",
            engineer_name=getattr(args, "engineer", "") or "",
            engineer_title=getattr(args, "engineer_title", "") or "",
            engineer_contact=getattr(args, "engineer_contact", "") or "",
            waveform_png=waveform_png,
            phasor_png=phasor_png,
            feeder=feeder_data,
            triage=triage,
        )
        if out_path:
            print(f"  DOCX → {out_path}")

    return record


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="comtrade-analyzer",
        description="COMTRADE Relay Event Analyzer — IEEE C37.111",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python main.py fault_event.cfg
  python main.py ./events/
  python main.py fault_event.cfg --window 0.1 --save-plots
  python main.py fault_event.cfg --csv --json --no-plot
  python main.py fault_event.cfg --report --engineer "Jane Smith" --location "North Substation"
  python main.py reclose_event.cfg --feeder --feeder-z 0.4 --source-kv 12.47 --report
  python main.py ./events/ --list
        """,
    )

    p.add_argument("path",
                   help="Path to .cfg / .cff file or folder containing events")

    p.add_argument("--csv", nargs="?", const="__auto__", metavar="FILE.csv",
                   help="Export channel data to CSV (auto-named if no path given)")

    p.add_argument("--json", nargs="?", const="__auto__", metavar="FILE.json",
                   help="Export summary to JSON")

    p.add_argument("--no-plot", action="store_true",
                   help="Skip all plots")

    p.add_argument("--save-plots", action="store_true",
                   help="Save plots as PNG files instead of showing interactively")

    p.add_argument("--window", type=float, metavar="SEC",
                   help="Zoom plot window (seconds) centred on trigger, e.g. 0.1")

    p.add_argument("--rms-plot", action="store_true",
                   help="Also generate a sliding-RMS current plot")

    p.add_argument("--seq-plot", action="store_true",
                   help="Also generate a symmetrical-sequence component plot")

    p.add_argument("--phasor-plot", action="store_true",
                   help="Generate a phasor diagram (V, I, and sequence components) "
                        "and embed it in the Word report")

    p.add_argument("--list", action="store_true",
                   help="List discovered COMTRADE files without processing")

    # Feeder / recloser mode
    p.add_argument("--feeder", action="store_true",
                   help="Enable distribution feeder / recloser analysis (multi-shot sequence, "
                        "fault location, HIF screen)")

    p.add_argument("--feeder-z", type=float, default=None, metavar="OHM/MILE",
                   help="Feeder positive-sequence impedance magnitude in Ω/mile "
                        "(used for fault location estimate, e.g. 0.4 for typical OH line)")

    p.add_argument("--source-kv", type=float, default=None, metavar="KV_LL",
                   help="Source line-to-line voltage in kV (used if voltage channels absent)")

    p.add_argument("--hif-threshold", type=float, default=50.0, metavar="AMPS",
                   help="HIF screen threshold in A (default 50 A; increase for heavily loaded feeders)")

    # Report generation
    p.add_argument("--report", action="store_true",
                   help="Generate a Word (.docx) event report (requires python-docx)")

    p.add_argument("--location", metavar="TEXT",
                   help="Substation / location name for the report header")

    p.add_argument("--device-type", metavar="TEXT",
                   help="Relay model / device type for the report header")

    p.add_argument("--engineer", metavar="NAME",
                   help="Engineer name for the report sign-off")

    p.add_argument("--engineer-title", metavar="TEXT",
                   help="Engineer title for the report sign-off")

    p.add_argument("--engineer-contact", metavar="TEXT",
                   help="Engineer contact info (phone / email) for the sign-off")

    return p


def main():
    args = build_parser().parse_args()

    if not os.path.exists(args.path):
        print(f"Error: path not found — {args.path}", file=sys.stderr)
        sys.exit(1)

    files = find_comtrade_files(args.path)

    if not files:
        print(f"No COMTRADE files (.cfg / .cff) found under: {args.path}",
              file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(files)} COMTRADE file(s) under: {args.path}")

    if args.list:
        for f in files:
            print(f"  {f}")
        return

    for filepath in files:
        process_file(filepath, args)

    print("\nDone.")


if __name__ == "__main__":
    main()
