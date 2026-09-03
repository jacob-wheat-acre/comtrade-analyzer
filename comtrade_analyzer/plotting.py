"""
plotting.py — Visualization for COMTRADE relay event data.

All plot functions accept an optional save_path; if provided the figure is
written to disk instead of displayed.  All functions return the Figure so
callers can further customize or embed in a GUI.
"""

from __future__ import annotations

from typing import List, Optional
import numpy as np
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.lines import Line2D

from .data_model import EventRecord
from .analysis import (
    compute_rms,
    detect_fault_inception,
    detect_trip_time,
    compute_sequence_components,
    compute_phasors_at,
)

# Deliberately no matplotlib.use() here.
#
# This module used to force "TkAgg" (despite the comment claiming otherwise).
# Because app.py imports this module *after* setting "Agg", the force won, so
# the GUI ran the interactive Tk backend while rendering figures on a worker
# thread. Tk is not thread-safe: that corrupted the Tcl interpreter and the
# process segfaulted in Tcl_DeleteHashEntry during teardown at quit.
#
# Leaving the backend alone lets the caller decide — app.py pins "Agg" before
# importing this, and the CLI gets matplotlib's own default (interactive on a
# desktop, Agg when headless), which is what plt.show() below expects.
import matplotlib

# Color palettes — readable on white background
_COLORS_CURRENT = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728"]
_COLORS_VOLTAGE = ["#9467bd", "#8c564b", "#e377c2", "#17becf"]


# ---------------------------------------------------------------------------
# Main event plot
# ---------------------------------------------------------------------------

def plot_event(
    record: EventRecord,
    current_channels: Optional[List[str]] = None,
    voltage_channels: Optional[List[str]] = None,
    digital_channels: Optional[List[str]] = None,
    show_rms: bool = True,
    window_s: Optional[float] = None,
    title: Optional[str] = None,
    save_path: Optional[str] = None,
) -> plt.Figure:
    """
    Produce the canonical relay-event overview plot.

    Panel layout (top to bottom):
      1. Current waveforms  [optional RMS overlay as dashed]
      2. Voltage waveforms  [if present]
      3. Each digital channel as a step plot  [one row each]

    Vertical markers:
      Red dashed  — fault inception (auto-detected)
      Green dotted — first TRIP rising edge

    Parameters
    ----------
    window_s : float, optional
        If given, only the ±window_s/2 window around the trigger is shown.
        Useful for zooming into the fault with e.g. window_s=0.1 (100 ms).
    """
    # Auto-select channels
    if current_channels is None:
        current_channels = _auto_channels(record, ("IA", "IB", "IC", "IN",
                                                    "I_A", "I_B", "I_C",
                                                    "CURR", "PHAS"))
    if voltage_channels is None:
        voltage_channels = _auto_channels(record, ("VA", "VB", "VC", "VN",
                                                    "V_A", "V_B", "V_C",
                                                    "VOLT"))
    if digital_channels is None:
        if len(record.digital_channels) > 20:
            from .analysis import extract_key_digital_bits
            digital_channels = [b["name"] for b in extract_key_digital_bits(record)]
        else:
            digital_channels = list(record.digital_channels.keys())

    # Filter to channels that actually exist
    current_channels = [n for n in current_channels if n in record.analog_channels]
    voltage_channels = [n for n in voltage_channels if n in record.analog_channels]
    digital_channels = [n for n in digital_channels if n in record.digital_channels]

    time = record.time

    # Build time mask for zoom window
    if window_s is not None:
        t0   = record.trigger_time - window_s / 2
        t1   = record.trigger_time + window_s / 2
        mask = (time >= t0) & (time <= t1)
    else:
        mask = np.ones(len(time), dtype=bool)

    t_plot = time[mask]

    # Detect event markers
    fault_idx  = detect_fault_inception(record, current_channels or None)
    trip_result = detect_trip_time(record)
    fault_t = float(record.time[fault_idx])  if fault_idx  is not None else None
    trip_t  = float(record.time[trip_result[0]]) if trip_result is not None else None

    # Build subplot grid
    n_current = 1 if current_channels else 0
    n_voltage = 1 if voltage_channels else 0
    n_digital = len(digital_channels)
    n_rows    = n_current + n_voltage + n_digital

    if n_rows == 0:
        print("No channels available to plot.")
        fig = plt.figure()
        return fig

    height_ratios = (
        ([3] * n_current)
        + ([2.5] * n_voltage)
        + ([0.9] * n_digital)
    )

    fig = plt.figure(figsize=(14, max(4, 1.5 * n_rows + 2)))
    gs  = gridspec.GridSpec(
        n_rows, 1,
        height_ratios=height_ratios,
        hspace=0.06,
        top=0.93, bottom=0.08, left=0.10, right=0.97,
    )

    axes      = [fig.add_subplot(gs[i]) for i in range(n_rows)]
    ax_bottom = axes[-1]

    row = 0

    # ---- Current panel ----
    if current_channels:
        ax = axes[row]
        for i, name in enumerate(current_channels):
            sig   = record.analog_channels[name][mask]
            color = _COLORS_CURRENT[i % len(_COLORS_CURRENT)]
            ax.plot(t_plot, sig, color=color, linewidth=0.75, label=name)

            if show_rms and record.sample_rate > 0:
                spc = record.samples_per_cycle()
                rms = compute_rms(record.analog_channels[name], spc)
                ax.plot(t_plot, rms[mask], color=color, linewidth=1.5,
                        linestyle="--", alpha=0.75, label=f"{name} RMS")

        _draw_markers(ax, fault_t, trip_t, record.trigger_time)
        units = _channel_units(record, current_channels[0]) if current_channels else "A"
        ax.set_ylabel(f"Current ({units})", fontsize=8)
        ax.legend(loc="upper right", fontsize=6.5, ncol=2)
        ax.grid(True, alpha=0.25, linestyle=":")
        ax.tick_params(labelbottom=False)
        row += 1

    # ---- Voltage panel ----
    if voltage_channels:
        ax = axes[row]
        for i, name in enumerate(voltage_channels):
            sig   = record.analog_channels[name][mask]
            color = _COLORS_VOLTAGE[i % len(_COLORS_VOLTAGE)]
            ax.plot(t_plot, sig, color=color, linewidth=0.75, label=name)

        _draw_markers(ax, fault_t, trip_t, record.trigger_time)
        units = _channel_units(record, voltage_channels[0]) if voltage_channels else "V"
        ax.set_ylabel(f"Voltage ({units})", fontsize=8)
        ax.legend(loc="upper right", fontsize=6.5)
        ax.grid(True, alpha=0.25, linestyle=":")
        ax.tick_params(labelbottom=(n_digital == 0))
        row += 1

    # ---- Digital channel rows ----
    for j, name in enumerate(digital_channels):
        ax  = axes[row]
        sig = record.digital_channels[name][mask].astype(float)
        ax.step(t_plot, sig, where="post", color="#222222", linewidth=1.0)
        ax.fill_between(t_plot, 0, sig, alpha=0.25, color="#555555", step="post")
        ax.set_ylim(-0.3, 1.6)
        ax.set_yticks([0, 1])
        ax.set_yticklabels(["0", "1"], fontsize=6)
        # Channel label on y-axis
        ax.set_ylabel(name, fontsize=7, rotation=0, ha="right", va="center",
                      labelpad=40)
        _draw_markers(ax, fault_t, trip_t, record.trigger_time)
        ax.grid(True, alpha=0.15, linestyle=":")
        is_last = (j == n_digital - 1)
        ax.tick_params(labelbottom=is_last)
        row += 1

    # Share x-axis across all panels
    for ax in axes[:-1]:
        ax.sharex(ax_bottom)

    ax_bottom.set_xlabel("Time (s)", fontsize=9)

    # Figure title
    if title is None:
        station = record.metadata.get("station_name", "")
        title   = f"Relay Event — {station}" if station else "Relay Event"
    fig.suptitle(title, fontsize=11, fontweight="bold")

    # Marker legend in top panel
    legend_handles = []
    legend_handles.append(Line2D([0], [0], color="orange",    linestyle="-",
                                  linewidth=1.0, alpha=0.6,  label="Trigger"))
    if fault_t is not None:
        legend_handles.append(Line2D([0], [0], color="red",   linestyle="--",
                                      linewidth=1.2, label="Fault Inception"))
    if trip_t is not None:
        legend_handles.append(Line2D([0], [0], color="green", linestyle=":",
                                      linewidth=1.4, label="Trip"))

    if legend_handles and n_rows > 0:
        # get_lines() also returns the unlabelled axvline markers drawn by
        # _draw_markers; matplotlib auto-names those '_child<N>' and they would
        # show up in the legend alongside the named handles built below.
        plotted = [ln for ln in axes[0].get_lines()
                   if not ln.get_label().startswith("_")]
        axes[0].legend(
            handles=plotted + legend_handles,
            loc="upper right", fontsize=6.5, ncol=2,
        )

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        print(f"Plot saved → {save_path}")
    else:
        plt.show()
        return fig          # interactive: the caller's window owns it

    # Saved to disk and nobody uses the return value, but pyplot holds a
    # global reference to every figure it creates. Without this a folder run
    # leaks one live figure (~45 MB here) per event, and a 100-event folder
    # walks the GUI into swap until macOS stops redrawing the window.
    plt.close(fig)
    return None


# ---------------------------------------------------------------------------
# RMS current comparison
# ---------------------------------------------------------------------------

def plot_rms_currents(
    record: EventRecord,
    current_channels: Optional[List[str]] = None,
    save_path: Optional[str] = None,
) -> plt.Figure:
    """
    Plot per-phase sliding-window RMS current.

    Useful for seeing the fault build-up and clearance without the
    waveform detail.
    """
    if current_channels is None:
        current_channels = _auto_channels(record, ("IA", "IB", "IC", "I_A", "I_B", "I_C"))

    current_channels = [n for n in current_channels if n in record.analog_channels]
    if not current_channels:
        print("No current channels found.")
        return plt.figure()

    spc = record.samples_per_cycle()

    fig, ax = plt.subplots(figsize=(12, 4))

    for i, name in enumerate(current_channels):
        rms = compute_rms(record.analog_channels[name], spc)
        ax.plot(record.time, rms, color=_COLORS_CURRENT[i % 4],
                linewidth=1.0, label=f"{name} RMS")

    fault_idx  = detect_fault_inception(record, current_channels)
    trip_result = detect_trip_time(record)

    ax.axvline(record.trigger_time, color="orange", linewidth=1.0,
               linestyle="-", alpha=0.5, label="Trigger")
    if fault_idx is not None:
        ax.axvline(record.time[fault_idx], color="red", linestyle="--",
                   linewidth=1.2, label="Fault Inception")
    if trip_result is not None:
        ax.axvline(record.time[trip_result[0]], color="green", linestyle=":",
                   linewidth=1.4, label="Trip")

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("RMS Current (A)")
    ax.set_title("Sliding One-Cycle RMS — Phase Currents")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3, linestyle=":")
    plt.tight_layout()

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        print(f"RMS plot saved → {save_path}")
    else:
        plt.show()
        return fig          # interactive: the caller's window owns it

    # Saved to disk and nobody uses the return value, but pyplot holds a
    # global reference to every figure it creates. Without this a folder run
    # leaks one live figure (~45 MB here) per event, and a 100-event folder
    # walks the GUI into swap until macOS stops redrawing the window.
    plt.close(fig)
    return None


# ---------------------------------------------------------------------------
# Sequence component plot
# ---------------------------------------------------------------------------

def plot_sequence_components(
    record: EventRecord,
    save_path: Optional[str] = None,
) -> Optional[plt.Figure]:
    """
    Plot positive-, negative-, and zero-sequence current magnitudes.

    Requires IA, IB, IC channels to be present.
    """
    def _get(*names):
        for n in names:
            ch = record.get_channel(n)
            if ch is not None:
                return ch
        return None

    ia = _get("IA", "Ia", "I_A")
    ib = _get("IB", "Ib", "I_B")
    ic = _get("IC", "Ic", "I_C")

    if ia is None or ib is None or ic is None:
        print("Sequence plot requires IA, IB, IC channels.")
        return None

    spc = record.samples_per_cycle()
    i0, i1, i2 = compute_sequence_components(ia, ib, ic, spc)

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(record.time, i1, color="#1f77b4", linewidth=1.0, label="|I₁| Positive")
    ax.plot(record.time, i2, color="#ff7f0e", linewidth=1.0, label="|I₂| Negative")
    ax.plot(record.time, i0, color="#2ca02c", linewidth=1.0, label="|I₀| Zero")

    _draw_markers(ax, None, None, record.trigger_time)
    fault_idx  = detect_fault_inception(record)
    trip_result = detect_trip_time(record)
    if fault_idx is not None:
        ax.axvline(record.time[fault_idx], color="red",   linestyle="--",
                   linewidth=1.2, label="Fault Inception")
    if trip_result is not None:
        ax.axvline(record.time[trip_result[0]], color="green", linestyle=":",
                   linewidth=1.4, label="Trip")

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Sequence Current (A)")
    ax.set_title("Symmetrical Sequence Components")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3, linestyle=":")
    plt.tight_layout()

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        print(f"Sequence plot saved → {save_path}")
    else:
        plt.show()
        return fig          # interactive: the caller's window owns it

    # Saved to disk and nobody uses the return value, but pyplot holds a
    # global reference to every figure it creates. Without this a folder run
    # leaks one live figure (~45 MB here) per event, and a 100-event folder
    # walks the GUI into swap until macOS stops redrawing the window.
    plt.close(fig)
    return None


# ---------------------------------------------------------------------------
# Phasor diagram
# ---------------------------------------------------------------------------

def plot_phasors(
    record: EventRecord,
    t_fault_s: Optional[float] = None,
    save_path: Optional[str] = None,
) -> Optional[plt.Figure]:
    """
    Three-panel phasor diagram:
      Panel 1 — Voltage phasors (Va, Vb, Vc)
      Panel 2 — Current phasors (Ia, Ib, Ic)
      Panel 3 — Sequence current phasors (I1, I2, I0)

    Pre-fault phasors are drawn faded/dashed for reference.
    Fault phasors are drawn as bold arrows.
    All angles are referenced to Va (fault) = 0°.
    """
    ph = compute_phasors_at(record, t_fault_s=t_fault_s)
    if ph is None:
        print("Phasor plot: insufficient data (record too short).")
        return None

    fault = ph["fault"]
    pre   = ph["pre"]
    seq_i = ph.get("seq_i", {})

    # Identify voltage and current channels
    v_channels = [n for n, info in record.analog_info.items()
                  if "V" in info.units.upper() and n in fault]
    i_channels = [n for n, info in record.analog_info.items()
                  if "A" in info.units.upper() and n in fault
                  and "IN" not in n.upper()]

    # Prefer ordered ABC subsets
    def _pick_abc(names, patterns):
        ordered = []
        for pats in patterns:
            for n in names:
                if any(p in n.upper() for p in pats):
                    ordered.append(n)
                    break
        return ordered or names[:3]

    v_plot = _pick_abc(v_channels,
                       [("VAN", "VA", "VAN1"), ("VBN", "VB"), ("VCN", "VC")])
    i_plot = _pick_abc(i_channels,
                       [("IA", "I_A", "IA1"), ("IB", "I_B"), ("IC", "I_C")])

    fig, axes = plt.subplots(1, 3, figsize=(15, 5.5))

    v_colors = ["#9467bd", "#8c564b", "#e377c2"]
    i_colors = ["#1f77b4", "#ff7f0e", "#2ca02c"]
    s_colors = {"I1": "#1f77b4", "I2": "#d62728", "I0": "#2ca02c"}

    t_fault_label = ph["t_fault_s"] * 1000
    trig_ms       = record.trigger_time * 1000
    rel_ms        = t_fault_label - trig_ms

    fig.suptitle(
        f"Phasor Diagram  —  fault window at {t_fault_label:.1f} ms  "
        f"({rel_ms:+.1f} ms from trigger)  |  reference: {ph['ref_channel']} = 0°",
        fontsize=10, fontweight="bold",
    )

    # ── Panels 0 and 1: voltage and current ──────────────────────────────────
    for panel_idx, (channels, colors, label) in enumerate([
        (v_plot, v_colors, "Voltage"),
        (i_plot, i_colors, "Current"),
    ]):
        ax = axes[panel_idx]
        ax.set_aspect("equal")
        ax.axhline(0, color="#cccccc", lw=0.6)
        ax.axvline(0, color="#cccccc", lw=0.6)

        # Determine scale from fault phasors
        mags_fault = [fault[n]["mag"] for n in channels if n in fault]
        scale = max(mags_fault) if mags_fault else 1.0
        if scale < 1e-9:
            scale = 1.0

        # Background angle grid
        for deg in range(0, 360, 30):
            rad = np.radians(deg)
            ax.plot([0, np.cos(rad) * scale * 1.15],
                    [0, np.sin(rad) * scale * 1.15],
                    color="#eeeeee", lw=0.5, zorder=0)

        # Concentric reference circles
        for frac in (0.5, 1.0):
            circ = plt.Circle((0, 0), scale * frac, color="#dddddd",
                               fill=False, lw=0.6, linestyle="--", zorder=0)
            ax.add_patch(circ)

        # Pre-fault phasors (faded, dashed)
        for n, color in zip(channels, colors):
            if n not in pre:
                continue
            m, ang = pre[n]["mag"], pre[n]["ang_deg"]
            x = m * np.cos(np.radians(ang))
            y = m * np.sin(np.radians(ang))
            ax.quiver(0, 0, x, y, angles="xy", scale_units="xy", scale=1,
                      color=color, alpha=0.30, width=0.005, zorder=1)

        # Fault phasors (bold)
        for n, color in zip(channels, colors):
            if n not in fault:
                continue
            m, ang = fault[n]["mag"], fault[n]["ang_deg"]
            x = m * np.cos(np.radians(ang))
            y = m * np.sin(np.radians(ang))
            ax.quiver(0, 0, x, y, angles="xy", scale_units="xy", scale=1,
                      color=color, alpha=0.95, width=0.008, zorder=3)
            # Label at tip
            ax.text(x * 1.12, y * 1.12,
                    f"{n}\n{m:.0f}∠{ang:.0f}°",
                    color=color, fontsize=7, ha="center", va="center",
                    fontweight="bold")

        units = record.analog_info[channels[0]].units if channels else ""
        ax.set_title(f"{label} Phasors ({units})", fontsize=9)
        ax.set_xlabel("Re", fontsize=8)
        ax.set_ylabel("Im", fontsize=8)
        lim = scale * 1.3
        ax.set_xlim(-lim, lim)
        ax.set_ylim(-lim, lim)
        ax.tick_params(labelsize=7)
        ax.grid(False)

        # Angle labels on the outer ring
        for deg, label_txt in [(0, "0°"), (90, "90°"), (180, "180°"), (270, "270°")]:
            rad = np.radians(deg)
            ax.text(np.cos(rad) * lim * 0.92,
                    np.sin(rad) * lim * 0.92,
                    label_txt, fontsize=6.5, ha="center", va="center",
                    color="#aaaaaa")

    # ── Panel 2: sequence current phasors ────────────────────────────────────
    ax = axes[2]
    ax.set_aspect("equal")
    ax.axhline(0, color="#cccccc", lw=0.6)
    ax.axvline(0, color="#cccccc", lw=0.6)

    seq_items = [
        ("I1", seq_i.get("I1", 0+0j), "Positive (I₁)"),
        ("I2", seq_i.get("I2", 0+0j), "Negative (I₂)"),
        ("I0", seq_i.get("I0", 0+0j), "Zero (I₀)"),
    ]

    seq_scale = max((abs(c) for _, c, _ in seq_items), default=1.0)
    if seq_scale < 1e-9:
        seq_scale = 1.0

    for deg in range(0, 360, 30):
        rad = np.radians(deg)
        ax.plot([0, np.cos(rad) * seq_scale * 1.15],
                [0, np.sin(rad) * seq_scale * 1.15],
                color="#eeeeee", lw=0.5, zorder=0)
    for frac in (0.5, 1.0):
        circ = plt.Circle((0, 0), seq_scale * frac, color="#dddddd",
                           fill=False, lw=0.6, linestyle="--", zorder=0)
        ax.add_patch(circ)

    for key, c, lbl in seq_items:
        if abs(c) < 1e-6:
            continue
        color = s_colors[key]
        x = c.real
        y = c.imag
        ang = np.degrees(np.angle(c))
        ax.quiver(0, 0, x, y, angles="xy", scale_units="xy", scale=1,
                  color=color, alpha=0.95, width=0.009, zorder=3)
        ax.text(x * 1.15, y * 1.15,
                f"{lbl}\n{abs(c):.0f}∠{ang:.0f}°",
                color=color, fontsize=7, ha="center", va="center",
                fontweight="bold")

    ax.set_title("Sequence Currents (A)", fontsize=9)
    ax.set_xlabel("Re", fontsize=8)
    ax.set_ylabel("Im", fontsize=8)
    lim = seq_scale * 1.35
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.tick_params(labelsize=7)
    ax.grid(False)

    for deg, label_txt in [(0, "0°"), (90, "90°"), (180, "180°"), (270, "270°")]:
        rad = np.radians(deg)
        ax.text(np.cos(rad) * lim * 0.92,
                np.sin(rad) * lim * 0.92,
                label_txt, fontsize=6.5, ha="center", va="center",
                color="#aaaaaa")

    plt.tight_layout(rect=[0, 0, 1, 0.94])

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        print(f"Phasor plot saved → {save_path}")
    else:
        plt.show()
        return fig          # interactive: the caller's window owns it

    # Saved to disk and nobody uses the return value, but pyplot holds a
    # global reference to every figure it creates. Without this a folder run
    # leaks one live figure (~45 MB here) per event, and a 100-event folder
    # walks the GUI into swap until macOS stops redrawing the window.
    plt.close(fig)
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _draw_markers(
    ax: plt.Axes,
    fault_t: Optional[float],
    trip_t: Optional[float],
    trigger_t: Optional[float] = None,
):
    """Draw vertical event-time markers on an axis."""
    if trigger_t is not None:
        ax.axvline(trigger_t, color="orange", linewidth=0.8, linestyle="-", alpha=0.5)
    if fault_t is not None:
        ax.axvline(fault_t, color="red",   linewidth=1.0, linestyle="--", alpha=0.85)
    if trip_t is not None:
        ax.axvline(trip_t,  color="green", linewidth=1.2, linestyle=":",  alpha=0.85)


def _auto_channels(record: EventRecord, keywords: tuple) -> List[str]:
    """Return analog channel names that contain any of the given keywords."""
    return [
        n for n in record.analog_channels
        if any(k in n.upper() for k in keywords)
    ]


def _channel_units(record: EventRecord, name: str) -> str:
    """Return the units string for an analog channel."""
    info = record.analog_info.get(name)
    return info.units if info else ""
