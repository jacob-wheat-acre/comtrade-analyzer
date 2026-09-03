"""
comtrade_parser.py — IEEE C37.111 COMTRADE file parser.

Supports:
  - Revisions 1991, 1999, and 2013
  - All four data file types the standard defines (7.4.9): ASCII, BINARY,
    BINARY32, FLOAT32
  - .cfg + .dat file pairs
  - .cff combined file format (2013)

See EXPORT_GUIDE.md for which of those to ask the relay for, and why.

Parsing assumptions
-------------------
* Sample times come from the CFG rate sections where they exist, and from the
  DAT timestamp column otherwise.  7.4.7 prefers the rate, and a recorder that
  populates nrates is entitled to leave the timestamp column zeroed.
* Timestamps, when used, are unsigned 32-bit integers in microseconds
  (multiplied by 'timemult' when provided).  Some relay vendors write the
  timestamp in different units; if values look wrong, check your vendor docs.
* Binary format: analog sample width follows the file type (2 bytes for
  BINARY, 4 for BINARY32/FLOAT32); digital channels are packed into 16-bit
  unsigned words (16 channels per word, LSB = channel 1).
* Engineering value = a * raw_integer + b  (from CFG channel descriptor).
* The trigger index is located by finding the first sample whose cumulative
  elapsed time equals or exceeds (trigger_datetime - start_datetime).
* Skew per channel is parsed but not applied (sub-microsecond effect; most
  post-fault analysis does not require it).
"""

import os
import re
from datetime import datetime
from typing import Optional, List

import numpy as np

from .data_model import EventRecord, ChannelInfo


# C37.111 7.4.9 names four data file types, and 8.6 gives the width of each.
# A relay that offers "Binary32" or "Float32" in its export menu writes that
# word into the CFG; matching only "BINARY" sent those files down the ASCII
# path, where every row failed to parse and the record came out empty.
_ANALOG_DTYPE = {
    "BINARY":   "<i2",
    "BINARY32": "<i4",
    "FLOAT32":  "<f4",
}

# What a record carries when the CFG left its date/time stamp blank or zeroed,
# which C37.111 7.4.8 permits. Compare against this rather than testing for a
# particular year: the record still analyses, it just cannot be placed on a
# timeline, and incident grouping needs to know that.
NO_DATETIME = datetime(1970, 1, 1)


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

        if cfg["file_type"] in _ANALOG_DTYPE:
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

        if cfg["file_type"] in _ANALOG_DTYPE:
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

    # C37.111 7.4.7: nrates == 0 does NOT mean "no rate line". It means the
    # sample period is not fixed, and a single line "0,endsamp" still follows.
    # Reading zero lines there leaves idx on that line and the start date is
    # then parsed from "0,<sample count>" — an event-triggered recorder's whole
    # folder fails to open for a rate line we declined to consume.
    sample_rates = []
    total_samples = 0
    for _ in range(max(n_rates, 1)):
        p = _split_cfg_line(lines[idx])
        rate     = float(p[0].strip())
        endsamp  = int(p[1].strip())
        if n_rates:
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
    # C37.111 7.4.9: ASCII, BINARY, BINARY32 or FLOAT32, non-case-sensitive.
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

    # C37.111 7.4.8 explicitly allows the stamp to be absent: the commas may
    # follow each other, or the field may be filled with zeros. Neither is a
    # broken file, so neither may stop it opening.
    if not date_str or set(date_str) <= set("0/-:. "):
        return NO_DATETIME

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

        # Stage the whole row and commit it only if every field parsed.
        # C37.111 8.4 writes a missing analog value as a null field, so a real
        # export does hit the failure path — and appending as we went left the
        # timestamp and the channels before the bad one one sample longer than
        # the rest. Ragged channels do not raise here; they surface much later
        # as nonsense in the analysis.
        try:
            ts     = float(parts[1]) * timemult   # microseconds
            arow   = [float(parts[2 + i]) for i in range(n_analog)]
            drow   = [int(parts[2 + n_analog + i]) for i in range(n_digital)]
        except (ValueError, IndexError):
            continue

        timestamps.append(ts)
        for i, v in enumerate(arow):
            analog_raw[i].append(v)
        for i, v in enumerate(drow):
            digital_raw[i].append(v)

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

    Per-sample layout (little-endian), C37.111 8.6:
      uint32   sequence number
      uint32   timestamp (× timemult)
      analog   × n_analog   — int16 (BINARY), int32 (BINARY32), float32 (FLOAT32)
      uint16   × ⌈n_digital/16⌉  (status channels packed LSB-first per word)

    Read as one structured array rather than a per-sample struct loop: a
    128 samples/cycle export of a two-second record is ~15 000 samples, and
    the loop cost showed up on real files.
    """
    n_analog   = cfg["n_analog"]
    n_digital  = cfg["n_digital"]
    timemult   = cfg["timemult"]
    n_dig_words = (n_digital + 15) // 16   # ceil(nD/16)
    analog_dt   = _ANALOG_DTYPE[cfg["file_type"]]

    dt = np.dtype([
        ("n",  "<u4"),
        ("ts", "<u4"),
        ("a",  analog_dt, (n_analog,)),
        ("d",  "<u2",     (n_dig_words,)),
    ])

    # A truncated final sample is a real occurrence on an interrupted
    # download; take the whole samples and drop the tail rather than raising.
    n_samples = len(data) // dt.itemsize
    rows = np.frombuffer(data, dtype=dt, count=n_samples)

    timestamps = rows["ts"].astype(np.float64) * timemult
    analog_raw = [rows["a"][:, i].astype(np.float64) for i in range(n_analog)]

    digital_raw = []
    for i in range(n_digital):
        word = rows["d"][:, i // 16]
        digital_raw.append((word >> (i % 16)) & 1)

    return _build_record(timestamps, analog_raw, digital_raw, cfg)


# ---------------------------------------------------------------------------
# Record assembly
# ---------------------------------------------------------------------------

def _time_from_rates(n_samples: int, sample_rates: list):
    """
    Sample times in seconds from the CFG rate sections, or None if the CFG
    does not carry usable ones.

    C37.111 7.4.7: the DAT timestamp is non-critical when nrates and samp are
    nonzero, and "use of nrates and samp variables is preferred for precise
    timing". Vendors take that literally — a relay that populates nrates is
    entitled to write zeros down the whole timestamp column, and reading time
    from it then gives every sample t=0, which reaches the analysis as a
    record with no duration and no sample rate.
    """
    if not sample_rates or n_samples == 0:
        return None
    if any(s["rate"] <= 0 for s in sample_rates):
        return None

    time = np.empty(n_samples, dtype=np.float64)
    first = 0        # 0-based index of the first sample in this section
    t0 = 0.0
    for sec in sample_rates:
        last = min(int(sec["end_sample"]), n_samples)   # 1-based, inclusive
        if last <= first:
            continue
        count = last - first
        step = 1.0 / sec["rate"]
        time[first:last] = t0 + np.arange(count) * step
        t0 += count * step
        first = last

    if first < n_samples:
        # endsamp under-reports the DAT — extend at the last known rate rather
        # than leaving the tail uninitialised.
        step = 1.0 / sample_rates[-1]["rate"]
        time[first:] = t0 + np.arange(n_samples - first) * step

    return time


def _build_record(timestamps_us: np.ndarray, analog_raw: list,
                  digital_raw: list, cfg: dict) -> EventRecord:
    """
    Convert raw parsed arrays into a fully-scaled EventRecord.

    Scaling: engineering_value = a * raw_integer + b
    Time vector: from the CFG sample rates where they exist, else the DAT
    timestamp column.
    """
    # Everything downstream indexes the channels with the same integers it
    # uses on the time vector, so they have to be the same length. Truncating
    # to the shortest is the honest reading of a short row: the samples up to
    # there are real.
    n_samples = min([len(timestamps_us)]
                    + [len(c) for c in analog_raw]
                    + [len(c) for c in digital_raw])
    timestamps_us = np.asarray(timestamps_us, dtype=np.float64)[:n_samples]
    analog_raw  = [c[:n_samples] for c in analog_raw]
    digital_raw = [c[:n_samples] for c in digital_raw]

    time = _time_from_rates(n_samples, cfg["sample_rates"])
    if time is None:
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
