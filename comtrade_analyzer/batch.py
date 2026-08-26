#!/usr/bin/env python3
"""
batch.py — One-command bulk analysis over a folder of real COMTRADE events.

Wraps fleet_analyze + fleet_dashboard into the workflow a protection group
actually runs: point it at the folder SUBNET (or AcSELerator) exports into, get
back a dashboard, a CSV and the WSO/EPSS numbers.  Two modes:

  on-demand   sweep the folder once and write outputs
  --watch     stay resident, re-sweeping on an interval as new events land

Both are incremental.  A manifest under <out>/.state records the size and mtime
of every file already analyzed, so a re-run only parses what is new — a watched
folder that has accumulated 40,000 events does not get re-parsed every cycle.
Pass --rebuild to force a full re-analysis.

Everything stays on disk.  The dashboard is a local HTML file; nothing is
uploaded anywhere unless you separately run `comtrade-dashboard --artifact` and
publish the result yourself.

Usage
-----
  comtrade-batch /path/to/events --devices devices.csv
  comtrade-batch /path/to/events --devices devices.csv --out //share/protection/review
  comtrade-batch /path/to/events --watch --interval 300
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import __version__
from .fleet_analyze import (
    DEFAULT_FEEDER_Z, _find_cfg, aggregate, analyze_one, load_config,
    print_summary, validate, write_csv,
)
from .fleet_dashboard import render as render_dashboard
from .triage import rule_table
from .wso_impact import class_table, load_registry

MANIFEST = "analyzed.json"
_HERE = Path(__file__).parent


# ---------------------------------------------------------------------------
# Incremental state
# ---------------------------------------------------------------------------

def _fingerprint(path: str) -> list:
    """(size, mtime) — cheap change detection without re-reading the file."""
    st = os.stat(path)
    return [st.st_size, round(st.st_mtime, 3)]


def load_manifest(state_dir: Path) -> dict:
    try:
        with open(state_dir / MANIFEST, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"version": __version__, "events": {}}


def save_manifest(state_dir: Path, manifest: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / (MANIFEST + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, default=str)
    os.replace(tmp, state_dir / MANIFEST)      # atomic; a killed run can't corrupt it


def partition(files: list, manifest: dict, rebuild: bool) -> tuple:
    """Split discovered files into (needs analysis, reusable cached results)."""
    if rebuild:
        return files, []
    fresh, cached = [], []
    for f in files:
        rec = manifest["events"].get(os.path.abspath(f))
        try:
            if rec and rec.get("fp") == _fingerprint(f):
                cached.append(rec["result"])
                continue
        except OSError:
            pass
        fresh.append(f)
    return fresh, cached


# ---------------------------------------------------------------------------
# One sweep
# ---------------------------------------------------------------------------

def sweep(args, registry: dict, cfg: dict, quiet: bool = False) -> Optional[dict]:
    events_dir = args.folder
    out_dir = Path(args.out or os.path.join(events_dir, "analysis"))
    state_dir = out_dir / ".state"

    files = _find_cfg(events_dir)
    if not files:
        if not quiet:
            print(f"No COMTRADE files under {events_dir}", file=sys.stderr)
        return None

    manifest = load_manifest(state_dir)
    fresh, cached = partition(files, manifest, args.rebuild)

    if not fresh and cached:
        if not quiet:
            print(f"No new events ({len(cached)} already analyzed).")
        if not args.always_write:
            return None

    if fresh and not quiet:
        print(f"Analyzing {len(fresh)} new event(s); {len(cached)} cached.")

    wave_opts = None if args.no_waveforms else tuple(args.waveform_buckets)
    payload = [(f, args.feeder_z, args.slow_trip_cycles, wave_opts) for f in fresh]

    t0 = time.time()
    results = None
    if args.jobs > 1 and len(payload) > 4:
        # A pool needs spawn, which re-imports the caller's main module. That
        # is fine from a console script but fails under an embedded or frozen
        # interpreter, and corporate endpoint software sometimes blocks the
        # child outright. Losing the workers must not lose the run.
        try:
            from concurrent.futures import ProcessPoolExecutor
            with ProcessPoolExecutor(max_workers=args.jobs) as pool:
                results = list(pool.map(analyze_one, payload, chunksize=4))
        except Exception as exc:                       # noqa: BLE001
            print(f"  Parallel workers unavailable ({type(exc).__name__}); "
                  f"falling back to a single process.", file=sys.stderr)
            results = None
    if results is None:
        results = [analyze_one(x) for x in payload]
    elapsed = time.time() - t0

    ok_new = [r for r in results if r["ok"]]
    parse_errors = [{"file": r["file"], "error": r["error"],
                     "diagnosis": r.get("diagnosis")} for r in results if not r["ok"]]
    for r in parse_errors:
        d = r.get("diagnosis")
        if d:
            print(f"  [FAIL] {d['message']}", file=sys.stderr)
            if d["detail"]:
                print(f"         {d['detail']}", file=sys.stderr)
            print(f"         → {d['fix']}", file=sys.stderr)
        else:
            print(f"  ERROR {r['file']}: {r['error']}", file=sys.stderr)

    # Roll up data-quality findings so one bad export setting is reported once,
    # not once per file across ten thousand events.
    from collections import Counter as _Counter
    from .diagnostics import ERROR as _ERR, WARN as _WARN
    tally, sample = _Counter(), {}
    for r in ok_new:
        for f in r.get("findings", []):
            if f["level"] in (_ERR, _WARN):
                tally[f["code"]] += 1
                sample.setdefault(f["code"], f)
    if tally and not quiet:
        print()
        print("  DATA QUALITY — these affect every result below")
        for code, n in tally.most_common():
            f = sample[code]
            mark = "[FAIL]" if f["level"] == _ERR else "[WARN]"
            print(f"    {mark} {f['message']}  ({n} file(s))")
            if f["fix"]:
                print(f"           → {f['fix']}")

    # Record what we just did so the next sweep can skip it
    for r in ok_new:
        try:
            manifest["events"][os.path.abspath(r["path"])] = {
                "fp": _fingerprint(r["path"]), "result": r,
            }
        except OSError:
            pass
    manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_manifest(state_dir, manifest)

    events = ok_new + cached
    events.sort(key=lambda e: (e.get("timestamp") or "", e["event_id"]))

    aggregates = aggregate(events, registry, args.epss_tiers, args.response_hours)
    truth = os.path.join(os.path.dirname(events_dir.rstrip("/")), "fleet_truth.json")
    validation = validate(events, truth) if os.path.isfile(truth) else None

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "folder": os.path.abspath(events_dir),
        "events_dir": os.path.abspath(events_dir),
        "registry_path": args.devices,
        "tool_version": __version__,
        "settings": {
            "feeder_z_ohm_per_mile": args.feeder_z,
            "epss_tiers": args.epss_tiers,
            "epss_max_shots": 0,
            "response_hours": args.response_hours,
            "slow_trip_cycles": args.slow_trip_cycles,
            "hif_threshold_a": 50.0,
            "waveforms": bool(wave_opts),
        },
        "triage_rules":  rule_table(args.slow_trip_cycles),
        "epss_classes":  class_table(),
        "files_found": len(files),
        "parse_errors": parse_errors,
        "data_quality": [{"code": c, "count": n, **sample[c]} for c, n in tally.most_common()],
        "elapsed_s": round(elapsed, 2),
        "events": events,
        "aggregates": aggregates,
        "validation": validation,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "fleet_analysis.json"
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, default=str)
    write_csv(events, str(out_dir / "fleet_events.csv"))

    html_path = out_dir / "fleet_dashboard.html"
    if not args.no_dashboard:
        render_dashboard(str(json_path), str(html_path))

    if not quiet:
        print_summary(result)
        print()
        print(f"  JSON      → {json_path}")
        print(f"  CSV       → {out_dir / 'fleet_events.csv'}")
        if not args.no_dashboard:
            print(f"  Dashboard → {html_path}")
            print(f"              open it in a browser — it is a local file, nothing is uploaded")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    cfg = load_config()
    p = argparse.ArgumentParser(
        prog="comtrade-batch",
        description="Bulk COMTRADE event analysis — dashboard, CSV and EPSS exposure "
                    "for a folder of relay events",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  comtrade-batch ./events --devices devices.csv
  comtrade-batch //share/subnet/export --devices devices.csv --out ./review
  comtrade-batch //share/subnet/export --watch --interval 300 --devices devices.csv
        """,
    )
    p.add_argument("folder", help="Folder of COMTRADE files (searched recursively)")
    p.add_argument("--devices", metavar="CSV",
                   help="Device registry CSV (auto-detected in the folder if absent)")
    p.add_argument("--out", metavar="DIR",
                   help="Output directory (default <folder>/analysis)")

    p.add_argument("--watch", action="store_true",
                   help="Stay resident and re-sweep as new events arrive")
    p.add_argument("--interval", type=int, default=300, metavar="SEC",
                   help="Seconds between sweeps in --watch mode (default 300)")
    p.add_argument("--rebuild", action="store_true",
                   help="Ignore the manifest and re-analyze every file")
    p.add_argument("--always-write", action="store_true",
                   help="Rewrite outputs even when no new events were found")

    p.add_argument("--feeder-z", type=float, default=DEFAULT_FEEDER_Z, metavar="OHM/MI",
                   help=f"Feeder Z1 magnitude for fault location (default {DEFAULT_FEEDER_Z})")
    p.add_argument("--response-hours", type=float,
                   default=cfg.get("wso", {}).get("avg_response_hours", 2.0), metavar="HRS",
                   help="Crew response time for customer-hour estimates")
    p.add_argument("--epss-tiers", type=int, nargs="+",
                   default=cfg.get("wso", {}).get("epss_tiers", [2, 3]), metavar="N",
                   help="Risk tiers receiving EPSS treatment")
    p.add_argument("--slow-trip-cycles", type=float,
                   default=cfg.get("triage", {}).get("slow_trip_cycles", 10.0), metavar="CYC",
                   help="Trip-delay threshold for the slow_trip flag")

    p.add_argument("--no-dashboard", action="store_true", help="Write JSON and CSV only")
    p.add_argument("--no-waveforms", action="store_true",
                   help="Skip waveform extraction (smaller JSON, no inline oscillography)")
    p.add_argument("--waveform-buckets", type=int, nargs=2, default=[180, 280],
                   metavar=("FULL", "ZOOM"), help="Envelope resolution (default 180 280)")
    p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 1),
                   help="Parallel worker processes")
    p.add_argument("--version", action="version", version=f"comtrade-analyzer {__version__}")
    return p


def main():
    args = build_parser().parse_args()

    if not os.path.isdir(args.folder):
        print(f"Error: folder not found — {args.folder}", file=sys.stderr)
        sys.exit(1)

    devices = args.devices
    if devices is None:
        # Look beside the events, then one level up, then beside the tool. A
        # SUBNET export folder is usually read-only, so the registry commonly
        # lives in its parent rather than in with the .cfg files.
        here = os.path.abspath(args.folder)
        for base in (here, os.path.dirname(here), str(_HERE.parent)):
            for cand in ("devices.csv", "fleet_devices.csv"):
                c = os.path.join(base, cand)
                if os.path.isfile(c):
                    devices = args.devices = c
                    break
            if devices:
                break
    registry = load_registry(devices) if devices else {}
    if devices:
        print(f"Registry: {len(registry)} device(s) from {devices}")
    else:
        print("No device registry — events will group under UNREGISTERED and "
              "customer-hour estimates will be zero. Pass --devices devices.csv.",
              file=sys.stderr)

    cfg = load_config()

    if not args.watch:
        sweep(args, registry, cfg)
        return

    # A watcher is normally run as a service with stdout redirected to a log.
    # Python block-buffers a non-tty stream, so without this the log stays empty
    # for hours and a SIGTERM discards whatever was pending.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    def _shutdown(_sig, _frm):
        print("\nStopped (signal).", flush=True)
        sys.stdout.flush()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)

    print(f"Watching {args.folder} every {args.interval}s.  Ctrl-C to stop.", flush=True)
    try:
        while True:
            stamp = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{stamp}] sweep")
            try:
                sweep(args, registry, cfg)
            except Exception as exc:                # noqa: BLE001 — a bad sweep must not kill the watcher
                print(f"  sweep failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
