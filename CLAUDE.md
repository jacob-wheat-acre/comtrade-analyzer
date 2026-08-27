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
triage.py            Priority 1/2/3 flags
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
pytest test_comtrade.py -v          # 81 tests, ~0.2 s — run this first
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

## The demo set

`demo/` is **tracked on purpose** — 100 synthetic events, the registry, the
ground truth, and a pre-built `demo_dashboard.html`. It exists because the tool
had to be demonstrated before SUBNET was returning COMTRADE files, and a
colleague's managed PC may not get the Python install working on the first try.
The pre-built HTML is the zero-dependency fallback.

Regenerate it with `fleet_gen --count 100 --seed 20260601`; the seed is what
makes it reproducible. Rebuild the HTML after any dashboard change, or it goes
stale against the code. `demo/analysis/` and `fleet/` stay ignored — those are
scratch.

Any dashboard built from a set with a `fleet_truth.json` beside it shows a
DEMO DATA badge. Ground truth only exists for generated events, so that badge
is a reliable "this is not real plant" signal.

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

## WSO / EPSS

The three-way classification is the whole point of `wso_impact.py`:

- `PERMANENT` — locked out under normal settings; already sustained, EPSS
  changes nothing
- `NOT_EXPOSED` — cleared with no reclose; EPSS can't suppress what didn't happen
- `WSO_EXPOSED` — needed ≥1 automatic reclose; **becomes a sustained outage
  under EPSS**

Only the third converts. Getting this wrong overstates or understates customer
impact in a wildfire-mitigation filing, so treat the boundaries as load-bearing.

`devices.csv` is real operational data and is gitignored — only
`devices_template.csv` is tracked. Never commit a populated registry, and don't
put real device IDs, feeder names, or customer counts in test fixtures or commit
messages.

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

## The GUI is a launcher

Folder mode runs `batch.sweep` and opens the dashboard; it renders nothing per
event. The old per-event folder loop is what leaked figures until the window
stopped redrawing. Single-file mode keeps the detailed plot/report path.

The dashboard carries everything the Word report used to show — provenance,
per-channel peaks, phasor diagram, digital operations log, DC offset — via
`extract_report_detail()` in `fleet_analyze.py`. The .docx is no longer the way
anyone looks at an event; treat it as legacy output, not the deliverable.

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

## Sharing conventions

This repo follows the pq-analyzer layout for distribution to colleagues:
`git clone` + `pip install -e .` so `git pull` updates the tool in place, with
`GIT_GUIDE.md` as the from-scratch walkthrough, `check_install.py` as the
diagnostic to run before asking anyone for help, and `.bat` launchers for
double-click use on Windows. Keep those three in step with any change to
installation or dependencies.

## Conventions

- Thresholds belong in `config.json`, not inline. `slow_trip_cycles`,
  `fault_threshold_multiplier`, `epss_tiers` are all user-tunable by design.
- Classification is explicitly "not a replacement for a relay algorithm" — keep
  that framing in docstrings and report prose. It's a review aid.
- 60 Hz is a default, not an assumption: use `record.line_freq()` and
  `record.samples_per_cycle()` rather than hardcoding 60 or a sample count.
