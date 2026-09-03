"""
comtrade_parser.py — IEEE C37.111 COMTRADE file parser.

Supports:
  - Revisions 1991, 1999, and 2013
  - ASCII and BINARY data formats
  - .cfg + .dat file pairs
  - .cff combined file format (2013)

Parsing assumptions
-------------------
* Timestamps in the DAT file are unsigned 32-bit integers in microseconds
  (multiplied by 'timemult' when provided).  Some relay vendors write the
  timestamp in different units; if values look wrong, check your vendor docs.
* Binary format: analog samples are signed 16-bit int; digital channels are
  packed into 16-bit unsigned words (16 channels per word, LSB = channel 1).
* Engineering value = a * raw_integer + b  (from CFG channel descriptor).
* The trigger index is located by finding the first sample whose cumulative
  elapsed time equals or exceeds (trigger_datetime - start_datetime).
* Skew per channel is parsed but not applied (sub-microsecond effect; most
  post-fault analysis does not require it).
"""

import os
import re
import struct
from datetime import datetime
from typing import Optional, List

import numpy as np

from .data_model import EventRecord, ChannelInfo


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class COMTRADEParser:
    """Parse IEEE C37.111 COMTRADE files into an EventRecord."""

    def parse(self, filepath: str) -> EventRecord:
        """
        Parse a COMTRADE dataset.

        filepath may point to:
        - A .cfg file (matching .dat/.DAT looked up automatically)
        - A .dat file (matching .cfg/.CFG looked up automatically)
        - A .cff combined file
        """
        ext = os.path.splitext(filepath)[1].lower()

        if ext == ".cff":
            return self._parse_cff(filepath)

        # .cfg or .dat — find both halves
        base = os.path.splitext(filepath)[0]
        cfg_path = _find_case(base, ".cfg")
        dat_path = _find_case(base, ".dat")

        if cfg_path is None:
            raise FileNotFoundError(f"Cannot find .cfg file for: {filepath}")
        if dat_path is None:
            raise FileNotFoundError(f"Cannot find .dat file for: {filepath}")

        return self._parse_cfg_dat(cfg_path, dat_path)

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    def _parse_cff(self, filepath: str) -> EventRecord:
        """Parse a combined .cff file (2013 revision)."""
        with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()

        sections: dict = {}
        current_type: Optional[str] = None
        buf: List[str] = []

        for line in raw.splitlines():
            # Section header marker: --- file type: XXX ---
            m = re.match(r"---+\s*file\s+type\s*:\s*(\w+)\s*---+", line, re.IGNORECASE)
            if m:
                if current_type is not None:
                    sections[current_type] = "\n".join(buf)
                current_type = m.group(1).upper()
                buf = []
            elif current_type is not None:
                buf.append(line)

        if current_type is not None:
            sections[current_type] = "\n".join(buf)

        cfg_text = sections.get("CFG", "")
        dat_text = sections.get("DAT", "")

        if not cfg_text:
            raise ValueError("No CFG section found in .cff file")

        cfg = _parse_cfg_lines(cfg_text.splitlines())

        if cfg["file_type"] == "BINARY":
            # Encode DAT text back to bytes for binary section
            dat_bytes = dat_text.encode("latin-1")
            return _parse_dat_binary_bytes(dat_bytes, cfg)
        else:
            return _parse_dat_ascii_lines(dat_text.splitlines(), cfg)

    def _parse_cfg_dat(self, cfg_path: str, dat_path: str) -> EventRecord:
        """Parse separate .cfg and .dat files."""
        with open(cfg_path, "r", encoding="utf-8", errors="replace") as fh:
            cfg_lines = fh.readlines()

        cfg = _parse_cfg_lines(cfg_lines)

        if cfg["file_type"] == "BINARY":
            return _parse_dat_binary_file(dat_path, cfg)
        else:
            with open(dat_path, "r", encoding="utf-8", errors="replace") as fh:
                dat_lines = fh.readlines()
            return _parse_dat_ascii_lines(dat_lines, cfg)


# ---------------------------------------------------------------------------
# CFG parsing
# ---------------------------------------------------------------------------

def _parse_cfg_lines(lines: list) -> dict:
    """
    Parse CFG text into a configuration dict.

    Handles 1991 (10-field analog) and 1999/2013 (13-field analog) formats.
    Strips trailing whitespace and ignores blank lines.
    """
    # Normalize: strip whitespace, drop blank lines
    lines = [ln.rstrip("\r\n").strip() for ln in lines]
    lines = [ln for ln in lines if ln]

    idx = 0

    # ---- Line 1: station_name, rec_dev_id [, rev_year] ----
    parts = _split_cfg_line(lines[idx])
    station_name = parts[0].strip() if len(parts) > 0 else ""
    rec_dev_id   = parts[1].strip() if len(parts) > 1 else ""
    rev_year     = parts[2].strip() if len(parts) > 2 else "1991"
    idx += 1

    # ---- Line 2: TT, ##A, ##D ----
    parts = _split_cfg_line(lines[idx])
    # strip trailing 'A'/'D' letters from channel counts
    n_analog  = int(re.sub(r"[AaDd]", "", parts[1].strip()))
    n_digital = int(re.sub(r"[AaDd]", "", parts[2].strip()))
    idx += 1

    # ---- Analog channel records ----
    analog_channels = []
    for _ in range(n_analog):
        p = _split_cfg_line(lines[idx])
        ch = {
            "index":      int(p[0].strip().lstrip("Aa")),
            "name":       p[1].strip(),
            "phase":      p[2].strip(),
            "circuit_id": p[3].strip(),
            "units":      p[4].strip(),
            "multiplier": float(p[5]),
            "offset":     float(p[6]),
            "skew":       float(p[7]) if p[7].strip() else 0.0,
            "min":        float(p[8]),
            "max":        float(p[9]),
        }
        # 1999/2013 adds primary, secondary, PS
        if len(p) >= 13:
            ch["primary"]   = float(p[10]) if p[10].strip() else 1.0
            ch["secondary"] = float(p[11]) if p[11].strip() else 1.0
            ch["ps_flag"]   = p[12].strip()
        else:
            ch["primary"]   = 1.0
            ch["secondary"] = 1.0
            ch["ps_flag"]   = "P"
        idx += 1
        analog_channels.append(ch)

    # ---- Digital channel records ----
    digital_channels = []
    for _ in range(n_digital):
        p = _split_cfg_line(lines[idx])
        ch = {
            "index":        int(p[0].strip().lstrip("Dd")),
            "name":         p[1].strip(),
            "phase":        p[2].strip() if len(p) > 2 else "",
            "circuit_id":   p[3].strip() if len(p) > 3 else "",
            "normal_state": int(p[4]) if len(p) > 4 and p[4].strip() else 0,
        }
        idx += 1
        digital_channels.append(ch)

    # ---- Line frequency ----
    lf = float(lines[idx].strip())
    idx += 1

    # ---- Number of sample rate sections ----
    n_rates = int(lines[idx].strip())
    idx += 1

    sample_rates = []
    total_samples = 0
    for _ in range(n_rates):
        p = _split_cfg_line(lines[idx])
        rate     = float(p[0].strip())
        endsamp  = int(p[1].strip())
        sample_rates.append({"rate": rate, "end_sample": endsamp})
        total_samples = endsamp
        idx += 1

    # ---- Start time ----
    start_time = _parse_comtrade_dt(lines[idx].strip())
    idx += 1

    # ---- Trigger time ----
    trigger_time = _parse_comtrade_dt(lines[idx].strip())
    idx += 1

    # ---- File type ----
    file_type = lines[idx].strip().upper()
    idx += 1

    # ---- Time multiplier (1999+, optional) ----
    timemult = 1.0
    if idx < len(lines):
        try:
            candidate = float(lines[idx].strip())
            # Sanity check: timemult is typically 1.0 or a small power of 10
            if 1e-6 <= candidate <= 1e6:
                timemult = candidate
                idx += 1
        except (ValueError, IndexError):
            pass

    return {
        "station_name":    station_name,
        "rec_dev_id":      rec_dev_id,
        "rev_year":        rev_year,
        "n_analog":        n_analog,
        "n_digital":       n_digital,
        "analog_channels": analog_channels,
        "digital_channels": digital_channels,
        "line_freq":       lf,
        "sample_rates":    sample_rates,
        "total_samples":   total_samples,
        "start_time":      start_time,
        "trigger_time":    trigger_time,
        "file_type":       file_type,
        "timemult":        timemult,
    }


def _split_cfg_line(line: str) -> list:
    """Split a CFG line on commas, returning a list of strings."""
    return line.split(",")


def _parse_comtrade_dt(s: str) -> datetime:
    """
    Parse COMTRADE date/time string.

    C37.111 says dd/mm/yyyy,hh:mm:ss.ssssss. Real relays disagree: SEL writes
    mm/dd/yyyy, and some exports use yyyy-mm-dd. Guessing wrongly is not a
    small error — 11/19/2023 read as dd/mm gives month 19 and the file simply
    fails to open, which is what a whole folder of real events did.

    So: take the standard order first, and fall back to the reading that
    produces a real date. Ambiguous dates (both halves <= 12) keep the
    standard's order rather than silently preferring one vendor.

    Also tolerates a space separator, a missing fractional part, and a
    two-digit year.
    """
    s = s.strip()

    # Accept comma or space between date and time
    s = s.replace(" ", ",", 1)
    parts = s.split(",")
    date_str = parts[0].strip()
    time_str = parts[1].strip() if len(parts) > 1 else "00:00:00.000000"

    if "-" in date_str and "/" not in date_str:
        # yyyy-mm-dd, as some exports write it
        y, m, d = date_str.split("-")
    else:
        d, m, y = date_str.split("/")
        if int(m) > 12 and int(d) <= 12:
            d, m = m, d          # the file is mm/dd/yyyy
    if len(y) == 2:
        y = ("20" if int(y) < 70 else "19") + y

    if "." in time_str:
        time_part, frac = time_str.split(".", 1)
        # Pad or truncate to exactly 6 digits
        frac = (frac + "000000")[:6]
    else:
        time_part = time_str
        frac = "000000"

    h, mi, sec = time_part.split(":")
    # Seconds can be fractional in the field itself on some exports.
    if "." in sec:
        sec = sec.split(".", 1)[0]
    return datetime(int(y), int(m), int(d), int(h), int(mi), int(sec), int(frac))


# ---------------------------------------------------------------------------
# DAT parsing — ASCII
# ---------------------------------------------------------------------------

def _parse_dat_ascii_lines(lines: list, cfg: dict) -> EventRecord:
    """
    Parse ASCII DAT content.

    Each row: sample_number, timestamp_us, a1, a2, ..., aN, d1, d2, ..., dM
    Analog values are raw integers; digitals are individual 0/1 values.
    """
    n_analog  = cfg["n_analog"]
    n_digital = cfg["n_digital"]
    timemult  = cfg["timemult"]

    timestamps   = []
    analog_raw   = [[] for _ in range(n_analog)]
    digital_raw  = [[] for _ in range(n_digital)]

    for line in lines:
        line = line.strip()
        if not line:
            continue

        parts = line.split(",")
        expected = 2 + n_analog + n_digital
        if len(parts) < expected:
            continue  # skip malformed rows

        try:
            ts = float(parts[1]) * timemult   # microseconds
            timestamps.append(ts)

            for i in range(n_analog):
                analog_raw[i].append(float(parts[2 + i]))

            for i in range(n_digital):
                digital_raw[i].append(int(parts[2 + n_analog + i]))
        except (ValueError, IndexError):
            continue

    return _build_record(np.array(timestamps), analog_raw, digital_raw, cfg)


# ---------------------------------------------------------------------------
# DAT parsing — Binary
# ---------------------------------------------------------------------------

def _parse_dat_binary_file(dat_path: str, cfg: dict) -> EventRecord:
    with open(dat_path, "rb") as fh:
        raw_bytes = fh.read()
    return _parse_dat_binary_bytes(raw_bytes, cfg)


def _parse_dat_binary_bytes(data: bytes, cfg: dict) -> EventRecord:
    """
    Parse binary DAT content.

    Per-sample layout (little-endian):
      uint32   sequence number
      uint32   timestamp (microseconds × timemult)
      int16    × n_analog   (raw analog values, signed)
      uint16   × ⌈n_digital/16⌉  (digital channels packed LSB-first per word)
    """
    n_analog   = cfg["n_analog"]
    n_digital  = cfg["n_digital"]
    timemult   = cfg["timemult"]
    n_dig_words = (n_digital + 15) // 16   # ceil(nD/16)

    # Bytes per sample
    sample_size = 4 + 4 + 2 * n_analog + 2 * n_dig_words

    timestamps  = []
    analog_raw  = [[] for _ in range(n_analog)]
    digital_raw = [[] for _ in range(n_digital)]

    n_data = len(data)
    offset = 0

    while offset + sample_size <= n_data:
        # Sequence number (not used in analysis but consumed)
        offset += 4

        # Timestamp
        ts = struct.unpack_from("<I", data, offset)[0] * timemult
        offset += 4
        timestamps.append(ts)

        # Analog channels (signed 16-bit)
        for i in range(n_analog):
            val = struct.unpack_from("<h", data, offset)[0]
            offset += 2
            analog_raw[i].append(float(val))

        # Digital words (16 channels per uint16)
        dig_words = []
        for _ in range(n_dig_words):
            word = struct.unpack_from("<H", data, offset)[0]
            offset += 2
            dig_words.append(word)

        for i in range(n_digital):
            word_idx = i // 16
            bit_idx  = i % 16
            digital_raw[i].append((dig_words[word_idx] >> bit_idx) & 1)

    return _build_record(np.array(timestamps), analog_raw, digital_raw, cfg)


# ---------------------------------------------------------------------------
# Record assembly
# ---------------------------------------------------------------------------

def _build_record(timestamps_us: np.ndarray, analog_raw: list,
                  digital_raw: list, cfg: dict) -> EventRecord:
    """
    Convert raw parsed arrays into a fully-scaled EventRecord.

    Scaling: engineering_value = a * raw_integer + b
    Time vector: convert microseconds to seconds (relative to recording start).
    """
    time = timestamps_us * 1e-6  # µs → s

    # Apply scaling to each analog channel
    analog_channels: dict = {}
    analog_info: dict = {}
    for i, ch_cfg in enumerate(cfg["analog_channels"]):
        name = ch_cfg["name"]
        a    = ch_cfg["multiplier"]
        b    = ch_cfg["offset"]
        raw  = np.array(analog_raw[i], dtype=np.float64)
        analog_channels[name] = a * raw + b
        analog_info[name] = ChannelInfo(
            name=name,
            units=ch_cfg["units"],
            multiplier=a,
            offset=b,
            phase=ch_cfg["phase"],
            circuit_id=ch_cfg["circuit_id"],
        )

    # Digital channels — simple 0/1 arrays
    digital_channels: dict = {}
    for i, ch_cfg in enumerate(cfg["digital_channels"]):
        name = ch_cfg["name"]
        digital_channels[name] = np.array(digital_raw[i], dtype=np.int8)

    # Trigger offset in seconds from recording start
    start_dt   = cfg["start_time"]
    trigger_dt = cfg["trigger_time"]
    trigger_offset_s = (trigger_dt - start_dt).total_seconds()

    # Find trigger sample index (first sample at or after trigger offset)
    if len(time) > 0:
        trigger_index = int(np.searchsorted(time, trigger_offset_s))
        trigger_index = min(trigger_index, len(time) - 1)
    else:
        trigger_index = 0

    # Primary sample rate
    sample_rate = cfg["sample_rates"][0]["rate"] if cfg["sample_rates"] else 0.0

    metadata = {
        "station_name": cfg["station_name"],
        "rec_dev_id":   cfg["rec_dev_id"],
        "rev_year":     cfg["rev_year"],
        "line_freq":    cfg["line_freq"],
        "start_time":   start_dt,
        "trigger_time_abs": trigger_dt,
        "file_type":    cfg["file_type"],
        "sample_rates": cfg["sample_rates"],
    }

    return EventRecord(
        time=time,
        analog_channels=analog_channels,
        digital_channels=digital_channels,
        analog_info=analog_info,
        sample_rate=sample_rate,
        trigger_time=trigger_offset_s,
        trigger_index=trigger_index,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _find_case(base: str, ext: str) -> Optional[str]:
    """Try both lower-case and upper-case extension; return first that exists."""
    for candidate in (base + ext, base + ext.upper()):
        if os.path.isfile(candidate):
            return candidate
    return None
