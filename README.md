# COMTRADE Relay Event Analyzer

Parses IEEE C37.111 COMTRADE oscillography files from distribution relays, classifies fault type, generates waveform and phasor plots, produces Word event reports, and quantifies the reliability impact of Wildfire Safety Operations (WSO) / EPSS settings.

Designed for protection engineers reviewing fault events on 12–35 kV distribution feeders.

---

## Features

- **Fault classification** — SLG, LL, LLG, 3PH using symmetrical-component analysis (Fortescue transform on DFT phasors)
- **Phasor diagrams** — voltage phasors, current phasors, and sequence components (I0 / I1 / I2) at the fault window
- **Waveform plots** — annotated with fault inception and trip time
- **RMS current overlay** and **sequence component time-series** plots
- **Word reports** — embedded plots, event metadata, digital operations log, engineer sign-off
- **Event triage** — Priority 1 / 2 / 3 classification flags (HIF suspect, lockout, 3PH fault, slow trip, no-trip, LLG, multiple shots) printed in the CLI, shown as a colored banner in the Word report, and color-coded in the GUI log
- **Feeder / recloser analysis** — multi-shot reclose sequence detection, fault location estimate, HIF screen
- **WSO reliability impact analysis** — quantifies how many normal-day events would convert from momentary to sustained outage under EPSS (zero automatic reclose shots)
- **GUI** (`app.py`) — dark-themed tkinter interface with drag-and-drop file selection, all options exposed, live colored log output
- **CLI** (`main.py`) — scriptable, folder-mode batch processing, CSV and JSON export

---

## Installation

Get the code first — same on every platform:

```
git clone https://github.com/jacob-wheat-acre/comtrade-analyzer.git
cd comtrade-analyzer
```

Then follow the block for your shell. **One command per line** — do not join
them with `&&`, which Windows PowerShell 5.1 does not accept.

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

That is the whole install. Note it calls the venv's Python **directly** rather
than activating first — activation is the step that most often fails on a
managed PC, and it is not required.

Run the tool the same way:

```powershell
.\.venv\Scripts\comtrade-batch.exe demo\events --devices demo\devices.csv
```

If you would rather activate the environment so you can type `comtrade-batch`
without the prefix:

```powershell
.\.venv\Scripts\Activate.ps1
```

If that returns **"running scripts is disabled on this system"**, your
execution policy blocks it. Allow it for this window only — this does not
change any machine setting and lasts until you close the terminal:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

If `python` is not recognised, try `py -m venv .venv` instead — `py` is the
launcher that Windows Python installs.

### Windows Command Prompt (cmd.exe)

```bat
python -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -e .
```

`cmd.exe` has no execution-policy restriction, so this is the simpler route if
PowerShell is fighting you.

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

### Why `-e`

`-e` installs the tool **in place**, pointing at this folder, so a later
`git pull` updates it with no reinstall. Leave it off and you would have to
reinstall after every update.

Always use `python -m pip`, never plain `pip`. On a PC with more than one
Python, plain `pip` frequently installs into the wrong one, which looks exactly
like nothing having installed at all.

---

Installing puts six commands on your PATH (inside the venv):

| Command | What it does |
|---|---|
| `comtrade-batch` | **Bulk folder analysis** — dashboard + CSV + EPSS numbers, incremental, optional watch mode |
| `comtrade-analyze` | Single event or folder — plots, Word report, CSV/JSON |
| `comtrade-wso` | WSO/EPSS reliability impact report |
| `comtrade-dashboard` | Re-render the dashboard from an existing `fleet_analysis.json` |
| `comtrade-gui` | tkinter desktop interface |
| `comtrade-demo-fleet` | Generate synthetic events to try the pipeline without real data |

Use `pip install -e .` instead — it installs the tool **in place**, so a later
`git pull` updates it with no reinstall. That is the recommended setup for a
shared team copy.

**New to git or setting up a colleague's PC?** [`GIT_GUIDE.md`](GIT_GUIDE.md) is
a from-scratch walkthrough — installing Python and Git, cloning, installing,
getting updates, and what to do when `git pull` complains. No prior command-line
experience assumed.

**Something not working?**

```bash
python check_install.py
```

It reports which Python is actually running, which libraries loaded, whether the
commands are on PATH, whether the folder is cloud-synced, and whether `git pull`
will work — then tells you what to do about anything broken. Paste its whole
output when reporting a problem.

**Double-clickable launchers** ship in the folder. `.bat` files are Windows-only
(macOS cannot run them); `.command` files are the macOS equivalent:

| Task | Windows | macOS |
|---|---|---|
| Open the GUI | `COMTRADE Analyzer.bat` | the Desktop app, see below |
| Analyze a folder | `Analyze Folder.bat` (drag a folder onto it) | `Analyze Folder.command` (drag the folder into the prompt) |
| Put an icon on the Desktop | `install_shortcut.bat` | `python3 install_shortcut.py` |

On macOS, `python3 install_shortcut.py` builds a real **COMTRADE Analyzer.app**
on your Desktop. On first launch macOS may warn that it is from an unidentified
developer — System Settings → Privacy & Security → Open Anyway.

Python 3.10+ required. tkinter ships with CPython on Windows and macOS; on Linux install `python3-tk` via your package manager. Running from a checkout without installing still works — `python main.py` and `python app.py` are kept as shims.

---

## Quick Start

**Bulk analysis of an export folder — the usual starting point:**
```bash
comtrade-batch ./events --devices devices.csv
```
Writes `analysis/fleet_dashboard.html`, `fleet_events.csv` and `fleet_analysis.json`.
Open the HTML in a browser; it is a local file with no external dependencies.

**GUI:**
```bash
comtrade-gui
```
Point it at a folder and click Run Analysis: it classifies every event and
opens the dashboard in your browser. It does **not** render a plot or a report
per event — the dashboard is the viewer, and it carries the waveform, phasor
diagram, digital operations log and peak quantities for each one. Point it at a
single `.cfg` instead and you get the detailed per-event output (plots, Word
report) as before.

**Single event:**
```bash
comtrade-analyze fault_event.cfg --report --phasor-plot --save-plots
```

**WSO reliability impact analysis:**
```bash
comtrade-wso ./events/ --devices devices.csv --response-hours 2
```

**Demo it without real data.** A 100-event synthetic set ships in `demo/`, so
you can show the tool before any COMTRADE files are flowing.

*Nothing to install* — open `demo/demo_dashboard.html` in a browser. It is a
complete, pre-built dashboard. This is the fallback if the Python install
fights you on a managed PC.

*Or run the pipeline yourself*, which is the better demo because people watch
it happen:
```bash
comtrade-batch demo/events --devices demo/devices.csv --settings demo/settings_example.csv
```

On Windows, if you did not activate the environment:

```powershell
.\.venv\Scripts\comtrade-batch.exe demo\events --devices demo\devices.csv
```

Either way: 100 events in about a second, then it opens the dashboard it
just built.

Every dashboard built from generated events carries a **DEMO DATA** badge in
the header, so nobody mistakes it for real plant.

To make a different set (a fresh seed gives different events):
```bash
comtrade-demo-fleet --count 250 --seed 7      # writes ./fleet/, gitignored
comtrade-batch ./fleet/events --devices ./fleet/fleet_devices.csv
```

---

## CLI Reference (`main.py`)

| Flag | Description |
|---|---|
| `--report` | Generate Word (.docx) event report |
| `--phasor-plot` | Add phasor diagram to plots and report |
| `--save-plots` | Save PNGs alongside the event file |
| `--rms-plot` | Add sliding-RMS current plot |
| `--seq-plot` | Add symmetrical-sequence time-series plot |
| `--csv` | Export channel data to CSV |
| `--json` | Export event summary to JSON |
| `--feeder` | Enable feeder/recloser analysis (reclose sequence, fault location, HIF screen) |
| `--feeder-z OHM/MI` | Feeder positive-sequence impedance for fault location (e.g. `0.4`) |
| `--source-kv KV` | Source line-to-line voltage in kV |
| `--window SEC` | Zoom plot window centered on trigger (e.g. `0.1`) |
| `--location TEXT` | Substation / location name for report header |
| `--device-type TEXT` | Relay model for report header |
| `--engineer NAME` | Engineer name for report sign-off |
| `--list` | List discovered COMTRADE files without processing |

**Example — full distribution feeder report:**
```bash
python main.py reclose_event.cfg \
  --report --phasor-plot --save-plots --feeder \
  --feeder-z 0.4 --source-kv 12.47 \
  --location "Maple Ave Sub" --device-type "SEL-351A" \
  --engineer "Jane Smith"
```

---

## Event Triage (Priority System)

Every event is automatically assigned a review priority:

| Priority | Meaning | Flags |
|---|---|---|
| **1 — Immediate review** | Rare or safety-critical | HIF suspect, lockout, 3-phase fault, no trip detected, slow trip |
| **2 — Routine review** | Elevated risk, weekly batch | LLG fault, multiple reclose shots |
| **3 — Archive** | Routine transient | Everything else |

The threshold for "slow trip" defaults to 10 cycles (167 ms at 60 Hz) and is configurable in `config.json` under `triage.slow_trip_cycles`.

---

## WSO Reliability Impact Analysis (`wso_impact.py`)

Processes a folder of normal-day COMTRADE events and estimates how many would convert from momentary to sustained outage under EPSS (zero automatic reclose shots) during Wildfire Safety Operations days.

### Event classification

| Class | Meaning | WSO effect |
|---|---|---|
| `PERMANENT` | Locked out under normal settings | None — already a sustained outage |
| `NOT_EXPOSED` | Cleared without a reclose | None — EPSS cannot suppress what didn't happen |
| `WSO_EXPOSED` | Required ≥1 automatic reclose to clear | Becomes a sustained outage under EPSS |

### Device registry

Copy `devices_template.csv` to `devices.csv` and populate it with your devices. `devices.csv` is excluded from git to protect operational data.

```
device_id,   station,        feeder,            zone,    risk_tier, customers_served
RCL_123-456, Maple Ave Sub,  Maple Ave Feeder,  ZONE_A,  2,         1240
BKR_FEED1234,Oak St Sub,     Oak St Feeder,     ZONE_A,  3,         890
```

- **device_id** — must match the `rec_dev_id` field in the COMTRADE CFG file (check line 1, field 2)
- **zone** — WSO deployment zone name
- **risk_tier** — fire risk tier (1, 2, or 3); tiers 2 and 3 receive EPSS treatment by default
- **customers_served** — used for customer-hour estimates

### Usage

```bash
python wso_impact.py ./events/ --devices devices.csv
python wso_impact.py ./events/ --devices devices.csv --response-hours 3
python wso_impact.py ./events/ --no-devices   # classify without zone grouping
```

Outputs land in `<folder>/wso_output/`:
- `wso_impact.json` — full results including per-zone and per-device breakdown
- `wso_impact_report.docx` — Word report with system summary, per-zone tables, and methodology note

---

## Bulk Analysis at Work (`comtrade-batch`)

The command built for running this against a real export folder. It analyzes
everything, writes the dashboard and CSV, and prints the EPSS numbers in one go.

```bash
comtrade-batch //share/subnet/export --devices devices.csv --out ./review
```

### It only parses what is new

A manifest under `<out>/.state/analyzed.json` records the size and mtime of every
file already analyzed, so re-running only touches new events. A folder that has
accumulated 40,000 events does not get re-parsed on every run.

```
Analyzing 2 new event(s); 30 cached.
```

Pass `--rebuild` to force a full re-analysis — do this after changing
`--feeder-z`, `--epss-tiers` or anything else that affects results.

### Watched-folder mode

For a folder SUBNET writes into continuously:

```bash
comtrade-batch //share/subnet/export --devices devices.csv --watch --interval 300
```

It sweeps every 5 minutes, analyzes only what arrived, and rewrites the outputs.
A failed sweep is logged and the watcher keeps going. Output is line-buffered and
`SIGTERM` is handled cleanly, so it behaves under a service manager:

```ini
# /etc/systemd/system/comtrade-batch.service
[Service]
ExecStart=/opt/comtrade/.venv/bin/comtrade-batch /srv/subnet/export \
          --devices /opt/comtrade/devices.csv --out /srv/protection/review \
          --watch --interval 300
Restart=on-failure
```

On Windows, point Task Scheduler at the same command without `--watch` and let
the scheduler supply the interval — the manifest makes repeat runs cheap.

### Useful flags

| Flag | Why |
|---|---|
| `--out DIR` | Write outputs to a share instead of next to the events |
| `--rebuild` | Re-analyze everything (after a settings change) |
| `--always-write` | Rewrite outputs even when nothing new arrived |
| `--no-waveforms` | Drop the inline oscillography — JSON goes from ~25 KB to ~1 KB per event |
| `--no-dashboard` | JSON and CSV only |
| `--jobs N` | Worker processes (defaults to CPU count - 1) |

---

## Relay settings (`--settings`)

SUBNET exports a flattened device settings table — one row per relay, with the
CT ratio and phase/ground pickups for up to three setting groups. Point the
batch at it and the ride-throughs stop being guesses:

```bash
comtrade-batch ./events --devices devices.csv --settings subnet_settings.xlsx
```

| Column | Used for |
|---|---|
| `Name` | matched against the COMTRADE `rec_dev_id` (punctuation and case ignored) |
| `CTR` | converts secondary pickups to the primary amps a record measures |
| `NOMINAL_SG` | the normal-day setting group |
| `SG1/2/3_PHASE`, `_GROUND` | pickups per group; `Not found` becomes empty |
| `TemplateDate` | actually the template name, e.g. `SEL-651R-WF-3PhTrip3PhLoc.2`, parsed into relay type, application, trip/location mode and version |

**Which group is EPSS is inferred**, not read: the most sensitive populated
group outside the nominal one. The dashboard always states which group it used
and that it was inferred, so it can be checked rather than trusted.

Pickups are assumed **secondary** and multiplied by CTR. If your export is
already primary, pass `--settings-primary`; either way the loader flags a
magnitude that looks like the wrong convention.

With settings loaded, each ride-through gets a verdict — *confirmed*, *ruled
out*, or *trips either way* — with the measured RMS fault current, both
pickups, and the multiple of each. Relays with no CTR or only one populated
group stay unresolved and say so.

---

## Handling operational data

Real event files, and everything derived from them, contain device IDs, feeder
names, customer counts and outage estimates.

- **`devices.csv` is gitignored** and must stay that way. Only
  `devices_template.csv` is tracked. Never commit a populated registry, and keep
  real device IDs and feeder names out of commit messages and test fixtures.
- **Every `comtrade-batch` output is a local file.** The dashboard is
  self-contained HTML that loads no external resources and sends nothing
  anywhere. Opening it in a browser does not transmit the data.
- **`comtrade-dashboard --artifact`** exists only to prepare the page for
  publishing to an external service. Publishing uploads the full per-event
  dataset — device IDs, feeders, customer counts and all. Do not use it with real
  operational data unless your organization has explicitly cleared that.

---

## Configuration (`config.json`)

```jsonc
{
  "analysis": {
    "line_frequency_hz": 60,
    "fault_threshold_multiplier": 2.0   // fault current = this × pre-fault RMS
  },
  "triage": {
    "slow_trip_cycles": 10.0            // trip delays longer than this → Priority 1 flag
  },
  "wso": {
    "epss_max_shots": 0,                // shots allowed under EPSS (0 = none)
    "epss_tiers": [2, 3],               // risk tiers that receive EPSS treatment
    "avg_response_hours": 2.0           // crew response time for customer-hour estimates
  }
}
```

---

## Tests

```bash
pytest test_comtrade.py -v
```

81 tests covering the parser, the peak-vs-RMS magnitude conventions, Fortescue
sequence math, fault classification, reclose-sequence detection, the WSO/EPSS
three-way boundary, triage flags, the HIF screen, the fault-location guard, the
batch manifest, and waveform decimation — plus end-to-end checks against the
generated fixtures.

Two tests are `xfail(strict=True)` and record known defects rather than hiding
them. If either flips to `XPASS`, the defect was fixed and the marker should be
removed in the same commit:

| Test | Defect |
|---|---|
| `test_the_slg_reference_fixture_classifies_as_slg` | `generate_test_data.py` drives the unfaulted phases at 140 A against an 800 A faulted phase, so `ratio_mid` lands at 0.154 against the classifier's `< 0.15` SLG gate and it classifies LLG. Fix the fixture (`I_FAULT_BC` 140 → ~110 A), not the threshold. |
| `test_no_current_rise_is_not_flagged` | `screen_high_impedance_fault` tests `0 < max_delta < threshold`, so floating-point dust on an unchanged current satisfies it — a steady load reports `delta 0.0 A` and `hif_suspect True` together. Needs a meaningful floor on `max_delta`. |

---

## Test Events

Synthetic COMTRADE files are included for validation. Regenerate them with:

```bash
python generate_test_ll.py        # A-B line-to-line fault, 12.47 kV
python generate_test_llg.py       # A-B double line-to-ground fault
python generate_test_3ph.py       # balanced three-phase fault
python generate_test_recloser.py  # SLG with 3-shot reclose sequence, lockout
python generate_test_data.py      # SLG single-trip reference event
```

All five classify correctly against their expected fault types.

---

## Desktop Shortcut (Windows / Mac)

```bash
python install_shortcut.py
```

Creates a desktop shortcut that launches the GUI. On Windows, uses `SHGetFolderPathW` with `CSIDL_DESKTOPDIRECTORY` to find the real Desktop path, which correctly handles OneDrive sync and Group Policy folder redirection.

---

## COMTRADE Format Notes

The analyzer reads IEEE C37.111 COMTRADE files (`.cfg` + `.dat`, ASCII or binary). It expects:

- **Analog channels** named with standard keywords for phase (A/B/C/N) and quantity (I/V) — see `config.json` `channel_keywords` for the full list
- **Digital channels** including at least a TRIP signal for trip-time detection; CLOSE/52A channels improve reclose sequence accuracy
- **Sample rate** ≥ 1 cycle of data before the trigger for pre-fault baseline

Channels are auto-detected from names — no manual mapping required for standard SEL relay exports.

---

## Pipeline Integration (SUBNET → COMTRADE)

The intended production workflow is:

```
SEL relay  →  SUBNET (COMTRADE export)  →  watched folder  →  wso_impact.py / main.py
```

SUBNET is configured to pull events from field relays and export COMTRADE `.cfg` + `.dat` pairs. The analyzer consumes that folder directly. No conversion step is needed on this end.

For current AcSELerator users: AcSELerator can batch-export `.cev` files to COMTRADE. Point the analyzer at the export folder.

---

## File Structure

```
comtrade-analyzer/
├── pyproject.toml            Package metadata and console entry points
├── comtrade_analyzer/
│   ├── batch.py              Bulk folder analysis (comtrade-batch) — incremental + watch
│   ├── main.py               Single-event CLI (comtrade-analyze)
│   ├── app.py                GUI (comtrade-gui)
│   ├── wso_impact.py         WSO reliability impact (comtrade-wso)
│   ├── fleet_analyze.py      Per-event pipeline, aggregation, waveform extraction
│   ├── fleet_dashboard.py    Dashboard renderer (comtrade-dashboard)
│   ├── dashboard_template.html   Dashboard markup, CSS and JS
│   ├── fleet_gen.py          Synthetic event generator (comtrade-demo-fleet)
│   ├── comtrade_parser.py    IEEE C37.111 parser (CFG + DAT)
│   ├── data_model.py         EventRecord dataclass
│   ├── analysis.py           Fault classification, phasor extraction, DFT
│   ├── feeder_analysis.py    Reclose sequence, fault location, HIF screen
│   ├── plotting.py           Waveform, RMS, sequence, phasor plots
│   ├── report.py             Word (.docx) report generator
│   ├── triage.py             Priority 1/2/3 event triage
│   ├── config.json           User-configurable thresholds
│   └── devices_template.csv  Device registry template (copy → devices.csv)
├── main.py, app.py           Compatibility shims for running from a checkout
├── install_shortcut.py       Desktop shortcut installer
├── make_icon.py              Icon generator (PNG / ICO / ICNS)
└── generate_test_*.py        Synthetic test event generators
```
