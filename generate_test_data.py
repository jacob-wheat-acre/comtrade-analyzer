"""
generate_test_data.py — Synthetic COMTRADE event generator for testing.

Creates a realistic Phase-A-to-Ground fault scenario:
  - 0 – 50 ms  : pre-fault (balanced 3-phase, 100 A peak, 6.9 kV peak)
  - 50 – 130 ms : fault (IA → 800 A, VAN collapses, DC offset on IA)
  - 130 ms      : TRIP asserted; breaker opens; currents decay to zero
  - 130 – 200 ms: post-fault

Sample rate : 1 920 Hz  (32 samples/cycle at 60 Hz)
Trigger     : 50 ms (fault inception)

Files written to ./test_event/ by default.
"""

import os
import numpy as np

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

FS          = 1920          # sample rate, Hz
F0          = 60            # power frequency, Hz
T_TOTAL     = 0.200         # total duration, s
T_FAULT     = 0.050         # fault inception time, s
T_TRIP      = 0.130         # TRIP assertion time, s
T_TRIGGER   = T_FAULT       # relay trigger coincides with fault

I_PREFAULT  = 100.0         # pre-fault peak phase current, A
I_FAULT_A   = 800.0         # faulted phase-A peak current, A
I_FAULT_BC  = 140.0         # feeding phases during SLG fault, A peak
V_PEAK      = 6_928.0       # pre-fault phase-voltage peak ≈ 4.8 kV rms × √2

# CFG scaling: value = a * raw + b
# Choose 'a' so that max engineering value fits comfortably in int16
A_CURRENT   = I_FAULT_A * 1.2 / 32767   # ~0.02926 A / LSB
A_VOLTAGE   = V_PEAK * 1.1 / 32767      # ~0.2327 V / LSB
B_FACTOR    = 0.0

# DC offset time constant (X/R ≈ 10 at 60 Hz → tau = X/R / (2π·f))
TAU_DC      = 10.0 / (2 * np.pi * F0)   # ~26.5 ms


def generate():
    n_samples  = int(FS * T_TOTAL)
    t          = np.arange(n_samples) / FS
    omega      = 2 * np.pi * F0

    # ---- Analog signals ----

    # Phase angles (A, B, C)
    theta_a = 0.0
    theta_b = -2 * np.pi / 3
    theta_c = +2 * np.pi / 3

    ia = np.zeros(n_samples)
    ib = np.zeros(n_samples)
    ic = np.zeros(n_samples)
    van = np.zeros(n_samples)
    vbn = np.zeros(n_samples)
    vcn = np.zeros(n_samples)
    iop = np.zeros(n_samples)   # ground / residual (IN = IA+IB+IC)

    for i, ti in enumerate(t):
        if ti < T_FAULT:
            # Balanced pre-fault
            ia[i]  = I_PREFAULT  * np.sin(omega * ti + theta_a)
            ib[i]  = I_PREFAULT  * np.sin(omega * ti + theta_b)
            ic[i]  = I_PREFAULT  * np.sin(omega * ti + theta_c)
            van[i] = V_PEAK      * np.sin(omega * ti + theta_a)
            vbn[i] = V_PEAK      * np.sin(omega * ti + theta_b)
            vcn[i] = V_PEAK      * np.sin(omega * ti + theta_c)

        elif ti < T_TRIP:
            # SLG fault: IA increases with DC offset; VAN collapses
            dt = ti - T_FAULT
            dc_offset = I_FAULT_A * 0.6 * np.exp(-dt / TAU_DC)  # 60 % DC
            ia[i]  = I_FAULT_A   * np.sin(omega * ti + theta_a) + dc_offset
            ib[i]  = I_FAULT_BC  * np.sin(omega * ti + theta_b)
            ic[i]  = I_FAULT_BC  * np.sin(omega * ti + theta_c)
            # VAN sags to ~15 % of normal; VBN, VCN slightly elevated
            van[i] = V_PEAK * 0.15 * np.sin(omega * ti + theta_a)
            vbn[i] = V_PEAK * 1.05 * np.sin(omega * ti + theta_b)
            vcn[i] = V_PEAK * 1.05 * np.sin(omega * ti + theta_c)

        else:
            # Post-fault: all currents zero, voltages recover
            dt = ti - T_TRIP
            ramp = min(1.0, dt / 0.02)   # 20 ms recovery ramp
            ia[i] = ib[i] = ic[i] = 0.0
            van[i] = V_PEAK * ramp * np.sin(omega * ti + theta_a)
            vbn[i] = V_PEAK * ramp * np.sin(omega * ti + theta_b)
            vcn[i] = V_PEAK * ramp * np.sin(omega * ti + theta_c)

    iop = ia + ib + ic   # residual / neutral current

    analog_eng = [ia, ib, ic, iop, van, vbn, vcn]

    # Convert engineering values → raw integers for DAT file
    a_factors = [A_CURRENT] * 4 + [A_VOLTAGE] * 3
    raw_analog = [
        np.clip(np.round(sig / a_fac), -32767, 32767).astype(int)
        for sig, a_fac in zip(analog_eng, a_factors)
    ]

    # ---- Digital signals ----
    trip_sig  = (t >= T_TRIP).astype(int)   # TRIP bit
    p51_sig   = (t >= T_FAULT).astype(int)  # 51P (time overcurrent) pick-up
    p50g_sig  = (t >= T_FAULT + 0.001).astype(int)  # 50G (inst. ground OC)
    lt_bit    = np.zeros(n_samples, dtype=int)       # LT (load encroachment)

    digital_sigs = [trip_sig, p51_sig, p50g_sig, lt_bit]

    # ---- Build timestamps (microseconds from recording start) ----
    timestamps_us = (t * 1e6).astype(int)

    # ---- Write CFG file ----
    return raw_analog, digital_sigs, timestamps_us, n_samples, a_factors


def write_cfg(out_dir: str):
    cfg_lines = [
        "Test Substation,SEL-421-Relay,1999",
        "11,7A,4D",
        "1,IA,A,,A,{a:.8f},0.0,0,-32767,32767,800,5,P".format(a=A_CURRENT),
        "2,IB,B,,A,{a:.8f},0.0,0,-32767,32767,800,5,P".format(a=A_CURRENT),
        "3,IC,C,,A,{a:.8f},0.0,0,-32767,32767,800,5,P".format(a=A_CURRENT),
        "4,IN,N,,A,{a:.8f},0.0,0,-32767,32767,800,5,P".format(a=A_CURRENT),
        "5,VAN,A,,V,{a:.8f},0.0,0,-32767,32767,6928,1,P".format(a=A_VOLTAGE),
        "6,VBN,B,,V,{a:.8f},0.0,0,-32767,32767,6928,1,P".format(a=A_VOLTAGE),
        "7,VCN,C,,V,{a:.8f},0.0,0,-32767,32767,6928,1,P".format(a=A_VOLTAGE),
        "1,TRIP,A,,0",
        "2,51P,A,,0",
        "3,50G,A,,0",
        "4,LT,,, 0",
        "60",
        "1",
        f"{FS},{int(FS * T_TOTAL)}",
        "01/01/2024,00:00:00.000000",
        f"01/01/2024,00:00:{T_TRIGGER * 1000:.0f}.{int((T_TRIGGER % 1) * 1e6):06d}",
        "ASCII",
        "1",
    ]

    # Rewrite trigger time correctly
    trig_hms = "00:00:00"
    trig_frac = f"{int(T_TRIGGER * 1e6):06d}"
    cfg_lines[14 + 3] = f"01/01/2024,{trig_hms}.{trig_frac}"

    cfg_path = os.path.join(out_dir, "test_event.cfg")
    with open(cfg_path, "w") as fh:
        fh.write("\n".join(cfg_lines) + "\n")
    print(f"Written: {cfg_path}")
    return cfg_path


def write_dat(out_dir: str, raw_analog, digital_sigs, timestamps_us, n_samples):
    dat_path = os.path.join(out_dir, "test_event.dat")
    with open(dat_path, "w") as fh:
        for i in range(n_samples):
            row_parts = [str(i + 1), str(int(timestamps_us[i]))]
            row_parts += [str(int(raw_analog[ch][i])) for ch in range(len(raw_analog))]
            row_parts += [str(int(digital_sigs[ch][i])) for ch in range(len(digital_sigs))]
            fh.write(",".join(row_parts) + "\r\n")
    print(f"Written: {dat_path}")
    return dat_path


def main():
    out_dir = os.path.join(os.path.dirname(__file__), "test_event")
    os.makedirs(out_dir, exist_ok=True)

    raw_analog, digital_sigs, timestamps_us, n_samples, _ = generate()

    write_cfg(out_dir)
    write_dat(out_dir, raw_analog, digital_sigs, timestamps_us, n_samples)

    print()
    print("Test data generated.  Run:")
    print(f"  python main.py {out_dir}/test_event.cfg")


if __name__ == "__main__":
    main()
