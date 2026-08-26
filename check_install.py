#!/usr/bin/env python3
"""Verify a COMTRADE Analyzer install and say plainly what to do about anything broken.

Run this when the tool won't start, when a batch run reports every file as a
parse error, or right after a fresh setup to confirm it took:

    python check_install.py

Written to run on a machine where the libraries are missing or broken, so it
imports nothing outside the standard library at module level.
"""

import os
import platform
import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# display name → (import name, why the tool needs it)
_REQUIRED = [
    ("numpy",       "numpy",      "all numeric processing"),
    ("matplotlib",  "matplotlib", "waveform, RMS, sequence and phasor plots"),
    ("python-docx", "docx",       "Word event and WSO reports"),
]

# Not pip-installable — bundled with CPython, but commonly absent on Linux.
_OPTIONAL = [
    ("tkinter", "tkinter", "the desktop GUI (comtrade-gui); the CLI works without it"),
    ("pytest",  "pytest",  "running the test suite (developers only)"),
]

_MIN_PYTHON = (3, 10)

_COMMANDS = ["comtrade-batch", "comtrade-analyze", "comtrade-wso",
             "comtrade-dashboard", "comtrade-gui", "comtrade-demo-fleet"]


def _line(char="-"):
    print(char * 72)


def _ok(msg):
    print("  [ OK ]  " + msg)


def _bad(msg):
    print("  [FAIL]  " + msg)


def _warn(msg):
    print("  [WARN]  " + msg)


def check_python():
    """Report the interpreter actually running, and whether it's new enough.

    The interpreter path is the single most useful line in this report: a very
    common failure is pip installing into one Python while the launcher runs
    another, which looks identical to 'nothing is installed'.
    """
    print("Python interpreter")
    v = sys.version_info
    print("     version:  %d.%d.%d" % (v.major, v.minor, v.micro))
    print("  executable:  %s" % sys.executable)
    print("    platform:  %s %s" % (platform.system(), platform.machine()))

    if (v.major, v.minor) < _MIN_PYTHON:
        _bad("Python %d.%d or newer is required." % _MIN_PYTHON)
        return False
    _ok("Python version is new enough.")
    return True


def check_imports():
    """Import each dependency and report the ones that fail."""
    print("\nRequired libraries")
    failures = []
    for name, module, why in _REQUIRED:
        try:
            mod = __import__(module)
            ver = getattr(mod, "__version__", "?")
            _ok("%-12s %-10s  (%s)" % (name, ver, why))
        except Exception as exc:
            _bad("%-12s missing or broken  (%s)" % (name, why))
            print("          %s: %s" % (type(exc).__name__, exc))
            failures.append(name)

    print("\nOptional libraries")
    for name, module, why in _OPTIONAL:
        try:
            __import__(module)
            _ok("%-12s present    (%s)" % (name, why))
        except Exception:
            _warn("%-12s missing    (%s)" % (name, why))
            if module == "tkinter":
                print("          tkinter ships with CPython on Windows and macOS.")
                print("          On Linux install it with your package manager,")
                print("          e.g.  sudo apt install python3-tk")
    return failures


def check_package():
    """Import the analysis engine the same way the console commands do."""
    print("\nCOMTRADE Analyzer package")
    sys.path.insert(0, str(_HERE))
    try:
        import comtrade_analyzer
        from comtrade_analyzer.comtrade_parser import COMTRADEParser  # noqa: F401
        from comtrade_analyzer.analysis import classify_fault          # noqa: F401
    except Exception as exc:
        _bad("comtrade_analyzer failed to import — no command will run.")
        print("          %s: %s" % (type(exc).__name__, exc))
        return False

    _ok("comtrade_analyzer %s imported." % getattr(comtrade_analyzer, "__version__", "?"))

    # The dashboard template is package data; a partial copy loses it silently
    # and only fails at the moment you try to render a dashboard.
    tpl = Path(comtrade_analyzer.__file__).parent / "dashboard_template.html"
    if tpl.is_file():
        _ok("dashboard_template.html found.")
    else:
        _bad("dashboard_template.html is missing — the dashboard cannot render.")
        print("          Expected at %s" % tpl)
        return False
    return True


def check_commands():
    """Report whether the console commands are on PATH."""
    print("\nConsole commands")
    found = [c for c in _COMMANDS if shutil.which(c)]
    if len(found) == len(_COMMANDS):
        _ok("All %d commands are on PATH (%s ...)." % (len(_COMMANDS), found[0]))
        return True
    if not found:
        _warn("None of the commands are on PATH.")
        print("          The tool still works from inside this folder:")
        print("              python main.py <event.cfg>")
        print("          To get the commands, run:  python -m pip install -e .")
        return False
    _warn("Only some commands are on PATH: %s" % ", ".join(found))
    print("          Reinstall with:  python -m pip install -e .")
    return False


def check_location():
    """Warn about cloud-synced folders, which can dehydrate files on read."""
    print("\nInstall location")
    print("  %s" % _HERE)

    lowered = str(_HERE).lower()
    if "onedrive" in lowered or "dropbox" in lowered or "box sync" in lowered:
        _warn("This folder is inside a cloud-synced directory.")
        print("          Sync can replace files with cloud-only placeholders and")
        print("          can conflict with git.  Consider moving the folder to")
        print("          %s." % (Path(os.path.expanduser("~")) / "Documents"))
        return False

    _ok("Not inside a cloud-synced folder.")
    return True


def check_registry():
    """devices.csv is operational data — present locally, never committed."""
    print("\nDevice registry")
    devices = _HERE / "devices.csv"
    template = _HERE / "comtrade_analyzer" / "devices_template.csv"

    if devices.is_file():
        try:
            rows = max(0, len(devices.read_text(encoding="utf-8-sig").strip().splitlines()) - 1)
            _ok("devices.csv found (%d device row(s))." % rows)
        except Exception as exc:
            _warn("devices.csv found but could not be read: %s" % exc)
        return True

    _warn("No devices.csv in this folder.")
    print("          Without it, events group under UNREGISTERED and")
    print("          customer-hour estimates are zero.")
    if template.is_file():
        print("          Copy the template and fill it in:")
        print("              copy comtrade_analyzer\\devices_template.csv devices.csv   (Windows)")
        print("              cp   comtrade_analyzer/devices_template.csv devices.csv   (Mac/Linux)")
    print("          devices.csv is gitignored on purpose — it holds real device")
    print("          IDs and customer counts.  Never commit it.")
    return False


def check_git():
    """Report whether this copy can receive updates via git pull."""
    print("\nGit")
    if (_HERE / ".git").is_dir():
        _ok("This copy came from git clone — `git pull` will work.")
        return True

    _warn("This folder is not a git repository.")
    print("          `git pull` will fail with 'fatal: not a git repository'.")
    print("          See GIT_GUIDE.md section 4 to convert it.")
    return False


def main():
    _line("=")
    print("COMTRADE Analyzer — install check")
    _line("=")

    python_ok = check_python()
    failures = check_imports()
    package_ok = check_package() if not failures else False
    commands_ok = check_commands()
    location_ok = check_location()
    registry_ok = check_registry()
    git_ok = check_git()

    print()
    _line("=")

    if python_ok and not failures and package_ok:
        print("RESULT: install looks good.")
        if not (commands_ok and location_ok and registry_ok and git_ok):
            print("Warnings above are worth fixing but won't stop the tool running.")
        _line("=")
        return 0

    print("RESULT: this install is broken.")
    print()

    if not python_ok:
        print("Fix Python first — everything else depends on it.")
        print("On a managed PC, request Python through your IT software portal.")
        print("See GIT_GUIDE.md section 1.")
        _line("=")
        return 1

    print("Reinstall the libraries.  From inside this folder, run:")
    print()
    # One line, no continuation character — this gets pasted into Windows cmd,
    # where a trailing backslash is not a line continuation.
    print("    python -m pip install --force-reinstall --no-cache-dir -e .")
    print()
    print("Use `python -m pip`, not plain `pip` — it guarantees the packages")
    print("go to the interpreter listed above rather than a different Python.")
    print()
    print("If that fails with an SSL, certificate, or proxy error, your network")
    print("is blocking the package server.  Open a ticket asking for access to")
    print("pypi.org and files.pythonhosted.org.  Do not apply workarounds that")
    print("disable certificate checking.")
    print()
    print("If it still fails, send this entire output to the maintainer.")
    _line("=")
    return 1


if __name__ == "__main__":
    sys.exit(main())
