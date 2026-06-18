"""
data_model.py — Core data structures for the COMTRADE relay event analyzer.

EventRecord is the central object that all parsers produce and all analysis
functions consume.  It holds everything a protection engineer needs: the time
vector, scaled engineering-unit channel arrays, and enough metadata to
reconstruct provenance.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional
import numpy as np


@dataclass
class ChannelInfo:
    """Metadata for a single analog channel."""
    name: str
    units: str
    multiplier: float   # 'a' factor: engineering_value = a * raw + b
    offset: float       # 'b' adder
    phase: str = ""     # A, B, C, N, etc.
    circuit_id: str = ""


@dataclass
class EventRecord:
    """
    Structured representation of one COMTRADE event file.

    time                 — seconds from recording start (monotonically increasing)
    analog_channels      — {channel_name: np.ndarray} in engineering units (A, V, etc.)
    digital_channels     — {channel_name: np.ndarray of int8 (0 or 1)}
    analog_info          — {channel_name: ChannelInfo} with scaling metadata
    sample_rate          — primary sample rate in Hz (0 if variable/unknown)
    trigger_time         — seconds from recording start at which the trigger fired
    trigger_index        — sample index closest to trigger_time
    metadata             — dict with station, device, revision, timestamps, etc.
    """
    time: np.ndarray
    analog_channels: Dict[str, np.ndarray]
    digital_channels: Dict[str, np.ndarray]
    analog_info: Dict[str, ChannelInfo]
    sample_rate: float
    trigger_time: float
    trigger_index: int
    metadata: Dict

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def duration_s(self) -> float:
        """Total recording duration in seconds."""
        if len(self.time) < 2:
            return 0.0
        return float(self.time[-1] - self.time[0])

    def channel_names(self) -> Dict[str, list]:
        """Return {'analog': [...], 'digital': [...]} channel name lists."""
        return {
            "analog": list(self.analog_channels.keys()),
            "digital": list(self.digital_channels.keys()),
        }

    def get_channel(self, name: str) -> Optional[np.ndarray]:
        """
        Look up a channel by name, checking analog first then digital.
        Case-insensitive match attempted if exact name fails.
        """
        if name in self.analog_channels:
            return self.analog_channels[name]
        if name in self.digital_channels:
            return self.digital_channels[name]
        # case-insensitive fallback
        name_upper = name.upper()
        for k, v in self.analog_channels.items():
            if k.upper() == name_upper:
                return v
        for k, v in self.digital_channels.items():
            if k.upper() == name_upper:
                return v
        return None

    def line_freq(self) -> float:
        """Power system frequency from metadata, defaulting to 60 Hz."""
        return float(self.metadata.get("line_freq", 60.0))

    def samples_per_cycle(self) -> int:
        """Number of samples in one power-frequency cycle."""
        lf = self.line_freq()
        if self.sample_rate > 0 and lf > 0:
            return max(1, int(round(self.sample_rate / lf)))
        return 1
