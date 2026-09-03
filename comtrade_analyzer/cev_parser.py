"""
cev_parser.py — SEL Compressed Event (CEV) file parser.

Produces an EventRecord compatible with all downstream analysis.

CEV structure (all rows are CSV, last field is a CRC checksum that is stripped):
  Row 1  "FID","<checksum>"
  Row 2  "FID=SEL-...",   "<checksum>"
  Row 3  "MONTH","DAY","YEAR","HOUR","MIN","SEC","MSEC","<checksum>"
  Row 4  <month>,<day>,<year>,<hour>,<min>,<sec>,<msec>,"<checksum>"
  Row 5  summary column headers (REF_NUM, FREQ, SAM/CYC_A, …, "checksum")
  Row 6  summary values
  Row 7  per-sample column headers: analog names, FREQ, TRIG,
         "<space-separated digital names>","<checksum>"
  Row 8+ one row per sample: analog values, freq_value, trig_flag,
         "<hex_bitfield>","<checksum>"  (hex_bitfield blank when no digital
         sample, which happens when SAM/CYC_D < SAM/CYC_A)

Analog values are already in engineering units (A, kV, Hz, etc.).
Digital states are packed MSB-first in the hex bitfield.
"""

import csv
import io
import os
from datetime import datetime
from typing import Optional
import numpy as np

from .comtrade_parser import ChannelInfo, NO_DATETIME
from .data_model import EventRecord


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _strip_crc(line: str) -> list:
    """Parse one CEV line, strip the trailing CRC (last quoted field)."""
    row = next(csv.reader(io.StringIO(line)))
    return row[:-1]   # last field is always the 4-hex-char CRC


def _channel_name_unit(raw: str) -> tuple:
    """
    'VAY(kV)' → ('VAY', 'kV')
    'IA'      → ('IA', 'A')
    'FREQ'    → ('FREQ', 'Hz')
    """
    raw = raw.strip()
    if raw.endswith(')') and '(' in raw:
        name, _, rest = raw.rpartition('(')
        return name.strip(), rest.rstrip(')')
    # Infer unit from name when not explicit
    up = raw.upper()
    if up in ('FREQ',):
        return raw, 'Hz'
    if up.startswith('I') and (len(up) <= 2 or not up[1].isalpha() or up[1] in 'ABCGNX'):
        return raw, 'A'
    if up.startswith('V'):
        return raw, 'V'
    return raw, ''


def _decode_bits(hex_str: str, n: int) -> list:
    """Unpack hex_str into n individual 0/1 values, MSB-first per byte."""
    s = hex_str.strip()
    if not s:
        return [0] * n
    try:
        raw = bytes.fromhex(s)
    except ValueError:
        return [0] * n
    bits = []
    for byte in raw:
        for shift in range(7, -1, -1):
            bits.append((byte >> shift) & 1)
    return bits[:n]


def _safe_float(s) -> Optional[float]:
    try:
        return float(str(s).strip())
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Public parser
# ---------------------------------------------------------------------------

class CEVParser:
    """Parse a SEL .CEV file and return an EventRecord."""

    def parse(self, filepath: str) -> EventRecord:
        with open(filepath, encoding='utf-8', errors='replace') as fh:
            raw_lines = [ln.rstrip('\r\n') for ln in fh if ln.strip()]

        if len(raw_lines) < 8:
            raise ValueError(f"CEV file too short: {filepath}")

        # --- FID (rows 1-2) -----------------------------------------------
        fid_row = _strip_crc(raw_lines[1])
        fid_str = fid_row[0].strip() if fid_row else ''
        # Strip leading "FID=" so we get the firmware identifier string
        rec_dev_id = fid_str[4:] if fid_str.upper().startswith('FID=') else fid_str

        # --- Datetime (rows 3-4) ------------------------------------------
        dt_vals = _strip_crc(raw_lines[3])
        try:
            mo, dy, yr = int(dt_vals[0]), int(dt_vals[1]), int(dt_vals[2])
            hr, mn, sc, ms = int(dt_vals[3]), int(dt_vals[4]), int(dt_vals[5]), int(dt_vals[6])
            event_dt = datetime(yr, mo, dy, hr, mn, sc, ms * 1000)
        except (ValueError, IndexError):
            event_dt = NO_DATETIME

        # --- Summary (rows 5-6) -------------------------------------------
        sum_hdr = _strip_crc(raw_lines[4])
        sum_val = _strip_crc(raw_lines[5])
        summary = dict(zip([h.strip() for h in sum_hdr],
                           [v.strip() for v in sum_val]))

        freq          = float(summary.get('FREQ', '60') or '60')
        samp_per_cyc_a = int(summary.get('SAM/CYC_A', '16') or '16')
        samp_per_cyc_d = int(summary.get('SAM/CYC_D', str(samp_per_cyc_a)) or str(samp_per_cyc_a))
        sample_rate    = freq * samp_per_cyc_a

        event_type = summary.get('EVENT', '').strip().strip('"')
        shot_s     = summary.get('SHOT', '').strip()
        shot       = int(shot_s) if shot_s.lstrip('-').isdigit() else None

        loc_s = summary.get('LOCATION', '').strip()
        fault_location_mi = _safe_float(loc_s)   # '$$$$$$$$' → None

        # --- Column headers (row 7) ---------------------------------------
        col_hdr = _strip_crc(raw_lines[6])
        # Last field is all digital channel names, space-separated.
        digital_names_raw = col_hdr[-1].split() if col_hdr else []
        analog_col_hdr    = col_hdr[:-1]

        # Find the TRIG column — always present; everything before it
        # (including FREQ) counts as analog.
        trig_col = next((i for i, h in enumerate(analog_col_hdr)
                         if h.strip().upper() == 'TRIG'), len(analog_col_hdr) - 1)
        # digital bitfield is the column right after TRIG in each data row
        digital_col = trig_col + 1

        raw_analog_hdrs = analog_col_hdr[:trig_col]  # all cols before TRIG
        n_analog  = len(raw_analog_hdrs)
        n_digital = len(digital_names_raw)

        channel_meta = [_channel_name_unit(h) for h in raw_analog_hdrs]

        # --- Sample rows (row 8 onward) -----------------------------------
        analog_buf   = [[] for _ in range(n_analog)]
        digital_buf  = [[] for _ in range(n_digital)]
        last_digital = [0] * n_digital
        trigger_idx  = None
        n_rows        = 0

        for line in raw_lines[7:]:
            row = _strip_crc(line)
            # Need at least analog values + TRIG
            if len(row) < n_analog + 1:
                continue
            try:
                avals = [float(row[i]) for i in range(n_analog)]
            except ValueError:
                continue

            # Trigger detection
            if trigger_idx is None and trig_col < len(row):
                tval = row[trig_col].strip()
                if tval:
                    trigger_idx = n_rows

            # Digital bitfield
            hex_field = row[digital_col].strip() if digital_col < len(row) else ''
            if hex_field:
                last_digital = _decode_bits(hex_field, n_digital)
            # Always write last known digital state (hold-last-value)
            for i, v in enumerate(last_digital):
                digital_buf[i].append(v)

            for i, v in enumerate(avals):
                analog_buf[i].append(v)
            n_rows += 1

        # If digital buffer length diverged from analog (can happen with
        # malformed rows), truncate everything to shortest.
        n_samples = n_rows
        if analog_buf:
            n_samples = min(n_samples, *(len(c) for c in analog_buf))
        if digital_buf:
            n_samples = min(n_samples, *(len(c) for c in digital_buf))

        time = np.arange(n_samples) / sample_rate if sample_rate > 0 else np.zeros(n_samples)

        if trigger_idx is None:
            trigger_idx = 0
        trigger_offset_s = float(trigger_idx) / sample_rate if sample_rate > 0 else 0.0

        # --- Build channel dicts ------------------------------------------
        analog_channels: dict = {}
        analog_info: dict     = {}
        for (name, unit), buf in zip(channel_meta, analog_buf):
            analog_channels[name] = np.array(buf[:n_samples], dtype=np.float64)
            analog_info[name] = ChannelInfo(
                name=name, units=unit,
                multiplier=1.0, offset=0.0,
                phase='', circuit_id='',
            )

        # Skip '*' channels (unnamed/reserved bits in the packed word)
        digital_channels: dict = {}
        for name, buf in zip(digital_names_raw, digital_buf):
            if name == '*':
                continue
            digital_channels[name] = np.array(buf[:n_samples], dtype=np.int8)

        metadata = {
            'station_name':          '',
            'rec_dev_id':            rec_dev_id,
            'rev_year':              'CEV',
            'line_freq':             freq,
            'start_time':            event_dt,
            'trigger_time_abs':      event_dt,
            'file_type':             'CEV',
            'sample_rates':          [{'rate': sample_rate, 'end_sample': n_samples}],
            # CEV-specific metadata not in COMTRADE
            'cev_event_type':        event_type,
            'cev_shot':              shot,
            'cev_fault_location_mi': fault_location_mi,
            'cev_filtered':          os.path.basename(filepath).upper().endswith('_FILTERED.CEV'),
            'cev_peak_ia':           _safe_float(summary.get('IA')),
            'cev_peak_ib':           _safe_float(summary.get('IB')),
            'cev_peak_ic':           _safe_float(summary.get('IC')),
            'cev_peak_ig':           _safe_float(summary.get('IG')),
        }

        return EventRecord(
            time=time,
            analog_channels=analog_channels,
            digital_channels=digital_channels,
            analog_info=analog_info,
            sample_rate=sample_rate,
            trigger_time=trigger_offset_s,
            trigger_index=trigger_idx,
            metadata=metadata,
        )
