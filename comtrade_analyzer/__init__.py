"""
comtrade_analyzer — IEEE C37.111 relay event analysis for distribution feeders.

Parses COMTRADE oscillography, classifies the fault, screens for high-impedance
faults and reclose sequences, assigns a review priority, and quantifies WSO/EPSS
reliability impact across a folder of events.

Console entry points (see pyproject.toml):
    comtrade-analyze   single event or folder → plots, Word report, CSV/JSON
    comtrade-batch     bulk folder analysis → dashboard + CSV (on-demand or watched)
    comtrade-wso       WSO/EPSS reliability impact report
    comtrade-gui       tkinter desktop interface
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
