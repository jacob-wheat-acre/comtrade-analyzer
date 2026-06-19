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

```bash
git clone https://github.com/jacob-wheat-acre/comtrade-analyzer.git
cd comtrade-analyzer
pip install -r requirements.txt
pip install python-docx          # required for Word report generation
```

Python 3.10+ recommended. tkinter is included with most Python distributions; on Linux install `python3-tk` via your package manager.

---

## Quick Start

**GUI:**
```bash
python app.py
```

**CLI — single file:**
```bash
python main.py fault_event.cfg --report --phasor-plot --save-plots
```

**CLI — folder batch:**
```bash
python main.py ./events/ --report --phasor-plot --save-plots
```

**WSO reliability impact analysis:**
```bash
python wso_impact.py ./events/ --devices devices.csv --response-hours 2
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
├── app.py                  GUI (tkinter)
├── main.py                 CLI entry point
├── wso_impact.py           WSO reliability impact batch analysis
├── comtrade_parser.py      IEEE C37.111 parser (CFG + DAT)
├── data_model.py           EventRecord dataclass
├── analysis.py             Fault classification, phasor extraction, DFT
├── feeder_analysis.py      Reclose sequence, fault location, HIF screen
├── plotting.py             Waveform, RMS, sequence, phasor plots
├── report.py               Word (.docx) report generator
├── triage.py               Priority 1/2/3 event triage
├── config.json             User-configurable thresholds
├── devices_template.csv    Device registry template (copy → devices.csv)
├── install_shortcut.py     Desktop shortcut installer
├── make_icon.py            Icon generator (PNG / ICO / ICNS)
└── generate_test_*.py      Synthetic test event generators
```
