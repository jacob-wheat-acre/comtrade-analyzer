# COMTRADE Analyzer — Dev Notes

Parses IEEE C37.111 oscillography from distribution relays, classifies the
fault, and quantifies WSO/EPSS reliability impact. Users are protection
engineers reviewing real events on 12–35 kV feeders.

`README.md` documents every CLI flag, the triage table, the device registry, and
the config keys — it is genuinely complete, so **read it rather than
re-deriving**. This file covers what it doesn't: the conventions inside the
code, and the traps.

## Module map

Everything lives in the `comtrade_analyzer/` package; imports between these are
relative (`from .analysis import ...`).

```
comtrade_parser.py   IEEE C37.111 CFG + DAT reader (ASCII and binary)
cev_parser.py        SEL Compressed Event (.CEV) reader — produces EventRecord
data_model.py        EventRecord — the one object everything passes around
analysis.py          RMS, fault inception, trip time, DFT phasors, sequence
                     components, fault classification, DC offset
feeder_analysis.py   Reclose sequence, fault location, HIF screen
triage.py            Priority 1/2/3 flags — _FLAGS + _TRIGGERS drive the UI
relay_settings.py    SUBNET settings catalog, template parsing, pickup math
topology.py          Mainline feeder connectivity  → comtrade-topology
incidents.py         Regroups event records into the faults that caused them
diagnostics.py       Says plainly what is wrong with a real COMTRADE file
wso_impact.py        EPSS impact — classify_event owns the three-way boundary
plotting.py          Waveform / RMS / sequence / phasor plots
report.py            Word (.docx) event report
main.py              Single-event CLI            → comtrade-analyze
app.py               tkinter GUI                 → comtrade-gui
batch.py             Bulk folder runner          → comtrade-batch
fleet_analyze.py     Per-event pipeline, aggregation, waveform extraction
fleet_dashboard.py   Dashboard renderer          → comtrade-dashboard
fleet_gen.py         Synthetic event generator   → comtrade-demo-fleet
dashboard_template.html   Dashboard markup/CSS/JS, __FLEET_DATA__ placeholder
```

Root `main.py` and `app.py` are two-line shims. Console entry points are in
`pyproject.toml`; `pip install -e .` for development.

**When editing `dashboard_template.html`:** syntax-check its three `<script>`
blocks before rendering:

```bash
python3 - <<'EOF'
import re, subprocess, pathlib
src = pathlib.Path("comtrade_analyzer/dashboard_template.html").read_text()
for i, b in enumerate(re.findall(r"<script>\n(.*?)\n</script>", src, re.S), 1):
    f = f"/tmp/block{i}.js"; pathlib.Path(f).write_text(b.replace("__FLEET_DATA__", "{}"))
    r = subprocess.run(["node", "--check", f], capture_output=True, text=True)
    print(f"block {i}: {'OK' if r.returncode == 0 else r.stderr[:300]}")
EOF
```

`EventRecord` (`data_model.py`, 95 lines — read it first) is the contract:
analog channels already scaled to engineering units, digital channels as int8,
plus `samples_per_cycle()` and `line_freq()`. Everything downstream assumes
engineering units; scaling happens once, in the parser.

## Validating a change

The five generators are the test suite:

```bash
python generate_test_data.py       # SLG, single trip
python generate_test_ll.py         # A-B line-to-line, 12.47 kV
python generate_test_llg.py        # A-B double line-to-ground
python generate_test_3ph.py        # balanced three-phase
python generate_test_recloser.py   # SLG, 3-shot reclose, lockout
python main.py test_ll/test_ll.cfg --report --phasor-plot --save-plots
```

```bash
pytest test_comtrade.py -v          # run this first
```

`test_comtrade.py` follows the pq-analyzer convention: one file, class-grouped,
numbered sections, helpers prefixed `_`, fixture-dependent tests behind
`skipif`. Records are built in-test via `_record()` / `_fault_record()` so the
math tests never depend on a generated file.

**Two tests are `xfail(strict=True)` and record real defects.** Strict means an
unexpected pass fails the suite — if one flips to `XPASS`, the defect was fixed
and the marker must come off in the same commit. Do not "fix" either by moving
a threshold:

- `test_the_slg_reference_fixture_classifies_as_slg` — see the known gap below.
- `test_no_current_rise_is_not_flagged` — `screen_high_impedance_fault` tests
  `0 < max_delta < threshold`, so floating-point dust on an unchanged current
  satisfies it. A steady balanced load reports `delta 0.0 A` and
  `hif_suspect True` at once, which reads as a Priority 1 downed conductor.
  `compute_feeder_summary` usually masks it by passing `fault_index=None` on a
  quiet record. The fix is a meaningful floor on `max_delta`, not `> 0`.

All five generators must still classify to their expected fault types — that is
the regression bar for anything in `analysis.py`.

**Known gap:** `generate_test_data.py` (the SLG reference) currently classifies
as **LLG**, not SLG. Its unfaulted phases are driven at 140 A against an 800 A
faulted phase, which lands `ratio_mid` at 0.154 versus the classifier's `< 0.15`
SLG gate — it misses by four thousandths and falls through to the LLG catch-all.
The honest fix is the fixture, not the threshold: drop `I_FAULT_BC` from 140 to
~110 A. Do **not** move the 0.15 threshold.

Plots block on `plt.show()` unless `--save-plots` is passed, so headless
regression runs need `--save-plots` and `MPLBACKEND=Agg`.

## Magnitude conventions — read this before touching the math

There are two magnitude conventions in `analysis.py` and mixing them is a
silent factor-of-√2 error:

- **`compute_rms()` returns RMS**, over a sliding window.
- **Every DFT phasor is PEAK**, not RMS. Both `_phasor()` in
  `compute_phasors_at()` and the kernel in `compute_sequence_components()` scale
  by `2/N`, which recovers peak amplitude. Nothing converts to RMS afterward, so
  reported phasor magnitudes and I0/I1/I2 are all peak.

Known wrinkle: `classify_fault()` compares a peak-scaled `i0_post` against an
RMS-derived `top` in the LL-vs-LLG test (`i0_post > 0.1 * top`). Both are
heuristic thresholds tuned against the synthetic fixtures, so the effective
threshold is ~0.071 in consistent units, not 0.10. **Don't "fix" this silently**
— it would move the LL/LLG boundary on real events. If you normalize it,
re-validate all five generators and say so in the commit.

## Sequence components

Standard Fortescue, `a = exp(j2π/3)`:

```
I0 = (Ia + Ib  + Ic ) / 3
I1 = (Ia + a·Ib + a²·Ic) / 3
I2 = (Ia + a²·Ib + a·Ic) / 3
```

Phase rotation is assumed **ABC**. There is no ACB handling; on an ACB system
I1 and I2 swap. If that ever needs supporting, it belongs in one place here.

`compute_sequence_components()` returns **magnitudes only**, NaN-padded for the
first `window_samples - 1` samples — use `np.nanmedian`/`np.nanmax`, never bare
`np.median`. `compute_phasors_at()` returns **complex** `seq_i`/`seq_v`; that's
the one to use when angle matters.

## Phasor reference

All phasors from `compute_phasors_at()` are rotated so the **fault-window Va
sits at 0°**, with `VAN`/`VA`/`Van`/`V_A` preferred and the first voltage
channel as fallback. Pre-fault phasors are rotated by the *same* angle, so
pre/fault angles are directly comparable. Windows are one cycle each, butted
against inception.

## Channel naming

Channels are auto-detected via `config.json` → `channel_keywords`, with
per-call candidate tuples in `analysis.py` (`_find_channel`, `_find_raw`).
Matching is case-insensitive but **not** fuzzy. A new relay export with
unfamiliar channel names is a config/keyword problem — extend the keyword list.

## WSO / EPSS — what the analysis is actually for

The goal is **reliability risk screening, not a filing**: find faults that ride
through today so they can be mitigated before a WSO day.

EPSS does two things — it disables reclosing *and* makes the relay more
sensitive/faster. Both create conversions, and they are not the same:

| Normal day | EPSS day | Class |
|---|---|---|
| below pickup, downstream fuse clears | trips the recloser | `EPSS_CANDIDATE` — **new** outage, whole downstream section |
| trips, reclose succeeds | trips, no reclose | `WSO_EXPOSED` — momentary → sustained |
| trips, locks out | same | `PERMANENT` — no change |
| record ends before any dead time | unknowable | `INDETERMINATE` |
| no fault current | nothing | `NOT_EXPOSED` |

`EPSS_CANDIDATE` is the one the tool exists to surface. It cannot be confirmed
without the device's normal and EPSS pickup settings — that work is pending a
SUBNET settings export.

**Do not treat a no-trip event as a misoperation.** It is `Rode Through` at
Priority 2, pointing at coordination review.

`INDETERMINATE` exists because "no reclose in the record" ≠ "no reclose
happened". `_MAX_DEAD_TIME_MS` (5 s) sets the cutoff.

## Tuning the triage rules

`triage.py` is the single source of truth. To change the scheme, edit
**`_FLAGS`** (priority, label, why it matters) and **`_TRIGGERS`** (how it
fires, which setting tunes it) side by side. `rule_table()` joins them,
`fleet_analyze`/`batch` export it as `triage_rules`, and the dashboard renders
it. Do **not** re-introduce a flag→priority map in the template; a test fails.

When adding a flag, also append to `evidence[...]` inside `triage_event()` so
the per-event "Why Priority N" block shows the measured value against the
threshold.

Priority is the minimum over the flags that fired; `reasons[].decisive` marks
which ones set it.

**Clearing time is measured against TWO standards, and they stack.**

| Flag | Default | Standard |
|---|---|---|
| `slow_trip` | `triage.slow_trip_cycles` = **30 cyc** (500 ms at 60 Hz) | wildfire / EPSS |
| `over_clearing_standard` | `triage.clearing_standard_s` = **2 s** (120 cyc) | everyday Tier 1 non-wildfire |

A trip can miss the first and meet the second. Past 2 s both fire.
`DEFAULT_SLOW_TRIP_CYCLES` / `DEFAULT_CLEARING_STANDARD_S` in `triage.py` are
the single source; `config.json`, both argparsers, the GUI namespace and
`fleet_gen.SLOW_TRIP_MS` all read them.

The two travel together as `triage_opts`; a second positional float in that
tuple is how `batch` and `fleet_analyze` would drift.

**`fleet_gen` had to move with it.** `slow_ms` is now 0.55-1.40 s; a new
`over_clearing` template at 2.2-3.4 s exercises both rungs.

## Feeder connectivity

`topology.py` is the mainline model: **connectivity only** — no impedance, no
laterals, no fuses, no load flow.

Format: one row per node, five columns:

```
feeder,node_id,kind,parent,tie_to
```

- `kind` — `source | breaker | recloser | sectionalizer | pmh | tie`
- `parent` — the node immediately upstream; empty only on a source
- `tie_to` — the far-end node; read on `kind=tie` rows only
- `model` — `PMH-9` / `PMH-11` / `PMH-10`; read on `kind=pmh` rows only

Each protective device owns the section immediately downstream. Normally-closed
edges form one tree per source; `kind=tie` rows are normally-open edges.

**Customers are not in this file.** They stay in `devices.csv`
(`customers_served`); `customers_below()` sums the subtree.

`subtree()` stops at a normally-open tie. `cross_ties=True` is the
post-restoration view.

Lookups are punctuation- and case-insensitive via `wso_impact._normalize`,
which is **imported, not re-implemented**.

`validate()` returns diagnostics-shaped findings (symptom, evidence, fix) for:
duplicate ids, typo'd parent, loop, orphan branch, tie with no far end, tie
authored from both sides, feeder name mismatch. A test enforces the fix text.

**`topology_builder.html`** (`comtrade-topology --build`) ensures every parent
and tie is a dropdown of already-entered rows. It carries **no second
validator** — everything real belongs to `topology.validate()`.

**Sidecars are found from the events folder.** `fleet_analyze.find_sidecar()`
looks one level up only when the folder is named `incident_events`/`events`.
**Both `batch.py` and `fleet_analyze` need this wiring.**

`fleet_gen` generates `topology.csv` and `devices.csv` from the same tables so
they cannot drift. `demo/topology.csv` is tracked; a real `topology.csv` is
gitignored.

**`customers_served` is a device's OWN section.** `customers_below()` is what
a trip actually drops; `fleet_analyze` puts that on every event as
`customers_affected`.

## Device naming

- **Breakers**: `BKR_<four letters of feeder name><circuit>` — `_breaker_id()`
- **Reclosers**: `RCL_###-###` from `_grid_num(circuit * 100 + offset)`. Offsets: 0 head, 1..n trunk, 20+ branch, 50+ ties, 70 cabinets.
- **PMH cabinets**: `PMH_###-###` same grid; ways are that plus `_W1`, `_W2`, …
- **Ties are reclosers**, named like them. No `TIE_` prefix.

**A feeder sits on the bus it is named for.** A feeder alone on its bus heads
with a **breaker**. This rule is absolute and a test enforces it outright.

`_TIES` names its endpoints by **(feeder, offset)**, not by device id.

**Tests must derive ids from the tree, never write them down.**
`TestIncidentGrouping._ev()` asserts the device exists (`unknown=True` for the
deliberate no-topology case).

## Automatic PMH cabinets

`PMH_WAYS` = **PMH-9: 2, PMH-11: 3, PMH-10: 4**. Only automatic cabinets are
mapped, and only their switch ways.

**Every way is its own row** — one row per way, not one per cabinet. Ways of one
enclosure share a `cabinet` id and agree on `model`.

`validate()` checks: `cabinet_over_connected`, `cabinet_model_disagrees`,
`way_without_cabinet`.

**A PMH way switch is a load-interrupter, not a protective device.** It is not
in `RECORDING_KINDS` — `fleet_gen.build_incident` never picks one as an event
origin. A cabinet with events would be a bug; a test says so.

## Relay settings

`relay_settings.py` reads the SUBNET export as a **catalog** — a flattened
table, one row per relay.

- Pickups are **secondary**; `primary = pickup * CTR`. Comparisons use RMS
  current over the fault window, never peak.
- The normal-day group is `NOMINAL_SG`. **The EPSS group is inferred** (most
  sensitive populated group outside the nominal one).
- `TemplateDate` holds the template *name*, not a date. Keep the trip/location
  mode regex lazy so it doesn't cross a digit.
- `sanity_check()` returns diagnostics-shaped findings: missing CTR, missing
  NOMINAL_SG, a single populated group, pickups whose magnitude suggests they
  are already primary.

Settings turn `EPSS_CANDIDATE` from a guess into a verdict.

## Binding real events to devices

**An event's device id is CFG line 1, field 2 (`rec_dev_id`).** `devices.csv`
has an **`aliases`** column for the strings relays emit, separated by `;` or
`|`.

`fleet_analyze.resolve_devices()` **must run before `group_events`** — a test
asserts the ordering in both entry points. It rewrites `device_id` to the
canonical name and keeps the original in `device_id_raw`.

Unmatched ids are an **error-level** data-quality finding. An alias claimed by
two devices is also an error; the first row in `devices.csv` keeps it.

## Diagnostics

`diagnostics.py` exists because a batch that reports "0 events analyzed" with
no reason is the worst outcome. Every finding must carry: symptom, evidence,
fix — a test enforces the fix text.

**A diagnostic must mirror what the analysis actually does.** `phase_currents_unnamed`
replicates the real candidate tuples from `analysis._find_channel`. If you
change those tuples, change this too.

`check_record()` runs on every parsed record; `explain_parse_error()` turns an
exception into a remedy. `batch.sweep` rolls findings up by code.

## Ground truth must track the classifier

`fleet_gen.expect_wso` and `wso_impact.classify_event` must agree.
`TestTheGeneratorsGroundTruthMatchesTheClassifier` guards it. Changing the
classes means changing the generator and regenerating `demo/fleet_truth.json`
in the same commit.

## Plotting releases its figures

Every function in `plotting.py` calls `plt.close(fig)` after `savefig` and
returns `None`; the interactive branch still returns the figure. This is
load-bearing: leaving figures open leaked ~45 MB per event.

`TestPlottingReleasesItsFigures` guards it. If you add a plotting function,
close its figure on the save path.

## The feeder one-line

The `#onelineCard` section draws the selected feeder from `FLEET.topology`,
which `fleet_analyze` embeds as **nodes, not a path**.

**Fill is switch STATE: red closed, green open** (`--sw-closed` / `--sw-open`).
This is the opposite of the traffic-light instinct — don't "fix" it. Fill
cannot carry review priority; that moved to a badge above the device. State is
colour alone on the drawing; the word `OPEN`/`CLOSED` survives in **tooltips
and `aria-label`** — a test asserts it stays there.

**Every box is filled** with its state colour and carries a black letter.
`OL_LETTER` / `olDevice`: `B` breaker, `R` recloser, `S` sectionalizer. No
circles.

**A tie draws as a box with `R`**, green because normally open. It is laid out
as a child at the end of the line — one column further out than the device it
hangs off, with the run to it dashed. Real devices are walked before ties so
the mainline keeps the parent's row.

`L.devices` excludes ties; `L.ties` is just them; `L.placed` is both.

A tie is labelled with its own id and the **feeder it backfeeds from** beneath.
The far device id is in the tooltip.

**`olDraw()` returns its record count and never writes a caption.** It fills
thirteen cards on the all-feeders page.

**Two pages, one drawer.** `#pageNav` switches between fleet review and
`#pageFeeders`. Both go through `olDraw(host, feeder, opts)`. A test counts the
definitions.

**`olRefresh()` redraws the diagram only on the review page.** Rebuilding the
incident list there would fight the click that triggered it.

**`[hidden] { display: none !important; }` is load-bearing.** `hidden` is a UA
rule at element specificity, so any class rule setting `display` beats it.

**`applyStation()` must sync both dropdowns** (`#fStation` and `#feedersSub`)
and redraw every panel that reads `AGG`. These used to hold separate state.

**Feeders sort by circuit number, never by name** — `olCircuit()` /
`olFeederCmp()`, used by the review dropdown *and* the all-feeders page. A
utility knows a feeder by its number.

With no incident selected the drawing shows **normal state** — every protective
device closed, every tie open. Selecting an incident opens the clearing device
and closes the restoring tie.

**No device id, feeder name or tie name may appear in the template.** A test
scans for `BKR_`, `RCL_`, `TIE_`, `BUS_`.

**A tie can be walked through.** `olJumpThroughTie()` / `olRevealTie()` handles
navigation. The all-feeders page passes `opts.onDevice` so a click filters the
table and returns you to it.

Build card shells before drawing into them: an SVG sized from a container that
is still `display:none` comes out at the fallback width.

## N-1: the contingency view

`#olOutage` takes one device out of service. With no live switching state and no
load flow, the island is the subtree and any N.O. tie inside it can restore it.

- **Restoration feeder is chosen, not assumed** (`#olRestore`). Only the chosen
  tie draws closed; closing every one at once is not a switching plan.
- **The gap is the point.** A section with no tie is customers who stay out.
  `olGapSummary()` reports that per feeder.
- **It does not claim the transfer is feasible.** Report the count, not a
  verdict. A test holds the wording.
- The contingency and incident overlay are **mutually exclusive**.
- `olSubtree()` in the page mirrors `topology.subtree()`: a N.O. tie is a leaf.

Per-device customers travel in the **topology payload**, not the event table.

## Dashboard cross-filtering

Chart marks carry `data-click` and are registered through `clickRef()` /
`bindClicks()`. Both maps are cleared together on resize.

`applyDrill()` **clears before it sets.** Where a dropdown exists (zone, fault,
priority, EPSS class) the drill sets it. Other dimensions become the removable
`#drillChip` predicate, which `currentRows()` applies alongside the selects.

SVG elements have no `.click()` method; tests must use
`dispatchEvent(new MouseEvent("click", {bubbles:true}))`.

## Scoping the review to one feeder

**The one-line's own feeder dropdown is the page scope.** `aggregate_by_station()`
and `aggregate_by_feeder()` pre-compute aggregates; the page swaps `AGG`/`TOT`.
**Narrowest wins**: feeder, else substation, else fleet.

`applyStation()` must redraw **every** panel that reads `AGG` — hero, units,
tiles, charts — plus the table and the one-line. A test lists the calls.

Key traps:
- **Never rebuild a `<select>` inside its own `change` handler.** `fillSelect()`
  rebuilds only when the option list actually changed.
- **Feeder names are matched on a normalised key** (`fkey()` / `feederAgg()`).
  A mismatch splits the page: panels that count events narrow, those that read
  an aggregate do not. `topology.validate()` reports `feeder_name_mismatch`.
- **Never hand a panel a WIDER aggregate than its own caption.** `deriveAggregate()`
  counts scoped events instead. It reports customer-hours as unavailable and
  marks itself `derived: true`.
- **A scope pick is never refused.** `applyFeederScope` always applies; tiles
  fall back to the wider aggregate and a note names the split.
- **A panel that filters `EV` itself keeps showing the fleet while everything
  around it narrows.** Everything that counts events goes through `scopedEvents()`.
  A test greps the render block for stray `EV.filter` / `EV.slice` / `EV.forEach`.
- The one-line offers **only that substation's feeders** when scoped.
- Following a tie to another substation calls `applyStation()` and must set
  `$("fStation").value`.

**There is ONE substation scope**, the `station` variable, shared by both pages.

## The GUI is a launcher

**`batch.sweep` has two callers.** `batch.main` uses argparse; the GUI assembles
a `SimpleNamespace`. Add an option to the parser and the GUI is silently short
of it. Read optional args as `getattr(args, name, default)`.
`TestTheGuiAndTheCliAgreeOnSweepsArguments` walks both files and fails on any
bare `args.X` the GUI does not set.

Folder mode runs `batch.sweep` and opens the dashboard. Single-file mode keeps
the detailed plot/report path.

## The GUI, matplotlib and Tk

**`plotting.py` must never call `matplotlib.use()`.** `app.py` pins `"Agg"`;
the force overrode it, ran the interactive Tk backend in the worker thread, and
the process died with SIGSEGV in `Tcl_DeleteHashEntry`.
`TestPlottingDoesNotForceAnInteractiveBackend` guards both halves.

**Cross-thread UI updates go through `COMTRADEApp._ui_call()`**, never a bare
`self.after()`. `_on_close` sets `_closing` and is wired to `WM_DELETE_WINDOW`.

## macOS GUI dialogs

`COMTRADEApp._come_to_front()` must be called before every modal dialog. It
activates the process via `osascript` so the panel doesn't open behind the
window. It also runs 120 ms after launch.

## Real exports break in ways the fixtures never will

Four traps that took down a whole folder run:

- **A backslash escape inside an f-string expression** is a SyntaxError before
  Python 3.12. Hoist the escape into a variable; a test scans `topology.py`.
- **`np.max` on an empty channel** raises rather than returning zero.
- **The date format.** The spec is `dd/mm/yyyy`; SEL writes `mm/dd/yyyy`.
  `_parse_comtrade_dt` tries standard order, falls back to the reading that
  yields a real date.
- **`analyze_one` guards the analysis, not just the parse.** Report that file
  and keep going.

Four more from the standard (`TestTheParserFollowsC37111`):

- **Four data file types, not two** (7.4.9): ASCII, BINARY, BINARY32, FLOAT32.
  `_ANALOG_DTYPE` is the table; dispatch is `file_type in _ANALOG_DTYPE`.
- **`nrates = 0` still has a rate line** (7.4.7): `range(max(n_rates, 1))`.
- **Time comes from the CFG sample rate, not the DAT timestamp column** (7.4.7):
  `_time_from_rates` is tried first; timestamps are the fallback.
- **The date/time stamp may be blank or zero-filled** (7.4.8). `NO_DATETIME` is
  the sentinel.

And one silent: **a null analog field** (8.4) left channels of different lengths.
`_parse_dat_ascii_lines` stages a whole row and commits it only if every field
parsed. `_build_record` truncates to the shortest channel.

`EXPORT_GUIDE.md` is the user-facing half — keep it in step with the GUI's Help
dialog.

## Windows portability

- **Pin `encoding="utf-8"` on every text open/read_text/write_text.**
  `TestTextIOPinsItsEncoding` scans the package for this.
- **A process pool needs spawn.** `batch.sweep` catches a broken pool and
  finishes serially; the GUI passes `jobs=1`.
- Keep `.bat` (Windows) and `.command` (macOS) launchers in step.

## Install / sharing

Windows PowerShell 5.1 rejects `&&`, has no `source`, and blocks `Activate.ps1`
under the default execution policy. README and GIT_GUIDE carry separate
PowerShell, cmd.exe and POSIX blocks, one command per line. The documented
Windows path skips activation and calls `.\.venv\Scripts\python.exe` directly.

`GIT_GUIDE.md`, `check_install.py`, and `.bat` launchers are the sharing
convention: `git clone` + `pip install -e .` so `git pull` updates in place.
`install_shortcut.py` must never write `COMTRADE Analyzer.bat` (tracked file).

## Icons and desktop shortcuts

Three traps in `make_icon.py` / `install_shortcut.py`:

- **Save the .ico from the LARGEST frame.** `_verify_ico()` reads the directory
  back and exits non-zero if a size is missing.
- **`bitmap_format="bmp"`.** Windows skips PNG entries smaller than 256x256.
- **`IconLocation = '<path>,0'`, then read it back.** Without the index Windows
  draws the target's icon (a .bat gets generic gears).

## Incidents — one fault, several records

A fault produces a record at the clearing device, at every upstream device that
correctly did not trip, and — after lockout — on a neighbouring feeder when a
tie picks the section back up. `fleet_gen.build_incident` emits all of them.

**`incident_id` lives in `fleet_truth.json` only.** A relay has no idea the
other records exist; the pipeline re-derives grouping from time + topology.

## Incident grouping

Two joins, not the same rule:

- **Same fault** — inside `window_s` (config `incidents.window_s`, 2 s) **and**
  `on_same_path()`. Both are required.
- **Restoration** — a `LOAD` record on the far side of a tie inside
  `restore_window_s` (15 min). Purely topological.

With **no topology** it degrades to same-feeder-and-window, which over-merges.

`clock_suspects()` reports pairs that share a path and fault type but miss the
window — a relay minutes or a timezone out looks exactly like a separate fault.

**`upstream_also_tripped` is an observation, not a verdict.** The tool surfaces
it; the engineer decides. This is not a FLISR verification machine.

## The LOAD class — cold load is not a three-phase fault

`classify_fault` returns **`LOAD`** for a balanced current rise with voltage
still up. `_voltage_held_up()` gates the 3PH branch on it (`_LOAD_STEP_V_RATIO`,
0.85). With **no voltage channels it stays 3PH**. This did not move any real
fault: all five generators classify unchanged.

## The demo set

`demo/` is tracked — 201 synthetic records from 115 incidents, registry,
topology, ground truth, and a pre-built `demo_dashboard.html`.

Records live in **`demo/incident_events/`**. Regenerate with
`fleet_gen --count 115 --seed 20260601`. **`--count` counts incidents.**
Rebuild the HTML after any dashboard change.

Any dashboard built from a set with `fleet_truth.json` shows a DEMO DATA badge.

## Feedback

Both the GUI (`✉ Feedback`) and the dashboard footer open a `mailto:` draft —
neither sends anything. Attached context is counts, versions and settings —
**never device names or event filenames**.

## Operational data

`devices.csv` is real operational data and is gitignored. Never commit a
populated registry, and don't put real device IDs, feeder names or customer
counts in test fixtures or commit messages.

## Telling a stale page from a fresh one

1. **The browser reuses a cached copy.** Every `webbrowser.open` appends
   `?v=<template hash>`. The page carries `Cache-Control: no-store`.
2. **The tool is running an INSTALLED copy.** `stale_install_warning()` fires
   when the running package is in site-packages *and* a git checkout sits at or
   above the working directory. Every entry point prints it.

Every render prints `page <hash> from <path>`. `template_stamp()` hashes the
template file itself.

## Keeping this file current

Update in the **same commit** as the change it describes; revise the relevant
section rather than appending.

`TestTheProjectNotesStayCurrent` guards: every module appears in the map, every
EPSS class is mentioned, superseded wording stays gone, and **this file is
≤ 40,000 characters**. It cannot check whether the prose is *true*.

## Conventions

- Thresholds belong in `config.json`, not inline.
- Classification is "not a replacement for a relay algorithm" — keep that
  framing in docstrings and report prose.
- 60 Hz is a default: use `record.line_freq()` and `record.samples_per_cycle()`.
