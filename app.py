#!/usr/bin/env python3
"""
app.py — COMTRADE Relay Event Analyzer GUI

Double-click this file (or run: python3 app.py) to open the analyzer.
No command-line flags required.
"""

import platform
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# Set non-interactive matplotlib backend before any module import touches pyplot
import matplotlib
matplotlib.use("Agg")

_SCRIPT = Path(__file__).parent

# ── Platform-appropriate fonts ────────────────────────────────────────────────
_IS_WIN    = platform.system() == "Windows"
_FONT_UI   = ("Segoe UI", 11)         if _IS_WIN else ("Helvetica", 11)
_FONT_UI_B = ("Segoe UI", 13, "bold") if _IS_WIN else ("Helvetica", 13, "bold")
_FONT_UI_S = ("Segoe UI",  9)         if _IS_WIN else ("Helvetica",  9)
_FONT_MONO   = ("Consolas", 10)       if _IS_WIN else ("Menlo", 10)
_FONT_MONO_B = ("Consolas", 10, "bold") if _IS_WIN else ("Menlo", 10, "bold")

# ── Colors ────────────────────────────────────────────────────────────────────
_BG       = "#f5f5f5"
_BTN_RUN  = "#1A3A6B"   # dark navy matching report branding
_BTN_TXT  = "#ffffff"
_LOG_BG   = "#1e1e1e"
_LOG_FG    = "#d4d4d4"
_LOG_ERR   = "#f48771"
_LOG_INFO  = "#4ec9b0"
_LOG_TIER1 = "#e06c75"   # red   — immediate review
_LOG_TIER2 = "#e5c07b"   # amber — routine review
_LOG_TIER3 = "#98c379"   # green — archive
_LABEL_FG  = "#333333"

# ── Module imports ────────────────────────────────────────────────────────────
try:
    sys.path.insert(0, str(_SCRIPT))
    from comtrade_parser import COMTRADEParser
    from analysis import compute_event_summary
    from plotting import plot_event, plot_rms_currents, plot_sequence_components, plot_phasors
    from report import generate_report, generate_word_report
    from feeder_analysis import compute_feeder_summary
    from triage import triage_event
    _MODULES_OK = True
    _import_error = ""
except Exception as _exc:
    import traceback as _tb
    (_SCRIPT / "import_error.log").write_text(_tb.format_exc())
    _MODULES_OK = False
    _import_error = str(_exc)

import logging as _logging


# ── GUI log handler (routes logging records into the Text widget) ─────────────

class _GUILogHandler(_logging.Handler):
    def __init__(self, log_widget, after_fn):
        super().__init__()
        self._widget = log_widget
        self._after  = after_fn

    def emit(self, record):
        msg = self.format(record) + "\n"
        tag = "error" if record.levelno >= _logging.WARNING else "info"
        self._after(0, lambda m=msg, t=tag: self._write(m, t))

    def _write(self, msg, tag):
        self._widget.config(state="normal")
        self._widget.insert("end", msg, tag)
        self._widget.see("end")
        self._widget.config(state="disabled")


# ── Main application window ───────────────────────────────────────────────────

class COMTRADEApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("COMTRADE Relay Event Analyzer")
        self.resizable(True, True)
        self.configure(bg=_BG)
        self.minsize(720, 580)
        self._set_icon()
        self._build_ui()
        self._running = False

        if not _MODULES_OK:
            messagebox.showerror(
                "Import Error",
                f"Failed to load analysis modules:\n\n{_import_error}\n\n"
                "Check import_error.log for details.",
            )

    def _set_icon(self):
        try:
            png = _SCRIPT / "icon.png"
            if png.exists():
                from PIL import Image, ImageTk
                img = Image.open(png).resize((64, 64), Image.LANCZOS)
                self._tk_icon = ImageTk.PhotoImage(img)
                self.iconphoto(True, self._tk_icon)
        except Exception:
            pass  # icon is cosmetic

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}

        # ── File row ──────────────────────────────────────────────────────────
        file_frame = tk.Frame(self, bg=_BG)
        file_frame.pack(fill="x", **pad)

        tk.Label(file_frame, text="Event File", width=16, anchor="w",
                 bg=_BG, fg=_LABEL_FG, font=_FONT_UI).pack(side="left")
        self._file_var = tk.StringVar()
        tk.Entry(file_frame, textvariable=self._file_var, font=_FONT_UI,
                 width=40).pack(side="left", padx=(0, 6), fill="x", expand=True)
        tk.Button(file_frame, text="Browse…", command=self._browse_file,
                  font=_FONT_UI).pack(side="left")
        tk.Button(file_frame, text="Folder…", command=self._browse_folder,
                  font=_FONT_UI).pack(side="left", padx=(4, 0))

        # ── Location row ──────────────────────────────────────────────────────
        loc_frame = tk.Frame(self, bg=_BG)
        loc_frame.pack(fill="x", **pad)

        tk.Label(loc_frame, text="Location", width=16, anchor="w",
                 bg=_BG, fg=_LABEL_FG, font=_FONT_UI).pack(side="left")
        self._location_var = tk.StringVar()
        tk.Entry(loc_frame, textvariable=self._location_var, font=_FONT_UI,
                 width=40).pack(side="left", fill="x", expand=True)
        tk.Label(loc_frame, text="(substation or circuit name)", bg=_BG, fg="#888888",
                 font=_FONT_UI_S).pack(side="left", padx=(6, 0))

        # ── Device type row ───────────────────────────────────────────────────
        dev_frame = tk.Frame(self, bg=_BG)
        dev_frame.pack(fill="x", **pad)

        tk.Label(dev_frame, text="Device Type", width=16, anchor="w",
                 bg=_BG, fg=_LABEL_FG, font=_FONT_UI).pack(side="left")
        self._device_var = tk.StringVar()
        tk.Entry(dev_frame, textvariable=self._device_var, font=_FONT_UI,
                 width=40).pack(side="left", fill="x", expand=True)
        tk.Label(dev_frame, text="(e.g. SEL-351R, GE D60, Beckwith M-3410)", bg=_BG,
                 fg="#888888", font=_FONT_UI_S).pack(side="left", padx=(6, 0))

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=12, pady=(4, 0))

        # ── Feeder / recloser section ─────────────────────────────────────────
        feeder_hdr = tk.Frame(self, bg=_BG)
        feeder_hdr.pack(fill="x", padx=12, pady=(6, 2))

        self._feeder_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            feeder_hdr,
            text="Feeder / Recloser Analysis",
            variable=self._feeder_var,
            bg=_BG, fg=_LABEL_FG, font=_FONT_UI,
            command=self._on_feeder_toggle,
        ).pack(side="left")
        tk.Label(feeder_hdr,
                 text="(multi-shot reclose sequence, fault location, HIF screen)",
                 bg=_BG, fg="#888888", font=_FONT_UI_S).pack(side="left", padx=(8, 0))

        self._feeder_params_frame = tk.Frame(self, bg=_BG)
        self._feeder_params_frame.pack(fill="x", padx=28, pady=(0, 4))

        _FW = 16   # label width inside feeder section

        def _feeder_row(label, var, hint="", width=12):
            f = tk.Frame(self._feeder_params_frame, bg=_BG)
            f.pack(fill="x", pady=2)
            tk.Label(f, text=label, width=_FW, anchor="w",
                     bg=_BG, fg=_LABEL_FG, font=_FONT_UI).pack(side="left")
            e = tk.Entry(f, textvariable=var, font=_FONT_UI, width=width,
                         state="disabled")
            e.pack(side="left")
            if hint:
                tk.Label(f, text=hint, bg=_BG, fg="#888888",
                         font=_FONT_UI_S).pack(side="left", padx=(6, 0))
            return e

        self._feeder_z_var   = tk.StringVar(value="0.4")
        self._source_kv_var  = tk.StringVar(value="12.47")
        self._hif_thresh_var = tk.StringVar(value="50")

        self._feeder_z_entry  = _feeder_row("Feeder Z",       self._feeder_z_var,
                                             "Ω/mile  (typical OH: 0.3–0.5, cable: 0.1–0.25)")
        self._source_kv_entry = _feeder_row("Source Voltage", self._source_kv_var,
                                             "kV L-L  (e.g. 12.47, 24.9, 34.5)")
        self._hif_entry       = _feeder_row("HIF Threshold",  self._hif_thresh_var,
                                             "A  (default 50; raise for heavy feeders)")

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=12, pady=(4, 0))

        # ── Report details (collapsible) ──────────────────────────────────────
        det_hdr = tk.Frame(self, bg=_BG)
        det_hdr.pack(fill="x", padx=12, pady=(4, 0))

        self._details_open = tk.BooleanVar(value=False)
        self._det_toggle_btn = tk.Button(
            det_hdr,
            text="▶  Report Details (engineer sign-off…)",
            command=self._toggle_details,
            bg=_BG, fg="#555555", font=_FONT_UI_S,
            relief="flat", cursor="hand2", anchor="w",
        )
        self._det_toggle_btn.pack(side="left", fill="x", expand=True)

        self._details_frame = tk.Frame(self, bg=_BG)
        # Not packed initially — expanded by the toggle button

        _DW = 18

        def _detail_row(label, var, placeholder=""):
            f = tk.Frame(self._details_frame, bg=_BG)
            f.pack(fill="x", padx=12, pady=2)
            tk.Label(f, text=label, width=_DW, anchor="w",
                     bg=_BG, fg=_LABEL_FG, font=_FONT_UI).pack(side="left")
            e = tk.Entry(f, textvariable=var, font=_FONT_UI, width=38)
            e.pack(side="left", fill="x", expand=True)
            if placeholder:
                tk.Label(f, text=placeholder, bg=_BG, fg="#888888",
                         font=_FONT_UI_S).pack(side="left", padx=(6, 0))
            return e

        tk.Label(self._details_frame,
                 text="  Engineer sign-off", bg=_BG, fg="#555555", font=_FONT_UI_S,
                 ).pack(anchor="w", padx=12, pady=(6, 0))

        self._eng_name_var    = tk.StringVar()
        self._eng_title_var   = tk.StringVar()
        self._eng_contact_var = tk.StringVar()

        _detail_row("Name",    self._eng_name_var,    "(e.g. Jane Smith)")
        _detail_row("Title",   self._eng_title_var,   "(e.g. Protection Engineer)")
        _detail_row("Contact", self._eng_contact_var, "(phone / email)")

        tk.Frame(self._details_frame, bg=_BG, height=6).pack()

        # ── Analysis options + Run button ─────────────────────────────────────
        self._sep_before_run = ttk.Separator(self, orient="horizontal")
        self._sep_before_run.pack(fill="x", padx=12, pady=(4, 0))

        opt_frame = tk.Frame(self, bg=_BG)
        opt_frame.pack(fill="x", padx=12, pady=(6, 2))

        tk.Label(opt_frame, text="Options", width=16, anchor="w",
                 bg=_BG, fg=_LABEL_FG, font=_FONT_UI).pack(side="left")

        self._save_plots_var  = tk.BooleanVar(value=True)
        self._rms_plot_var    = tk.BooleanVar(value=False)
        self._seq_plot_var    = tk.BooleanVar(value=False)
        self._phasor_plot_var = tk.BooleanVar(value=True)
        self._csv_var         = tk.BooleanVar(value=False)
        self._json_var        = tk.BooleanVar(value=False)

        def _opt_chk(text, var):
            tk.Checkbutton(opt_frame, text=text, variable=var,
                           bg=_BG, fg=_LABEL_FG, font=_FONT_UI_S,
                           ).pack(side="left", padx=(0, 12))

        _opt_chk("Save Plots",  self._save_plots_var)
        _opt_chk("Phasor Plot", self._phasor_plot_var)
        _opt_chk("RMS Plot",    self._rms_plot_var)
        _opt_chk("Seq Plot",    self._seq_plot_var)
        _opt_chk("CSV",         self._csv_var)
        _opt_chk("JSON",        self._json_var)

        btn_frame = tk.Frame(self, bg=_BG)
        btn_frame.pack(fill="x", padx=12, pady=(4, 8))

        self._run_btn = tk.Button(
            btn_frame, text="Run Analysis",
            command=self._run,
            bg=_BTN_RUN, fg=_BTN_TXT, activebackground="#0e2347",
            font=_FONT_UI_B,
            relief="flat", cursor="hand2", padx=20, pady=8,
        )
        self._run_btn.pack(side="left")

        self._open_btn = tk.Button(
            btn_frame, text="Open Output Folder",
            command=self._open_folder,
            font=_FONT_UI, relief="flat", cursor="hand2",
            bg=_BG, padx=12, pady=8,
        )
        self._open_btn.pack(side="left", padx=(12, 0))
        self._open_btn.config(state="disabled")

        tk.Button(
            btn_frame, text="? Help",
            command=self._show_help,
            font=_FONT_UI, relief="flat", cursor="hand2",
            bg=_BG, fg="#555555", padx=12, pady=8,
        ).pack(side="right")

        # ── Log window ────────────────────────────────────────────────────────
        log_frame = tk.Frame(self, bg=_BG)
        log_frame.pack(fill="both", expand=True, padx=12, pady=(4, 12))

        self._log = tk.Text(
            log_frame, bg=_LOG_BG, fg=_LOG_FG,
            font=_FONT_MONO, relief="flat",
            state="disabled", wrap="word",
        )
        self._log.tag_config("info",  foreground=_LOG_INFO)
        self._log.tag_config("error", foreground=_LOG_ERR)
        self._log.tag_config("done",  foreground="#b5cea8", font=_FONT_MONO_B)
        self._log.tag_config("tier1", foreground=_LOG_TIER1, font=_FONT_MONO_B)
        self._log.tag_config("tier2", foreground=_LOG_TIER2, font=_FONT_MONO_B)
        self._log.tag_config("tier3", foreground=_LOG_TIER3)

        scroll = ttk.Scrollbar(log_frame, command=self._log.yview)
        self._log["yscrollcommand"] = scroll.set
        scroll.pack(side="right", fill="y")
        self._log.pack(side="left", fill="both", expand=True)

        self._log_write(
            "Ready.  Select a COMTRADE file (.cfg / .cff) or folder and click Run Analysis.\n"
        )

    # ── Feeder section toggle ─────────────────────────────────────────────────

    def _on_feeder_toggle(self):
        state = "normal" if self._feeder_var.get() else "disabled"
        for entry in (self._feeder_z_entry, self._source_kv_entry, self._hif_entry):
            entry.config(state=state)

    # ── Report details collapse / expand ──────────────────────────────────────

    def _toggle_details(self):
        if self._details_open.get():
            self._details_frame.pack_forget()
            self._details_open.set(False)
            self._det_toggle_btn.config(text="▶  Report Details (engineer sign-off…)")
        else:
            self._details_frame.pack(fill="x", before=self._sep_before_run)
            self._details_open.set(True)
            self._det_toggle_btn.config(text="▼  Report Details (engineer sign-off…)")

    # ── File browser ──────────────────────────────────────────────────────────

    def _browse_file(self):
        path = filedialog.askopenfilename(
            title="Select COMTRADE event file",
            filetypes=[
                ("COMTRADE files", "*.cfg *.cff *.CFG *.CFF"),
                ("CFG files", "*.cfg *.CFG"),
                ("CFF files", "*.cff *.CFF"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._file_var.set(path)

    def _browse_folder(self):
        path = filedialog.askdirectory(title="Select folder containing COMTRADE files")
        if path:
            self._file_var.set(path)

    def _open_folder(self):
        folder = _SCRIPT / "ct_output"
        folder.mkdir(exist_ok=True)
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])

    # ── Run ───────────────────────────────────────────────────────────────────

    def _run(self):
        if self._running:
            return
        if not _MODULES_OK:
            messagebox.showerror(
                "Import Error",
                "Analysis modules failed to load.\nCheck import_error.log for details.",
            )
            return

        path = self._file_var.get().strip()
        if not path:
            messagebox.showerror("Missing file",
                                 "Please select a COMTRADE file or folder first.")
            return
        if not Path(path).exists():
            messagebox.showerror("Not found", f"Cannot find:\n{path}")
            return

        feeder_params = None
        if self._feeder_var.get():
            try:
                feeder_z = float(self._feeder_z_var.get())
            except ValueError:
                messagebox.showerror("Invalid input",
                                     "Feeder Z must be a number (e.g. 0.4).")
                return
            try:
                source_kv = float(self._source_kv_var.get())
            except ValueError:
                messagebox.showerror("Invalid input",
                                     "Source Voltage must be a number (e.g. 12.47).")
                return
            try:
                hif_thresh = float(self._hif_thresh_var.get())
            except ValueError:
                messagebox.showerror("Invalid input",
                                     "HIF Threshold must be a number (e.g. 50).")
                return
            feeder_params = {
                "feeder_z":   feeder_z,
                "source_kv":  source_kv,
                "hif_thresh": hif_thresh,
            }

        params = {
            "path":               path,
            "location":           self._location_var.get().strip(),
            "device_type":        self._device_var.get().strip(),
            "engineer":           self._eng_name_var.get().strip(),
            "engineer_title":     self._eng_title_var.get().strip(),
            "engineer_contact":   self._eng_contact_var.get().strip(),
            "save_plots":         self._save_plots_var.get(),
            "phasor_plot":        self._phasor_plot_var.get(),
            "rms_plot":           self._rms_plot_var.get(),
            "seq_plot":           self._seq_plot_var.get(),
            "csv":                self._csv_var.get(),
            "json_export":        self._json_var.get(),
            "feeder":             feeder_params,
        }

        self._log_clear()
        self._run_btn.config(state="disabled", text="Running…")
        self._open_btn.config(state="disabled")
        self._running = True

        threading.Thread(target=self._run_worker, args=(params,), daemon=True).start()

    def _run_worker(self, params):
        handler = _GUILogHandler(self._log, self.after)
        handler.setFormatter(_logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        root_log = _logging.getLogger()
        root_log.addHandler(handler)
        try:
            self._do_analysis(params)
        except Exception as exc:
            import traceback
            self._log_write(
                f"\nUnexpected error: {exc}\n{traceback.format_exc()}\n", tag="error"
            )
        finally:
            root_log.removeHandler(handler)
            self.after(0, self._reset_run_btn)

    def _do_analysis(self, params):
        import glob

        path   = params["path"]
        outdir = _SCRIPT / "ct_output"
        outdir.mkdir(exist_ok=True)

        # Discover files
        if Path(path).is_file():
            files = [path]
        else:
            files = []
            for pattern in ("*.cfg", "*.CFG", "*.cff", "*.CFF"):
                files.extend(glob.glob(str(Path(path) / pattern)))
                files.extend(
                    glob.glob(str(Path(path) / "**" / pattern), recursive=True)
                )
            files = sorted(set(files))

        if not files:
            self._log_write(
                "No COMTRADE files (.cfg / .cff) found.\n", tag="error"
            )
            return

        self._log_write(f"Found {len(files)} file(s).\n")

        last_stem = None
        for filepath in files:
            self._log_write(f"\n── {Path(filepath).name} ──\n")
            try:
                self._process_one(filepath, params, outdir)
                last_stem = Path(filepath).stem
            except Exception as exc:
                import traceback
                self._log_write(
                    f"ERROR: {exc}\n{traceback.format_exc()}\n", tag="error"
                )

        self._log_write("\nDone.  Outputs saved to ct_output/\n", tag="done")
        self.after(0, lambda: self._open_btn.config(state="normal"))

        # Auto-open the Word report for single-file runs
        if len(files) == 1 and last_stem:
            rpt = outdir / f"{last_stem}_report.docx"
            if rpt.exists():
                self._open_doc(str(rpt))

    def _process_one(self, filepath, params, outdir):
        import csv, json
        import numpy as np

        stem = Path(filepath).stem

        # Parse
        record = COMTRADEParser().parse(filepath)
        self._log_write(
            f"  Loaded: {len(record.analog_channels)} analog, "
            f"{len(record.digital_channels)} digital, "
            f"{len(record.time)} samples @ {record.sample_rate:.0f} Hz\n",
            tag="info",
        )

        # Event summary
        summary = compute_event_summary(record)
        summary["trigger_time"] = record.trigger_time

        ft = summary.get("fault_type", "UNKNOWN")
        if ft != "UNKNOWN":
            self._log_write(f"  Fault type: {ft}\n")

        fi_s = summary.get("fault_inception_s")
        ti_s = summary.get("trip_time_s")
        if fi_s is not None:
            self._log_write(f"  Fault inception: {fi_s * 1000:.2f} ms\n")
        if ti_s is not None:
            td = summary.get("trip_delay_ms", 0)
            self._log_write(f"  Trip time: {ti_s * 1000:.2f} ms  ({td:.1f} ms operate)\n")

        # Plots
        save_base    = str(outdir / stem)
        waveform_png = None
        phasor_png   = None

        if params["save_plots"]:
            png_path = save_base + "_waveform.png"
            plot_event(record, save_path=png_path)
            if Path(png_path).exists():
                waveform_png = png_path
                self._log_write(f"  Waveform PNG → {stem}_waveform.png\n")

            if params["phasor_plot"]:
                ph_path = save_base + "_phasors.png"
                plot_phasors(record, save_path=ph_path)
                if Path(ph_path).exists():
                    phasor_png = ph_path
                    self._log_write(f"  Phasor PNG   → {stem}_phasors.png\n")

            if params["rms_plot"]:
                plot_rms_currents(record, save_path=save_base + "_rms.png")
                self._log_write(f"  RMS PNG      → {stem}_rms.png\n")

            if params["seq_plot"]:
                plot_sequence_components(record, save_path=save_base + "_seq.png")
                self._log_write(f"  Sequence PNG → {stem}_seq.png\n")

        # CSV
        if params["csv"]:
            csv_path = str(outdir / (stem + "_data.csv"))
            a_names = list(record.analog_channels.keys())
            d_names = list(record.digital_channels.keys())
            with open(csv_path, "w", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(["time"] + a_names + d_names)
                for i in range(len(record.time)):
                    row = [f"{record.time[i]:.9f}"]
                    for n in a_names:
                        row.append(f"{record.analog_channels[n][i]:.6g}")
                    for n in d_names:
                        row.append(str(int(record.digital_channels[n][i])))
                    writer.writerow(row)
            self._log_write(f"  CSV          → {stem}_data.csv\n")

        # JSON
        if params["json_export"]:
            def _serial(v):
                if isinstance(v, (np.floating, np.integer)):
                    return float(v)
                return str(v)
            json_path = str(outdir / (stem + "_summary.json"))
            with open(json_path, "w") as fh:
                json.dump(summary, fh, indent=2, default=_serial)
            self._log_write(f"  JSON         → {stem}_summary.json\n")

        # Feeder / recloser analysis
        feeder_data = None
        if params["feeder"]:
            fp = params["feeder"]
            feeder_data = compute_feeder_summary(
                record,
                feeder_impedance_ohm_per_mile=fp["feeder_z"],
                source_voltage_ll_kv=fp["source_kv"],
                hif_threshold_a=fp["hif_thresh"],
            )
            seq = feeder_data.get("reclose_sequence")
            if seq is not None and seq.total_shots > 0:
                outcome = "LOCKOUT" if seq.locked_out else "no lockout"
                self._log_write(
                    f"  Reclose: {seq.total_shots} shot(s) → {outcome}\n",
                    tag="info",
                )
            loc = feeder_data.get("fault_location")
            if loc and loc.get("estimated_miles") is not None:
                self._log_write(
                    f"  Fault location: ~{loc['estimated_miles']:.1f} miles, "
                    f"{loc.get('i_fault_rms_a', 0):.0f} A rms\n"
                )
            hif = feeder_data.get("hif_screen", {})
            if hif.get("hif_suspect"):
                self._log_write("  WARNING: HIF suspect (low fault current)\n", tag="error")

        # Triage
        triage = triage_event(summary, feeder_data=feeder_data)
        priority = triage["priority"]
        p_tag = {1: "tier1", 2: "tier2", 3: "tier3"}[priority]
        self._log_write(f"\n  {triage['summary_line']}\n", tag=p_tag)
        for note in triage["notes"]:
            self._log_write(f"    {note}\n", tag=p_tag)

        # Word report (always generated)
        report_data = generate_report(record)
        out_path = generate_word_report(
            record,
            report_data,
            outdir=outdir,
            stem=stem,
            location=params["location"],
            device_type=params["device_type"],
            engineer_name=params["engineer"],
            engineer_title=params["engineer_title"],
            engineer_contact=params["engineer_contact"],
            waveform_png=waveform_png,
            phasor_png=phasor_png,
            feeder=feeder_data,
            triage=triage,
        )
        if out_path:
            self._log_write(f"  DOCX         → {Path(out_path).name}\n", tag="info")

    def _open_doc(self, path: str):
        def _do():
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif sys.platform == "win32":
                subprocess.Popen(["start", "", path], shell=True)
            else:
                subprocess.Popen(["xdg-open", path])
        self.after(0, _do)

    def _reset_run_btn(self):
        self._run_btn.config(state="normal", text="Run Analysis")
        self._open_btn.config(state="normal")
        self._running = False

    # ── Help window ───────────────────────────────────────────────────────────

    def _show_help(self):
        win = tk.Toplevel(self)
        win.title("COMTRADE Analyzer — Help")
        win.configure(bg=_BG)
        win.resizable(True, True)
        win.minsize(600, 500)

        hdr = tk.Frame(win, bg=_BTN_RUN)
        hdr.pack(fill="x")
        tk.Label(hdr, text="COMTRADE Relay Event Analyzer — Reference",
                 bg=_BTN_RUN, fg="white", font=_FONT_UI_B,
                 pady=10, padx=16).pack(anchor="w")

        outer = tk.Frame(win, bg=_BG)
        outer.pack(fill="both", expand=True, padx=16, pady=10)

        txt = tk.Text(outer, bg=_BG, fg=_LABEL_FG, font=_FONT_UI,
                      relief="flat", wrap="word", cursor="arrow",
                      state="normal", padx=6, pady=4)
        sb = ttk.Scrollbar(outer, command=txt.yview)
        txt["yscrollcommand"] = sb.set
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

        _f0, _fs = _FONT_UI[0], _FONT_UI[1]
        txt.tag_config("h1",   font=(_f0, _fs, "bold"), foreground=_BTN_RUN,
                       spacing1=10, spacing3=2)
        txt.tag_config("rule", font=(_f0, 8), foreground="#cccccc")
        txt.tag_config("h2",   font=(_f0, _fs, "bold"), foreground="#333333",
                       spacing1=8, spacing3=1, lmargin1=12, lmargin2=12)
        txt.tag_config("body", font=_FONT_UI, foreground="#555555",
                       lmargin1=24, lmargin2=24, spacing3=2)

        def section(title):
            txt.insert("end", f"\n{title}\n", "h1")
            txt.insert("end", "─" * 60 + "\n", "rule")

        def concept(title, body_text):
            txt.insert("end", f"  {title}\n", "h2")
            for line in body_text.splitlines():
                txt.insert("end", f"  {line}\n", "body")
            txt.insert("end", "\n")

        section("Getting Started")

        concept("File Selection",
                "Browse for a single .cfg or .cff file, or select a folder\n"
                "to batch-process all COMTRADE files within it.\n"
                "\n"
                "COMTRADE files come as a .cfg + .dat pair or as a single\n"
                "combined .cff file.  Select the .cfg or .cff — the parser\n"
                "locates the matching .dat automatically.")

        concept("Location & Device Type",
                "Optional text that appears in the Word report header.\n"
                "  Location    — substation, feeder, or site name\n"
                "  Device Type — relay make/model (e.g. SEL-351R, GE D60)")

        concept("Report Details",
                "Expand this section to fill in the engineer sign-off block\n"
                "at the end of the Word report:\n"
                "  Name, Title, Contact (phone / email)")

        section("Feeder / Recloser Analysis")

        concept("Enable Feeder Analysis",
                "Check this box for events from distribution feeder relays\n"
                "or reclosers.  Adds three sections to the Word report:\n"
                "\n"
                "  Multi-shot reclose sequence\n"
                "    Detects each FAST/SLOW shot from 52A aux contact or TRIP\n"
                "    digital.  Reports operate time and dead time per shot.\n"
                "    Classifies outcome as TEMPORARY (cleared on reclose) or\n"
                "    PERMANENT (lockout after all shots).\n"
                "\n"
                "  Fault location estimate\n"
                "    Uses Z = V_fault / I_fault impedance method.  Requires\n"
                "    voltage channels and Feeder Z (Ω/mile) to convert to\n"
                "    distance in miles.\n"
                "\n"
                "  High-Impedance Fault (HIF) screen\n"
                "    Flags events where fault current rise < HIF Threshold.")

        concept("Feeder Z (Ω/mile)",
                "Positive-sequence impedance magnitude of the line conductor.\n"
                "Typical values:\n"
                "  Overhead conductor   0.3–0.5 Ω/mile\n"
                "  Underground cable    0.1–0.25 Ω/mile\n"
                "\n"
                "Default 0.4 Ω/mile is a reasonable starting point for OH.")

        concept("Source Voltage (kV L-L)",
                "Nominal line-to-line system voltage.  Used when voltage\n"
                "channels are absent from the record.\n"
                "Common values: 4.16, 12.47, 13.8, 24.9, 34.5 kV")

        concept("HIF Threshold (A)",
                "Flag as HIF suspect when the fault current rise is less than\n"
                "this value.  Default 50 A is appropriate for lightly loaded\n"
                "feeders.  Raise to 100–200 A on heavily loaded circuits to\n"
                "avoid false positives from normal load variation.")

        section("Options")

        concept("Save Plots",
                "Saves the waveform plot as a PNG and embeds it in the Word\n"
                "report.  Recommended — the waveform is the most valuable\n"
                "element of any relay event report.")

        concept("RMS Plot",
                "Additional sliding-window RMS current overlay plot.\n"
                "Useful for visualizing fault magnitude and recovery profile.")

        concept("Seq Plot",
                "Symmetrical-sequence component plot (I0, I1, I2).\n"
                "Useful for fault type confirmation and post-fault analysis.")

        concept("CSV",
                "Exports all channel data (time, analog, digital) as a flat\n"
                "CSV file for Excel or MATLAB post-processing.")

        concept("JSON",
                "Exports the event summary (fault inception, trip time, peak\n"
                "currents and voltages, fault type) as JSON.")

        section("Output Files")

        concept("Output folder: ct_output/",
                "All outputs are written to ct_output/ next to app.py.\n"
                "Files are named from the input file stem:\n"
                "  <stem>_waveform.png — waveform plot (currents, voltages, digital)\n"
                "  <stem>_rms.png      — sliding RMS currents (if enabled)\n"
                "  <stem>_seq.png      — sequence components  (if enabled)\n"
                "  <stem>_data.csv     — channel data          (if enabled)\n"
                "  <stem>_summary.json — event summary         (if enabled)\n"
                "  <stem>_report.docx  — Word event report     (always)")

        section("Standards Reference")

        concept("IEEE C37.111 — COMTRADE",
                "The IEEE Standard for Common Format for Transient Data Exchange\n"
                "defines the .cfg and .dat (or combined .cff) file format used\n"
                "by relay manufacturers to export oscillography records.\n"
                "\n"
                "Revisions supported: 1991, 1999, and 2013.\n"
                "DAT formats supported: ASCII and binary (16-bit integer).")

        concept("Fault Type Classification",
                "The analyzer classifies fault type from post-fault phase RMS\n"
                "ratios using symmetrical-component theory:\n"
                "\n"
                "  SLG — Single Line-to-Ground  (one phase suppressed, I0 >> 0)\n"
                "  LL  — Line-to-Line           (two phases depressed, no I0)\n"
                "  LLG — Double Line-to-Ground  (two phases depressed, I0 > 0)\n"
                "  3PH — Three-Phase            (all phases equally depressed)")

        concept("FAST vs SLOW Element Discrimination",
                "Shot type is determined from operate time:\n"
                "  FAST  — operate time ≤ 2 cycles  (50P instantaneous element)\n"
                "  SLOW  — operate time >  2 cycles  (51P time-overcurrent element)\n"
                "\n"
                "Typical FAST: 8–16 ms at 60 Hz.  Typical SLOW on CO-8 curve\n"
                "at 7× pickup: 200–400 ms.")

        txt.config(state="disabled")

        tk.Button(win, text="Close", command=win.destroy,
                  font=_FONT_UI, relief="flat", padx=20, pady=6,
                  bg="#dddddd", cursor="hand2").pack(pady=(0, 14))

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _log_write(self, text, tag=None):
        def _write():
            self._log.config(state="normal")
            if tag:
                self._log.insert("end", text, tag)
            else:
                self._log.insert("end", text)
            self._log.see("end")
            self._log.config(state="disabled")
        self.after(0, _write)

    def _log_clear(self):
        self._log.config(state="normal")
        self._log.delete("1.0", "end")
        self._log.config(state="disabled")


if __name__ == "__main__":
    app = COMTRADEApp()
    app.mainloop()
