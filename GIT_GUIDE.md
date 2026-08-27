# COMTRADE Analyzer — Setup and Update Guide

A from-scratch walkthrough for getting COMTRADE Analyzer onto a new PC and
keeping it current. No prior experience with git or the command line is
assumed.

Written for Windows, since that's what the team runs. Mac notes are at the end.

**Time required:** about 30 minutes of your attention, plus however long the
Python software request takes to be approved.

---

## Contents

- [0. Why git, and what it actually is](#0-why-git-and-what-it-actually-is)
- [1. Install Python](#1-install-python)
- [2. Install Git](#2-install-git)
- [3. Set up your GitHub sign-in](#3-set-up-your-github-sign-in)
- [4. Get the code (`git clone`)](#4-get-the-code-git-clone)
- [5. Install the tool](#5-install-the-tool)
- [6. Check it took](#6-check-it-took)
- [7. Your device registry](#7-your-device-registry)
- [8. Getting updates (`git pull`)](#8-getting-updates-git-pull)
- [9. When `git pull` complains](#9-when-git-pull-complains)
- [10. For the maintainer: pushing changes](#10-for-the-maintainer-pushing-changes)
- [11. Glossary](#11-glossary)
- [12. Cheat sheet](#12-cheat-sheet)
- [13. Mac](#13-mac)

---

## 0. Why git, and what it actually is

COMTRADE Analyzer is a set of Python files that changes regularly — new relay
export quirks, corrected thresholds, report fixes. If your copy came from a
network share or a ZIP someone sent you, there is no way to get just the fixes.
You get a whole new folder every time, and no way to tell what changed.

Git solves that. It's a program that tracks a folder's history. The
authoritative copy lives on GitHub (a website that hosts git folders); your PC
keeps its own full copy. One command, `git pull`, compares the two and
downloads only what actually changed — usually a few kilobytes, in a second.

Three terms that show up constantly, in plain language:

| Term | Means |
|---|---|
| **repository** (or **repo**) | The tracked folder, plus its whole history. `comtrade-analyzer` is a repo. |
| **origin** | The nickname your PC uses for the GitHub copy. When you type `git pull`, it pulls from origin. |
| **commit** | One saved snapshot of the folder, with a message describing what changed. History is a list of commits. |

You do **not** need to understand branches, merges, or staging to use the tool.
Sections 1–9 are all a normal user ever needs.

---

## 1. Install Python

Python is the language COMTRADE Analyzer is written in. Without it, nothing
runs. **Version 3.10 or newer is required.**

On a managed PC you usually cannot install it yourself — request it through
your IT software portal. Ask for "Python 3.11 or newer, 64-bit".

To check whether it is already there, open Command Prompt (press Start, type
`cmd`, press Enter) and run:

```
python --version
```

If that prints `Python 3.11.x` or higher, you are set. If it says the command
is not recognized, try:

```
py --version
```

If `py` works but `python` doesn't, Python is installed but not on the system
PATH — ask IT to add it, and use `py` in place of `python` everywhere below
until they do.

---

## 2. Install Git

Git is the program that downloads and updates the code. Request "Git for
Windows" through the same software portal.

Check it:

```
git --version
```

Anything from 2.30 up is fine. Accept every default in the installer if you
run it yourself.

---

## 3. Set up your GitHub sign-in

If the repository is private, git needs to know who you are.

1. Ask the maintainer to add your GitHub account as a collaborator.
2. The first time you run `git clone`, a browser window opens asking you to
   sign in to GitHub. Sign in and approve.

Windows stores the sign-in afterwards, so this happens once.

If no browser window appears and it asks for a password in the terminal
instead, stop — GitHub no longer accepts account passwords there. Tell the
maintainer; you need Git Credential Manager, which ships with Git for Windows.

---

## 4. Get the code (`git clone`)

Pick a folder that is **not** inside OneDrive, Dropbox, or Box. Cloud sync
replaces files with placeholders and fights with git. `C:\Users\<you>\Documents`
is a good choice.

```
cd %USERPROFILE%\Documents
git clone https://github.com/jacob-wheat-acre/comtrade-analyzer.git
cd comtrade-analyzer
```

You now have a folder called `comtrade-analyzer` with the code in it.

### If you already have a copy that didn't come from `git clone`

A folder someone emailed you is not a repo, and `git pull` will fail on it with
`fatal: not a git repository`. The fix is to clone a fresh copy alongside it as
above, move your `devices.csv` across, and delete the old folder.

---

## 5. Install the tool

From inside the `comtrade-analyzer` folder:

```
python -m pip install -e .
```

Use `python -m pip`, never plain `pip`. On a PC with more than one Python,
plain `pip` frequently installs into the wrong one, which looks exactly like
"nothing installed".

The `-e` matters: it installs the tool **in place**, pointing at this folder.
That means a `git pull` updates the tool immediately, with no reinstall. Leave
it off and you would have to reinstall after every update.

This gives you six commands:

| Command | What it does |
|---|---|
| `comtrade-batch` | Bulk-analyze a folder of events → dashboard, CSV, EPSS numbers |
| `comtrade-analyze` | One event → plots and a Word report |
| `comtrade-wso` | WSO/EPSS reliability impact report |
| `comtrade-dashboard` | Re-render a dashboard from an existing analysis |
| `comtrade-gui` | The desktop interface |
| `comtrade-demo-fleet` | Generate fake events to try the tool safely |

If the install fails with an SSL, certificate, or proxy error, your network is
blocking the package server. Open a ticket asking for access to `pypi.org` and
`files.pythonhosted.org`. **Do not** apply workarounds that disable certificate
checking.

### Desktop shortcut

```
install_shortcut.bat
```

Double-clicking that once puts a "COMTRADE Analyzer" icon on your Desktop that
opens the GUI. You can also just double-click `COMTRADE Analyzer.bat` in the
folder.

---

## 6. Check it took

```
python check_install.py
```

This prints a line-by-line report: which Python is actually running, which
libraries loaded, whether the commands are on PATH, whether your folder is
cloud-synced, and whether `git pull` will work. It ends with either
`RESULT: install looks good.` or specific instructions.

Run it any time something misbehaves, and paste its whole output when you
report a problem — it answers most of the questions the maintainer would ask.

Then try the tool on the demo data that ships with it, so you are not
debugging against a real event:

```
comtrade-batch demo\events --devices demo\devices.csv
```

It analyses 100 synthetic events in about a second and opens the dashboard.

**If that fails, you can still demo.** `demo\demo_dashboard.html` is a
complete pre-built dashboard — double-click it. It needs no Python at all, so
it works even while you are still sorting the install out.

---

## 7. Your device registry

`devices.csv` maps relay device IDs to zone, fire risk tier and customers
served. Without it, every event groups under UNREGISTERED and the
customer-hour estimates come out zero.

Create yours from the template:

```
copy comtrade_analyzer\devices_template.csv devices.csv
```

Then fill it in. `device_id` must match the `rec_dev_id` field in the COMTRADE
CFG file — that is line 1, second field.

**`devices.csv` is gitignored on purpose.** It holds real device IDs, feeder
names and customer counts. It must never be committed, and neither should real
device IDs or feeder names appear in commit messages. Only
`devices_template.csv` is tracked.

The same care applies to what the tool produces. Every output is a local file
and nothing is uploaded anywhere — but a dashboard built from real events
contains the whole registry, so treat those HTML files as operational data too.

---

## 8. Getting updates (`git pull`)

From inside the folder:

```
cd %USERPROFILE%\Documents\comtrade-analyzer
git pull
```

That's the whole update. Because the tool was installed with `-e`, the new code
is live immediately — no reinstall.

Two exceptions where you should also re-run the install:

- The maintainer says a new library was added.
- `check_install.py` starts reporting a missing library.

```
python -m pip install -e .
```

---

## 9. When `git pull` complains

### "Your local changes to the following files would be overwritten by merge"

You edited a tracked file. Almost always this is accidental. To throw your
change away and take the maintainer's version:

```
git checkout -- <the file it named>
git pull
```

To keep your change instead, copy the file somewhere outside the folder first,
then run the two commands above.

If it names `devices.csv`, something is wrong — that file should be ignored.
Tell the maintainer.

### "fatal: not a git repository"

Your folder didn't come from `git clone`. See section 4.

### "Permission denied" or a sign-in loop

Your GitHub access lapsed or was never granted. Ask the maintainer to confirm
you are still a collaborator, then run `git pull` again and complete the
browser sign-in.

### Anything else

Run `python check_install.py` and send its output, along with the exact text of
the git error, to the maintainer. Do not run commands you found online that
include `--force`, `reset --hard`, or `clean -fd`; they delete work silently.

---

## 10. For the maintainer: pushing changes

```
git status                          # see what changed
git add -A
git commit -m "Short description of what changed and why"
git push
```

Before pushing anything that touches the analysis math, run the test suite:

```
pytest test_comtrade.py -v
```

It must be green. Two tests are `xfail` on purpose and record known defects —
if either flips to `XPASS`, the defect was fixed and the marker should come off
in the same commit.

Regenerate the fixtures and confirm the classifications still hold:

```
python generate_test_ll.py
python generate_test_llg.py
python generate_test_3ph.py
python generate_test_recloser.py
python generate_test_data.py
pytest test_comtrade.py -k fixture -v
```

Never commit `devices.csv`, anything under `fleet/`, or real device IDs and
feeder names in a commit message.

### When collaborators join

Add them on GitHub under Settings → Collaborators, then send them this guide.

---

## 11. Glossary

| Term | Means |
|---|---|
| **COMTRADE** | IEEE C37.111, the standard file format relays export event records in. A `.cfg` header plus a `.dat` of samples. |
| **CFG / DAT** | The two halves of one event. Both are needed; the tool is pointed at the `.cfg`. |
| **EPSS** | Enhanced Powerline Safety Settings — reclosing disabled on high fire-risk days. |
| **WSO-exposed** | An event that needed an automatic reclose to clear, so it becomes a sustained outage under EPSS. |
| **repo / origin / commit** | See section 0. |
| **PATH** | The list of folders Windows searches for a command. If a command "is not recognized", it isn't on PATH. |
| **editable install** | `pip install -e .` — the tool runs from this folder, so `git pull` updates it. |

---

## 12. Cheat sheet

```
git pull                                  update to the latest code
python check_install.py                   diagnose a broken install
pytest test_comtrade.py -v                run the test suite

comtrade-batch <folder> --devices devices.csv          analyze a folder
comtrade-batch <folder> --watch --interval 300         keep watching it
comtrade-batch <folder> --rebuild                      re-analyze everything
comtrade-analyze <event.cfg> --report --save-plots     one event, full report
comtrade-gui                                           desktop interface
```

Double-clickable equivalents in the folder: `COMTRADE Analyzer.bat` opens the
GUI, and dragging an events folder onto `Analyze Folder.bat` runs a batch.

---

## 13. Mac

Same steps, different commands.

```
# Python 3.10+ and git, via Homebrew
brew install python git

cd ~/Documents
git clone https://github.com/jacob-wheat-acre/comtrade-analyzer.git
cd comtrade-analyzer
python3 -m pip install -e .
python3 check_install.py
```

Use `python3` rather than `python` throughout.

**The `.bat` files do not work on a Mac.** They are Windows batch scripts;
double-clicking one on macOS opens it in a text editor or does nothing. The Mac
equivalents are:

| Windows | macOS |
|---|---|
| `install_shortcut.bat` | `python3 install_shortcut.py` |
| `COMTRADE Analyzer.bat` | the Desktop app that command creates |
| `Analyze Folder.bat` | `Analyze Folder.command` (double-click it) |

`python3 install_shortcut.py` builds a real **COMTRADE Analyzer.app** on your
Desktop, icon and all. The first time you open it, macOS may say it is from an
unidentified developer — go to System Settings → Privacy & Security and click
**Open Anyway**. That is a one-time approval.

`Analyze Folder.command` prompts for a folder. You can drag the folder from
Finder straight into the Terminal window to paste its path, then press Return.
It runs the batch and opens the dashboard for you when it finishes.

If macOS refuses to run the `.command` file, it lost its executable bit in
transit. Fix it once from Terminal:

```
chmod +x "Analyze Folder.command"
```

If matplotlib complains about a missing GUI backend when saving plots, that is
harmless — plots are written to PNG either way. Pass `--save-plots` rather than
letting it open a window.
