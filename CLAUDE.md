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
relative (`from .analysis import ...`). Small and cleanly layered — you can read
any of these whole.

```
comtrade_parser.py   IEEE C37.111 CFG + DAT reader (ASCII and binary)
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

Root `main.py` and `app.py` are two-line shims so `python main.py` and the
desktop shortcut keep working from a checkout. Console entry points are declared
in `pyproject.toml`; `pip install -e .` for development.

**When editing `dashboard_template.html`:** it holds three `<script>` blocks, and
a stray edit into one of them is not caught until the page loads. Syntax-check
before rendering:

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
This predates the packaging work (verified against pristine `HEAD`). The honest
fix is the fixture, not the threshold: drop `I_FAULT_BC` from 140 to ~110 A so
the unfaulted phases carry roughly load current, as they physically would. Do
**not** move the 0.15 threshold to make it pass.

Plots block on `plt.show()` unless `--save-plots` is passed, so headless
regression runs need `--save-plots` and `MPLBACKEND=Agg`. There is no pytest suite; if you
add one, `classify_fault` and the sequence math are the places it pays off.

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

Phase rotation is assumed **ABC**. There is no ACB handling anywhere; on an ACB
system I1 and I2 swap. If that ever needs supporting, it belongs in one place
here, not sprinkled through the callers.

`compute_sequence_components()` returns **magnitudes only**, NaN-padded for the
first `window_samples - 1` samples — use `np.nanmedian`/`np.nanmax`, never bare
`np.median`. `compute_phasors_at()` returns **complex** `seq_i`/`seq_v` in its
dict; that's the one to use when angle matters.

## Phasor reference

All phasors from `compute_phasors_at()` are rotated so the **fault-window Va sits
at 0°**, with `VAN`/`VA`/`Van`/`V_A` preferred and the first voltage channel as
fallback (`ref_channel` in the returned dict says which was used). Pre-fault
phasors are rotated by the *same* angle, so pre/fault angles are directly
comparable. Windows are one cycle each, butted against inception: fault window
starts at inception, pre-fault window ends at it.

## Channel naming

Channels are auto-detected from names via `config.json` → `channel_keywords`,
with per-call candidate tuples in `analysis.py` (`_find_channel`,
`_find_raw`). Matching is case-insensitive but **not** fuzzy. A new relay export
with unfamiliar channel names is a config/keyword problem — extend the keyword
list; don't add another hardcoded tuple.

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

`EPSS_CANDIDATE` is the one the tool exists to surface, and it is usually the
*larger* outage: a lateral fuse drops 30 customers, the recloser above it drops
the whole feeder section. It cannot be confirmed without the device's normal
and EPSS pickup settings — that work is pending a SUBNET settings export.

**Do not treat a no-trip event as a misoperation.** It was `no_trip` at
Priority 1 with "possible relay misoperation" wording; on real rising-edge
current triggers that would flag every healthy below-pickup record as urgent.
It is now `Rode Through` at Priority 2, pointing at coordination review.

`INDETERMINATE` exists because "no reclose in the record" was being read as "no
reclose happened". In this fleet 38 of 40 such records end a median 190 ms after
the trip, while measured dead times run 455–2050 ms — none of them could have
shown a reclose. `_MAX_DEAD_TIME_MS` (5 s) sets the cutoff.

## Tuning the triage rules

`triage.py` is the single source of truth and the dashboard renders it. To
change the scheme, edit **`_FLAGS`** (priority, label, why it matters) and
**`_TRIGGERS`** (how it fires, which setting tunes it) side by side — they are
adjacent on purpose. `rule_table()` joins them, `fleet_analyze`/`batch` export
it as `triage_rules`, and the page's Triage rules card renders it. Do **not**
re-introduce a flag→priority map in the template; a test fails if you do.

When adding a flag, also append to `evidence[...]` inside `triage_event()` so
the per-event "Why Priority N" block can say what actually triggered it — a
bare label without the measured value against the threshold is what made the
priorities opaque in the first place.

Priority is the minimum over the flags that fired; `reasons[].decisive` marks
which ones set it, and the page greys the rest.

## Feeder connectivity

`topology.py` is the mainline model: **connectivity only** — no impedance, no
laterals, no fuses, no load flow. It exists to answer what event files alone
cannot: are two records the same fault seen at two depths, what goes dark when
a device opens, and which feeder could back it up.

The format is one row per node, five columns, because real topology gets typed
into a spreadsheet from a wall map — there is no GIS export:

```
feeder,node_id,kind,parent,tie_to
```

- `feeder` — the feeder this node belongs to; the **station name** on a source row.
- `node_id` — must match `device_id` in `devices.csv` for anything that records.
- `kind` — `source | breaker | recloser | sectionalizer | tie`.
- `parent` — the node immediately upstream; empty only on a source.
- `tie_to` — the far-end node; read on `kind=tie` rows only.

Each protective device **owns the section immediately downstream of it**, so a
feeder is a source, a head device, two or three mid-line reclosers and its ties
— six or seven lines. Normally-closed edges form one tree per source; `kind=tie`
rows *are* the normally-open edges, and closing one re-parents a subtree onto
another feeder.

**Customers are not in this file.** They stay in `devices.csv`
(`customers_served`); `customers_below()` sums the subtree from the registry.
Two places to edit a customer count is how they drift.

`subtree()` stops at a normally-open tie — a N.O. tie carries nothing, and
traversing it would double the outage on every lockout. `cross_ties=True` is the
post-restoration view.

Lookups are punctuation- and case-insensitive via `wso_impact._normalize`, which
is **imported, not re-implemented** — a second copy is exactly how the
diagnostics channel check drifted from the analysis it mirrored.

`validate()` returns diagnostics-shaped findings (symptom, evidence, fix) for the
mistakes hand-authoring actually produces: duplicate ids, a typo'd parent, a
loop, an orphan branch, a tie with no far end, a tie authored from both sides.
A test enforces the fix text, same bar as `diagnostics.py`.

The tool does **not** simulate FLISR — that runs in the ADMS. It draws the
feeder and places events on it; the engineer judges whether the scheme behaved.

`demo/topology.csv` is tracked (invented, matching `demo/devices.csv`); a real
`topology.csv` is gitignored operational data, and
`comtrade_analyzer/topology_template.csv` is what you copy.

**Sidecars are found from the events folder.** The docs tell people to point at
the events folder, and the registry and topology sit *beside* it, not in it.
`fleet_analyze.find_sidecar()` looks one level up, but only when the folder is
named `incident_events`/`events` — otherwise it would drag in an unrelated
parent's files. `batch.py` shipped with no topology lookup at all, so the
documented command produced a dashboard whose feeder pages could never appear.
**Both entry points need wiring, not just `fleet_analyze`.**

`fleet_gen` generates `topology.csv` and `devices.csv` from the same
`_SUBSTATIONS` / `_FEEDERS` / `_TIES` tables, so the demo's tree and its
registry cannot drift. Hand-authored files are the other path; the template is
what you copy for those.

**`customers_served` is a device's OWN section, not its whole feeder.** What a
trip actually drops is the subtree below it, which is `customers_below()` —
`fleet_analyze` puts that on every event as `customers_affected`. Without a
topology the two are equal, which is the right fallback for a plain folder.

## Relay settings

`relay_settings.py` reads the SUBNET export as a **catalog**, not a native SEL
settings file — it is a flattened table, one row per relay.

- Pickups are **secondary**; `primary = pickup * CTR`. Comparisons use RMS
  current over the fault window, never peak: peak carries DC offset and would
  overstate the multiple.
- The normal-day group is `NOMINAL_SG`. **The EPSS group is inferred** (most
  sensitive populated group outside the nominal one) and every surface says so.
  If a column ever identifies it explicitly, read that instead of inferring.
- `TemplateDate` holds the template *name*, not a date. The trip and location
  modes run together (`3PhTrip3PhLoc`) — keep the letter run lazy and stop it
  crossing a digit, or the Loc match swallows the Trip mode.
- `sanity_check()` returns diagnostics-shaped findings: missing CTR, missing
  NOMINAL_SG, a single populated group, and pickups whose magnitude suggests
  they are already primary.

Settings turn `EPSS_CANDIDATE` from a guess into a verdict; a ride-through that
cannot reach the EPSS pickup either is reclassified `NOT_EXPOSED`.

## Diagnostics

`diagnostics.py` exists because a batch that reports "0 events analyzed" with
no reason is the worst outcome on someone else's machine. Every finding must
carry three things — symptom, evidence, fix — and a test enforces the fix text.

**A diagnostic must mirror what the analysis actually does, not approximate
it.** The first version classified channels by *units*; `analysis._find_channel`
matches by *name*. A vendor file with units "A" and channels called `CH1_ANLG`
passed the check and then classified every event UNKNOWN. `phase_currents_unnamed`
now replicates the real candidate tuples. If you change those tuples, change
this too.

`check_record()` runs on every parsed record; `explain_parse_error()` turns an
exception into a remedy. `batch.sweep` rolls findings up by code so one bad
export setting is reported once, not ten thousand times, and the dashboard
shows them as a banner above everything else.

## Ground truth must track the classifier

`fleet_gen.expect_wso` and `wso_impact.classify_event` have to agree. When the
model gained EPSS_CANDIDATE and INDETERMINATE the generator was not updated and
the demo build shipped showing 60% detector agreement — the panel that exists
to prove the tool works. `TestTheGeneratorsGroundTruthMatchesTheClassifier`
guards it. Changing the classes means changing the generator and regenerating
`demo/fleet_truth.json` in the same commit.

## Plotting releases its figures

Every function in `plotting.py` calls `plt.close(fig)` after `savefig` and
returns `None`; the interactive branch (`plt.show()`) still returns the figure
because the caller's window owns it. This is load-bearing, not tidiness:
pyplot holds a global reference to every figure it creates, no caller uses the
return value on the save path, and leaving them open leaked ~45 MB per event.
A 100-event folder run through the GUI reached multiple GB, went to swap, and
the window stopped redrawing — reported as "the app screen blacks out".

`TestPlottingReleasesItsFigures` guards it. If you add a plotting function,
close its figure on the save path.

## The feeder one-line

The `#onelineCard` section draws the selected feeder from `FLEET.topology`,
which `fleet_analyze` embeds as **nodes, not a path** — the dashboard is one
self-contained file and cannot go back to `topology.csv` on someone else's
machine.

- Layout is orthogonal and left-to-right: substation bus at the left, depth is
  the column, a branch takes its own row. Never a diagonal — that is not how a
  one-line is drawn.
- A breaker is square, a recloser round, a sectionalizer a diamond, so the
  symbol carries the kind without reading the legend. Fill is the worst
  priority among that device's records *in the current filter*, which is why
  `olRefresh()` hangs off every filter change and off `applyDrill`.
- A tie is authored once, from whichever side, so `olLayout` anchors it to
  whichever end is on the drawn feeder and labels the other. The label flips to
  the left of the stub near the right edge instead of being clipped.
- Selecting an incident rings the clearing device solid and the devices that
  held dashed, dims the rest, and drills the table to that incident's records.

**No device id, feeder name or tie name may appear in the template.** Those are
operational data; a leaked one ships to everyone who clones the repo. A test
scans for `BKR_`, `RCL_`, `TIE_` and `BUS_`.

`olRefresh()` redraws the diagram only on the review page. Rebuilding the
incident list there would fight the click that triggered it.

**`[hidden] { display: none !important; }` is load-bearing.** `hidden` is a UA
rule at element specificity, so *any* class rule setting `display` beats it.
`.pagenav` did exactly that and the tab showed with no listeners attached — a
visible control that does nothing.

**Two pages, one drawer.** `#pageNav` switches between the fleet review and
`#pageFeeders`, which stacks every feeder grouped by substation. Both go through
`olDraw(host, feeder, opts)` — a second copy of the layout is how the two would
drift apart, and a test counts the definitions. The all-feeders page passes
`opts.onDevice` so a click there filters the table *and* returns you to it.

Build the card shells before drawing into them: an SVG sized from a container
that is still `display:none` comes out at the fallback width.

A device can back up more than one feeder, so ties on one anchor get a `slot`
each and drop to their own depth. Without it the labels sit on top of each
other and read as noise — `L.tieSlots` reserves the lane height.

## Dashboard cross-filtering

Chart marks carry `data-click` and are registered through `clickRef()` /
`bindClicks()`, mirroring the tooltip registry. Both maps are cleared together
on resize.

`applyDrill()` **clears before it sets** — the other order wiped the value it
had just assigned, and every dropdown-backed drill silently did nothing while
the chip-backed ones worked. Where a dropdown exists (zone, fault, priority,
EPSS class) the drill sets it so the control shows what happened; dimensions
with no dropdown (shot count, clearing band, one device) become the removable
`#drillChip` predicate, which `currentRows()` applies alongside the selects.

Note SVG elements have no `.click()` method, so a test harness must
`dispatchEvent(new MouseEvent("click", {bubbles:true}))`. Real user clicks fire
the listener normally.

## The GUI is a launcher

Folder mode runs `batch.sweep` and opens the dashboard; it renders nothing per
event. The old per-event folder loop is what leaked figures until the window
stopped redrawing. Single-file mode keeps the detailed plot/report path.

The dashboard carries everything the Word report used to show — provenance,
per-channel peaks, phasor diagram, digital operations log, DC offset — via
`extract_report_detail()` in `fleet_analyze.py`. The .docx is no longer the way
anyone looks at an event; treat it as legacy output, not the deliverable.

## The GUI, matplotlib and Tk

**`plotting.py` must never call `matplotlib.use()`.** It used to force
`"TkAgg"`, and because `app.py` imports it *after* pinning `"Agg"`, the force
won — so the GUI ran the interactive Tk backend while the worker thread
rendered figures. Tk is not thread-safe; that corrupted the Tcl interpreter and
the process died with SIGSEGV in `Tcl_DeleteHashEntry` under `Tcl_DeleteInterp`
when the user quit, half an hour after the run that caused it. The backend
belongs to the caller: `app.py` pins Agg, the CLI takes matplotlib's default.
`TestPlottingDoesNotForceAnInteractiveBackend` guards both halves.

**Cross-thread UI updates go through `COMTRADEApp._ui_call()`**, never a bare
`self.after()`. The analysis thread outlives a window close, and callbacks
landing in a torn-down interpreter is the other half of the same crash.
`_on_close` sets `_closing` and is wired to `WM_DELETE_WINDOW`.

## macOS GUI dialogs

`filedialog.askdirectory()` / `askopenfilename()` open a native panel owned by
our process. A Python launched from a .app bundle or a Terminal is frequently
not the *active* application, and the panel then opens **behind** the main
window — the dialog is modal, so the window it hides stops responding and the
app looks frozen with an unclickable Finder panel somewhere underneath.

`COMTRADEApp._come_to_front()` fixes it and must be called before every modal
dialog. Raising the Tk window is not sufficient on its own: the *process* has to
be activated, which needs `osascript ... set frontmost of (first process whose
unix id is <pid>)`. It also runs 120 ms after launch so the main window does not
come up behind whatever the user was looking at.

## Windows portability

Developed on macOS, run on Windows. Three things that only fail over there:

- **Pin `encoding="utf-8"` on every text open/read_text/write_text.** Windows
  defaults to cp1252; `dashboard_template.html` carries °, →, ±, Ω, ∠ and box
  drawing, so an unpinned read raises there and nowhere here.
  `TestTextIOPinsItsEncoding` scans the package for this.
- **A process pool needs spawn**, which re-imports the caller's `__main__` in
  every worker. `batch.sweep` catches a broken pool and finishes serially; the
  GUI passes `jobs=1` outright — 100 events take 1.2 s serial vs 0.7 s pooled,
  which is not worth the fragility inside a GUI.
- `.bat` is Windows-only, `.command` is macOS-only. Keep both in step.

## Install instructions are platform-specific

Windows PowerShell 5.1 ships with Windows and is what colleagues will use. It
rejects `&&` between commands (*"The token '&&' is not a valid statement
separator"*), has no `source`, and blocks `Activate.ps1` under the default
execution policy on a managed PC. A single "works everywhere" install line does
not exist — README and GIT_GUIDE both carry separate PowerShell, cmd.exe and
POSIX blocks, one command per line.

The documented Windows path deliberately **skips activation** and calls
`.\.venv\Scripts\python.exe` directly, because activation is the step most
likely to fail and is not required. Keep it that way.

## Icons and desktop shortcuts

Three traps here, all learned the hard way in pq-analyzer and replicated in
`make_icon.py` / `install_shortcut.py`. They are generated on a Mac and consumed
on Windows, so nothing on the machine that writes them notices a mistake.

- **Save the .ico from the LARGEST frame.** Pillow silently drops every
  requested size larger than the image being saved, so `frames[0].save(...)`
  on the 16 px frame yields a one-entry 16x16 file and Windows falls back to
  the interpreter's own icon. `_verify_ico()` reads the directory back and
  exits non-zero if a size is missing.
- **`bitmap_format="bmp"`.** Windows only reads PNG inside an .ico at 256x256
  and skips — not fails, skips — smaller PNG entries.
- **`IconLocation = '<path>,0'`, then read it back.** Without the index Windows
  draws the target's icon, and the target is a .bat, whose icon is the generic
  gears. `Save()` reports nothing when the shell declines a value.
- **Delete the .lnk before recreating it**, and call `SHChangeNotify` after.
  A rewritten .lnk keeps its cached bitmap, which looks exactly like the
  installer not having run.

`install_shortcut.py` must never write `COMTRADE Analyzer.bat`. The .bat is a
tracked file; generating a second copy is how the two drifted apart in
pq-analyzer, and you got whichever half depending on what you ran last.

## Incidents — one fault, several records

A fault does not produce one event file. It produces a record at the device
that cleared it, a record at every device between there and the substation that
saw the same current and correctly did **not** trip, and — after a lockout —
a record on a *neighbouring feeder* when a tie picks the stranded section back
up. `fleet_gen.build_incident` emits all of them.

What holds a set together, and what does not:

- **Same fault current.** On a radial mainline there is no branch between the
  devices on one path, so every device upstream of the fault sees the *same*
  magnitude. What differs is pre-fault **load** — an upstream device feeds
  everything below it — and that is topology, not impedance.
- **Load steps down** on a witness record after the device below opens.
  `Shot.load_after` carries it. Leaving load flat across a downstream trip is
  the detail a protection engineer spots first.
- **Fault current is sized against the feeder head**, not the device that
  cleared. Every device on the path needs unfaulted/faulted RMS under the
  classifier's 0.15 gate, and the head carries the most load — size it there or
  the witness records misclassify as LLG.
- **The tie pickup is tens of seconds later**, on another feeder. `classify_event`
  and any time-window correlation must not expect it inside a fault window;
  grouping it needs the topology.
- **`incident_id` lives in `fleet_truth.json` only.** A relay has no idea the
  other records exist, so the pipeline has to re-derive the grouping the way it
  must on a real SUBNET pull. Putting it in the CFG would be cheating.

## Incident grouping

`incidents.py` rebuilds the sets from what a real pull actually has. Two joins,
and they are not the same rule:

- **Same fault** — inside `window_s` (config `incidents.window_s`, 2 s) **and**
  `on_same_path()`. Both are required. Time alone merges unrelated faults across
  the fleet during a storm; topology alone merges every fault that feeder ever
  had.
- **Restoration** — a `LOAD` record on the far side of a tie that backs up a
  section someone just locked out, inside `restore_window_s` (15 min). Purely
  topological: the tie close is tens of seconds later, on another feeder, under
  a different device id, so no time window finds it.

With **no topology** it degrades to same-feeder-and-window. That over-merges two
faults a second apart on one feeder, which is stated rather than hidden.

`clock_suspects()` reports pairs that share a path and a fault type but miss the
window. A relay minutes out — or a whole timezone out from a missed setting —
looks exactly like a separate fault, and silently splitting the incident is the
failure mode worth naming.

**`upstream_also_tripped` is an observation, not a verdict.** Two devices on one
path both operating is invisible in either record alone, but fuse saving and a
genuine over-trip look identical from here. The tool surfaces it; the engineer
looks at the one-line and decides. This is not a FLISR verification machine —
FLISR runs in the ADMS.

Grouping accuracy is validated against `fleet_truth.json` and printed with the
other detector-agreement numbers. It only means something on generated data, so
treat it as a regression guard.

## The LOAD class — cold load is not a three-phase fault

`classify_fault` returns **`LOAD`** for a balanced current rise on all three
phases with the voltage still up. Cold-load inrush when a tie closes onto a
restored section is indistinguishable from a 3PH fault in current alone, and it
was being reported as one; voltage is the only discriminator, so
`_voltage_held_up()` gates the 3PH branch on it (`_LOAD_STEP_V_RATIO`, 0.85).

With **no voltage channels it stays 3PH** — an unknown is not evidence of a
load step. `triage` fires no flags on a LOAD record and `wso_impact` returns
`NOT_EXPOSED`, because there is no fault for EPSS to convert. Without this,
every FLISR restoration read as a Priority 1 three-phase fault on a healthy
feeder.

This did not move any real fault: all five generators classify unchanged.

## The demo set

`demo/` is **tracked on purpose** — 201 synthetic records from 115 incidents,
the registry, the topology, the ground truth, and a pre-built
`demo_dashboard.html`. It exists because the tool had to be demonstrated before
SUBNET was returning COMTRADE files, and a colleague's managed PC may not get
the Python install working on the first try. The pre-built HTML is the
zero-dependency fallback.

The records live in **`demo/incident_events/`** — named for what it holds, since
the corpus is organised around incidents rather than loose events.
`fleet_analyze.resolve_inputs` looks for that name first and falls back to a
plain `events/` dump, which is what a real SUBNET pull is.

Regenerate it with `fleet_gen --count 115 --seed 20260601`; the seed is what
makes it reproducible. **`--count` counts incidents, not records** — each yields
roughly 1.7 files. Rebuild the HTML after any dashboard change, or it goes stale
against the code. `demo/analysis/` and `fleet/` stay ignored — those are
scratch.

Any dashboard built from a set with a `fleet_truth.json` beside it shows a
DEMO DATA badge. Ground truth only exists for generated events, so that badge
is a reliable "this is not real plant" signal.

## Feedback

Both the GUI (`✉ Feedback`) and the dashboard footer open a **pre-filled mail
draft** via `mailto:` — neither sends anything. The message goes to the user's
own mail client to read, edit and send, which matters because the address is
outside the corporate network.

Attached context is counts, versions and settings — **never device names or
event filenames**. Those are operational data and this is the one path in the
tool that leaves the network.

## Operational data

`devices.csv` is real operational data and is gitignored — only
`devices_template.csv` is tracked, plus `demo/devices.csv`, which is invented.
Never commit a populated registry, and don't put real device IDs, feeder names
or customer counts in test fixtures or commit messages.

The one path that leaves the network is the feedback draft; it carries counts
and settings, never device names or event filenames.

## Sharing conventions

This repo follows the pq-analyzer layout for distribution to colleagues:
`git clone` + `pip install -e .` so `git pull` updates the tool in place, with
`GIT_GUIDE.md` as the from-scratch walkthrough, `check_install.py` as the
diagnostic to run before asking anyone for help, and `.bat` launchers for
double-click use on Windows. Keep those three in step with any change to
installation or dependencies.

## Keeping this file current

Update it in the **same commit** as the change it describes, and *revise* the
relevant section rather than appending a new one — this file drifted into two
contradictory WSO sections that way, one of them still describing the
superseded three-class model.

`TestTheProjectNotesStayCurrent` guards the parts that can be checked: every
module appears in the map, every EPSS class is mentioned, and the superseded
wording stays gone. It cannot check whether the prose is *true*, so when you
change behaviour, re-read the section that covers it.

## Conventions

- Thresholds belong in `config.json`, not inline. `slow_trip_cycles`,
  `fault_threshold_multiplier`, `epss_tiers` are all user-tunable by design.
- Classification is explicitly "not a replacement for a relay algorithm" — keep
  that framing in docstrings and report prose. It's a review aid.
- 60 Hz is a default, not an assumption: use `record.line_freq()` and
  `record.samples_per_cycle()` rather than hardcoding 60 or a sample count.
