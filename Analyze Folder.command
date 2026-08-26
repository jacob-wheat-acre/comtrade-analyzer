#!/bin/bash
# Bulk-analyze a folder of COMTRADE events.  (macOS — double-click to run.)
#
# Tip: when it asks for the folder, you can drag the folder from Finder
# straight into the Terminal window to paste its path, then press Return.

cd "$(dirname "$0")" || exit 1
TOOL_DIR="$(pwd)"

echo "COMTRADE Analyzer — bulk folder analysis"
echo

TARGET="$1"
if [ -z "$TARGET" ]; then
    printf 'Drag the folder of COMTRADE events here, then press Return:\n> '
    read -r TARGET
fi

# A dragged path arrives with escaped spaces and sometimes surrounding quotes
TARGET="${TARGET%\"}"; TARGET="${TARGET#\"}"
TARGET="${TARGET%\'}"; TARGET="${TARGET#\'}"
TARGET="$(printf '%s' "$TARGET" | sed 's/\\ / /g')"

if [ ! -d "$TARGET" ]; then
    echo
    echo "Not a folder: $TARGET"
    echo "Press Return to close."
    read -r _
    exit 1
fi

REGISTRY=()
if [ -f "$TOOL_DIR/devices.csv" ]; then
    REGISTRY=(--devices "$TOOL_DIR/devices.csv")
    echo "Using device registry: $TOOL_DIR/devices.csv"
else
    echo "No devices.csv found — events will group under UNREGISTERED."
fi
echo

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 was not found.  See GIT_GUIDE.md section 13 (Mac)."
    echo "Press Return to close."
    read -r _
    exit 1
fi

python3 -m comtrade_analyzer.batch "$TARGET" "${REGISTRY[@]}"
STATUS=$?

DASHBOARD="$TARGET/analysis/fleet_dashboard.html"
if [ $STATUS -eq 0 ] && [ -f "$DASHBOARD" ]; then
    echo
    echo "Opening the dashboard..."
    open "$DASHBOARD"
fi

echo
echo "Press Return to close."
read -r _
