#!/usr/bin/env python3
"""
install_shortcut.py — create a desktop launcher for COMTRADE Analyzer.
  Mac:     builds a .app bundle on the Desktop
  Windows: creates a .lnk shortcut on the Desktop

Run once after setting up the tool folder:
  python3 install_shortcut.py
"""

import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).parent.resolve()


def _find_desktop() -> Path:
    """
    Return the real Desktop directory path.

    On Windows this asks the Shell API directly — handles OneDrive Desktop
    sync, Group Policy folder redirection, and custom registry paths that
    break the naive Path.home() / 'Desktop' assumption.
    """
    if platform.system() == "Windows":
        try:
            import ctypes
            import ctypes.wintypes
            buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
            # CSIDL_DESKTOPDIRECTORY (0x10) = the real on-disk Desktop folder.
            # NOT CSIDL_DESKTOP (0x00), which is a virtual namespace root.
            # SHGetFolderPathW respects folder redirection and OneDrive moves.
            ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)
            p = Path(buf.value)
            if p.is_dir():
                return p
        except Exception:
            pass
        # Fallback: check common OneDrive path before giving up
        home = Path.home()
        for candidate in [
            home / "OneDrive" / "Desktop",
            home / "OneDrive - Xcel Energy" / "Desktop",
            home / "Desktop",
        ]:
            if candidate.is_dir():
                return candidate

    return Path.home() / "Desktop"


# ── Mac ───────────────────────────────────────────────────────────────────────

def install_mac():
    desktop = _find_desktop()
    app     = desktop / "COMTRADE Analyzer.app"

    if app.exists():
        shutil.rmtree(app)

    # Clear stale pyc cache so the .app always loads current source
    pycache = TOOL_DIR / "__pycache__"
    if pycache.exists():
        shutil.rmtree(pycache)

    contents  = app / "Contents"
    macos_dir = contents / "MacOS"
    res_dir   = contents / "Resources"
    for d in (macos_dir, res_dir):
        d.mkdir(parents=True)

    r   = subprocess.run(["which", "python3"], capture_output=True, text=True)
    py3 = r.stdout.strip() or sys.executable

    # Force the native slice on Apple Silicon so tkinter doesn't get launched
    # under Rosetta.  On an Intel Mac `arch -arm64` fails outright, so only add
    # it where it applies.
    arch_prefix = "arch -arm64 " if platform.machine() == "arm64" else ""

    launcher = macos_dir / "COMTRADE Analyzer"
    launcher.write_text(
        f'#!/bin/bash\n'
        f'cd "{TOOL_DIR}"\n'
        f'rm -rf "{TOOL_DIR}/__pycache__"\n'
        f'exec {arch_prefix}"{py3}" "{TOOL_DIR / "app.py"}"\n'
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    icns_src = TOOL_DIR / "icon.icns"
    if icns_src.exists():
        shutil.copy(icns_src, res_dir / "icon.icns")

    (contents / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
        ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        '    <key>CFBundleExecutable</key>\n'
        '    <string>COMTRADE Analyzer</string>\n'
        '    <key>CFBundleIconFile</key>\n'
        '    <string>icon</string>\n'
        '    <key>CFBundleName</key>\n'
        '    <string>COMTRADE Analyzer</string>\n'
        '    <key>CFBundleDisplayName</key>\n'
        '    <string>COMTRADE Analyzer</string>\n'
        '    <key>CFBundleIdentifier</key>\n'
        '    <string>com.protection.comtrade-analyzer</string>\n'
        '    <key>CFBundlePackageType</key>\n'
        '    <string>APPL</string>\n'
        '    <key>CFBundleVersion</key>\n'
        '    <string>1.0</string>\n'
        '    <key>CFBundleShortVersionString</key>\n'
        '    <string>1.0</string>\n'
        '    <key>LSUIElement</key>\n'
        '    <false/>\n'
        '</dict>\n'
        '</plist>\n'
    )

    os.system(f'touch "{app}"')

    print(f"\n  COMTRADE Analyzer.app created on your Desktop ({desktop})")
    print(f"  Double-click it any time to launch the tool.\n")
    print(f"  Note: on first launch macOS may show a security warning.")
    print(f"  If so: System Settings → Privacy & Security → Open Anyway\n")


# ── Windows ───────────────────────────────────────────────────────────────────

def install_windows():
    desktop  = _find_desktop()
    shortcut = desktop / "COMTRADE Analyzer.lnk"
    bat      = TOOL_DIR / "COMTRADE Analyzer.bat"
    ico      = TOOL_DIR / "icon.ico"

    # The .bat is a tracked file in the repo, not something generated here.
    # This function used to write its own three-line version over the top,
    # which meant the launcher you got depended on whether you had run the
    # installer -- the repo copy falls back to pythonw and explains itself when
    # Python is missing; the generated one did neither. One implementation.
    if not bat.exists():
        print(f"\n  {bat.name} is missing from {TOOL_DIR}.")
        print("  Re-clone or `git checkout` it; the shortcut points at it.\n")
        return

    if not ico.exists():
        print(f"\n  {ico.name} is missing, so the shortcut would get the "
              "default icon.\n  Run:  python3 make_icon.py\n")
        return

    # Replace rather than update. A .lnk that already carries a bad icon path
    # keeps its cached bitmap through a rewrite of the same file, and the
    # symptom that follows -- "I re-ran the installer and nothing changed" --
    # is indistinguishable from the installer not having worked.
    if shortcut.exists():
        try:
            shortcut.unlink()
        except OSError as exc:
            print(f"\n  Could not replace the old shortcut: {exc}\n")

    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{shortcut}'); "
        f"$sc.TargetPath = '{bat}'; "
        f"$sc.WorkingDirectory = '{TOOL_DIR}'; "
        # ",0" names the icon's index within the file. Without an IconLocation
        # at all, Windows draws the target's icon -- and the target is a .bat,
        # whose icon is the generic gears.
        f"$sc.IconLocation = '{ico},0'; "
        "$sc.Description = 'COMTRADE Relay Event Analyzer'; "
        "$sc.Save(); "
        # Read it back. Save() reports nothing when the shell quietly declines
        # a value, and an unset icon is exactly the failure this is fixing.
        f"$c = $ws.CreateShortcut('{shortcut}'); "
        "Write-Output ('ICON=' + $c.IconLocation)"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, text=True,
    )

    if not shortcut.exists():
        print(f"\n  Could not create shortcut: {result.stderr.strip()}")
        print(f"  You can still launch by double-clicking '{bat.name}'\n")
        return

    print(f"\n  Shortcut created: {shortcut}")
    readback = next((l.split("=", 1)[1].strip()
                     for l in result.stdout.splitlines() if l.startswith("ICON=")),
                    "")
    if readback and readback.lower().startswith(str(ico).lower()):
        print(f"  Icon: {readback}")
    else:
        print(f"  Warning: the shortcut's icon did not stick "
              f"(reads back as {readback or 'empty'}).")
        print("  Right-click the shortcut -> Properties -> Change Icon, and "
              f"point it at:\n    {ico}")

    # Tell the shell its icon cache is stale. Without this the desktop keeps
    # drawing the previous icon until something else happens to invalidate it.
    try:
        import ctypes
        SHCNE_ASSOCCHANGED, SHCNF_IDLIST = 0x08000000, 0x0000
        ctypes.windll.shell32.SHChangeNotify(
            SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
    except Exception:
        pass
    print("\n  If the desktop still shows the old icon, the shell is serving a "
          "cached copy:\n      ie4uinit.exe -show\n  or sign out and back in.\n")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    system = platform.system()
    print(f"Desktop detected: {_find_desktop()}")
    if system == "Darwin":
        install_mac()
    elif system == "Windows":
        install_windows()
    else:
        print("Unsupported platform — manually create a shortcut to app.py")
        sys.exit(1)
