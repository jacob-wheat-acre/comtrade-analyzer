"""
generate_test_recloser.py — Synthetic multi-shot recloser event for testing.

Simulates a FAST-SLOW-LOCKOUT sequence on a 12.47 kV distribution feeder:

  0 – 50 ms   : pre-fault (balanced load, 80 A peak)
  50 ms        : A-phase to ground fault (IA → 600 A, VAN collapses)
  66 ms        : TRIP on 50P (instantaneous, Shot 1 — FAST, 16 ms operate time)
  66 – 566 ms : Dead time 1 (500 ms open interval)
  566 ms       : Reclose attempt 1 — fault still present (permanent fault)
  570 ms       : 51P picks up again
  910 ms       : TRIP on 51P (Shot 2 — SLOW, ~344 ms operate time on CO-8 curve)
  910 – 2910 ms: Dead time 2 (2000 ms open interval)
  2910 ms      : Reclose attempt 2 — fault still present → LOCKOUT

Total record: ~3.2 s at 1920 Hz sample rate (61,440 samples)

Files written to ./test_recloser/ by default.
"""

import os
import numpy as np

# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

FS          = 1920         # sample rate, Hz
F0          = 60           # power frequency, Hz
T_TOTAL     = 3.200        # total duration, s

I_LOAD      = 80.0         # pre-fault peak load current, A
I_FAULT_A   = 600.0        # faulted phase-A peak (first shot), A
I_FAULT_BC  = 100.0        # unfaulted phases during SLG, A peak
V_PEAK      = 10_180.0     # 12.47 kV L-L → 7200 V L-N RMS × √2 ≈ 10,180 V

# Shot timings
T_FAULT1    = 0.050        # first fault inception, s
T_TRIP1     = 0.066        # first trip (50P, 16 ms), s
T_RECLOSE1  = 0.566        # first reclose (500 ms dead time), s

T_FAULT2    = 0.570        # second fault (fault still present), s
T_TRIP2     = 0.910        # second trip (51P, 340 ms on CO-8 curve at 7.5× pickup)
T_RECLOSE2  = 2.910        # second reclose attempt (2000 ms dead time)

T_FAULT3    = 2.914        # third fault inception (reclose into fault)
T_LOCKOUT   = 2.930        # lockout asserted (~16 ms — another 50P operation)

# Scaling
A_CURRENT   = I_FAULT_A * 1.2 / 32767
A_VOLTAGE   = V_PEAK * 1.1 / 32767
TAU_DC      = 10.0 / (2 * np.pi * F0)


def _phase(t, theta, amp):
    return amp * np.sin(2 * np.pi * F0 * t + theta)


def generate():
    n_samples = int(FS * T_TOTAL)
    t         = np.arange(n_samples) / FS
    omega     = 2 * np.pi * F0
    tha, thb, thc = 0.0, -2 * np.pi / 3, +2 * np.pi / 3

    ia  = np.zeros(n_samples)
    ib  = np.zeros(n_samples)
    ic  = np.zeros(n_samples)
    van = np.zeros(n_samples)
    vbn = np.zeros(n_samples)
    vcn = np.zeros(n_samples)
    _in = np.zeros(n_samples)

    for i, ti in enumerate(t):
        if ti < T_FAULT1:
            # --- Pre-fault balanced load ---
            ia[i], ib[i], ic[i] = (_phase(ti, th, I_LOAD) for th in (tha, thb, thc))
            van[i], vbn[i], vcn[i] = (_phase(ti, th, V_PEAK) for th in (tha, thb, thc))

        elif ti < T_TRIP1:
            # --- Shot 1 fault ---
            dt = ti - T_FAULT1
            dc = I_FAULT_A * 0.7 * np.exp(-dt / TAU_DC)
            ia[i]  = I_FAULT_A * np.sin(omega * ti + tha) + dc
            ib[i]  = I_FAULT_BC * np.sin(omega * ti + thb)
            ic[i]  = I_FAULT_BC * np.sin(omega * ti + thc)
            van[i] = V_PEAK * 0.10 * np.sin(omega * ti + tha)
            vbn[i] = V_PEAK * 1.08 * np.sin(omega * ti + thb)
            vcn[i] = V_PEAK * 1.08 * np.sin(omega * ti + thc)

        elif ti < T_RECLOSE1:
            # --- Dead time 1 (breaker open) ---
            ia[i] = ib[i] = ic[i] = 0.0
            van[i] = vbn[i] = vcn[i] = 0.0

        elif ti < T_FAULT2:
            # --- Post-reclose 1, brief healthy interval ---
            ia[i], ib[i], ic[i] = (_phase(ti, th, I_LOAD) for th in (tha, thb, thc))
            van[i], vbn[i], vcn[i] = (_phase(ti, th, V_PEAK) for th in (tha, thb, thc))

        elif ti < T_TRIP2:
            # --- Shot 2 fault (same SLG, fault still present) ---
            dt = ti - T_FAULT2
            ia[i]  = I_FAULT_A * 0.85 * np.sin(omega * ti + tha)
            ib[i]  = I_FAULT_BC * np.sin(omega * ti + thb)
            ic[i]  = I_FAULT_BC * np.sin(omega * ti + thc)
            van[i] = V_PEAK * 0.12 * np.sin(omega * ti + tha)
            vbn[i] = V_PEAK * 1.06 * np.sin(omega * ti + thb)
            vcn[i] = V_PEAK * 1.06 * np.sin(omega * ti + thc)

        elif ti < T_RECLOSE2:
            # --- Dead time 2 ---
            ia[i] = ib[i] = ic[i] = 0.0
            van[i] = vbn[i] = vcn[i] = 0.0

        elif ti < T_FAULT3:
            # --- Post-reclose 2, very brief ---
            ia[i], ib[i], ic[i] = (_phase(ti, th, I_LOAD) for th in (tha, thb, thc))
            van[i], vbn[i], vcn[i] = (_phase(ti, th, V_PEAK) for th in (tha, thb, thc))

        elif ti < T_LOCKOUT:
            # --- Shot 3 fault → lockout ---
            ia[i]  = I_FAULT_A * 0.9 * np.sin(omega * ti + tha)
            ib[i]  = I_FAULT_BC * np.sin(omega * ti + thb)
            ic[i]  = I_FAULT_BC * np.sin(omega * ti + thc)
            van[i] = V_PEAK * 0.08 * np.sin(omega * ti + tha)
            vbn[i] = V_PEAK * 1.07 * np.sin(omega * ti + thb)
            vcn[i] = V_PEAK * 1.07 * np.sin(omega * ti + thc)

        else:
            # --- Post-lockout (all dead) ---
            ia[i] = ib[i] = ic[i] = 0.0
            van[i] = vbn[i] = vcn[i] = 0.0

    _in = ia + ib + ic

    # ---- Digital channels ----
    trip_sig = ((t >= T_TRIP1) & (t < T_RECLOSE1) |
                (t >= T_TRIP2) & (t < T_RECLOSE2) |
                (t >= T_LOCKOUT)).astype(int)

    p50p_sig = ((t >= T_FAULT1) & (t < T_TRIP1) |
                (t >= T_FAULT3)).astype(int)

    p51p_sig = ((t >= T_FAULT2) & (t < T_TRIP2)).astype(int)

    p79_sig  = ((t >= T_RECLOSE1) & (t < T_RECLOSE1 + 0.010) |
                (t >= T_RECLOSE2) & (t < T_RECLOSE2 + 0.010)).astype(int)

    lock_sig = (t >= T_LOCKOUT).astype(int)

    # 52A: breaker closed = 1; open = 0
    a52_sig = (1 - ((t >= T_TRIP1) & (t < T_RECLOSE1) |
                    (t >= T_TRIP2) & (t < T_RECLOSE2) |
                    (t >= T_LOCKOUT)).astype(int))

    # Scale analog → raw integers
    raw_ia  = np.clip(np.round(ia  / A_CURRENT), -32767, 32767).astype(int)
    raw_ib  = np.clip(np.round(ib  / A_CURRENT), -32767, 32767).astype(int)
    raw_ic  = np.clip(np.round(ic  / A_CURRENT), -32767, 32767).astype(int)
    raw_in  = np.clip(np.round(_in / A_CURRENT), -32767, 32767).astype(int)
    raw_van = np.clip(np.round(van / A_VOLTAGE), -32767, 32767).astype(int)
    raw_vbn = np.clip(np.round(vbn / A_VOLTAGE), -32767, 32767).astype(int)
    raw_vcn = np.clip(np.round(vcn / A_VOLTAGE), -32767, 32767).astype(int)

    timestamps_us = (t * 1e6).astype(int)

    analog_raw  = [raw_ia, raw_ib, raw_ic, raw_in, raw_van, raw_vbn, raw_vcn]
    digital_raw = [trip_sig, p50p_sig, p51p_sig, p79_sig, a52_sig, lock_sig]

    return analog_raw, digital_raw, timestamps_us, n_samples


def write_cfg(out_dir: str):
    n_samples = int(FS * T_TOTAL)
    cfg_lines = [
        "Maple Ave Feeder,SEL-351R-Recloser,1999",
        "13,7A,6D",
        f"1,IA,A,,A,{A_CURRENT:.8f},0.0,0,-32767,32767,600,5,P",
        f"2,IB,B,,A,{A_CURRENT:.8f},0.0,0,-32767,32767,600,5,P",
        f"3,IC,C,,A,{A_CURRENT:.8f},0.0,0,-32767,32767,600,5,P",
        f"4,IN,N,,A,{A_CURRENT:.8f},0.0,0,-32767,32767,600,5,P",
        f"5,VAN,A,,V,{A_VOLTAGE:.8f},0.0,0,-32767,32767,10180,1,P",
        f"6,VBN,B,,V,{A_VOLTAGE:.8f},0.0,0,-32767,32767,10180,1,P",
        f"7,VCN,C,,V,{A_VOLTAGE:.8f},0.0,0,-32767,32767,10180,1,P",
        "1,TRIP,A,,0",
        "2,50P,A,,0",
        "3,51P,A,,0",
        "4,79,A,,0",
        "5,52A,A,,1",
        "6,LOCK,A,,0",
        "60",
        "1",
        f"{FS},{n_samples}",
        "01/06/2026,14:22:15.000000",
        "01/06/2026,14:22:15.050000",
        "ASCII",
        "1",
    ]
    cfg_path = os.path.join(out_dir, "test_recloser.cfg")
    with open(cfg_path, "w") as fh:
        fh.write("\n".join(cfg_lines) + "\n")
    print(f"Written: {cfg_path}")


def write_dat(out_dir, analog_raw, digital_raw, timestamps_us, n_samples):
    dat_path = os.path.join(out_dir, "test_recloser.dat")
    with open(dat_path, "w") as fh:
        for i in range(n_samples):
            row = [str(i + 1), str(int(timestamps_us[i]))]
            row += [str(int(ch[i])) for ch in analog_raw]
            row += [str(int(ch[i])) for ch in digital_raw]
            fh.write(",".join(row) + "\r\n")
    print(f"Written: {dat_path}")


def main():
    out_dir = os.path.join(os.path.dirname(__file__), "test_recloser")
    os.makedirs(out_dir, exist_ok=True)
    analog_raw, digital_raw, timestamps_us, n_samples = generate()
    write_cfg(out_dir)
    write_dat(out_dir, analog_raw, digital_raw, timestamps_us, n_samples)
    print()
    print("Test recloser data generated.  Run:")
    print(f"  python3 main.py {out_dir}/test_recloser.cfg --feeder --feeder-z 0.4 --source-kv 12.47 --report")


if __name__ == "__main__":
    main()
