#!/usr/bin/env python3
"""Compatibility shim — the GUI now lives in comtrade_analyzer.app.

Kept so `python app.py` and the desktop shortcut still work from a checkout.
Installed users get the `comtrade-gui` command instead.
"""
from comtrade_analyzer.app import main

if __name__ == "__main__":
    main()
