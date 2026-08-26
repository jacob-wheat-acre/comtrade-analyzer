#!/usr/bin/env python3
"""Compatibility shim — the CLI now lives in comtrade_analyzer.main.

Kept so `python main.py ...` still works from a checkout. Installed users get
the `comtrade-analyze` command instead.
"""
from comtrade_analyzer.main import main

if __name__ == "__main__":
    main()
