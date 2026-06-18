#!/usr/bin/env python3
"""
generate_test_ll.py — Synthetic A-B line-to-line fault on a 12.47 kV feeder.

Physics
-------
An LL fault has no ground return path, so zero-sequence current is absent.
The fault current circulates between phases A and B:  Ia = -Ib at the fault
point (approximately, neglecting load current on B).  Phase C remains
essentially unaffected.

Voltage response: with a grounded-wye source, both Va and Vb are pulled toward
the average of the two source voltages at the fault point.  The result is that
Va and Vb both collapse to roughly 50% L-N magnitude and converge to the same
angle, while Vc holds near normal (typically 1.05 pu due to voltage regulation
on the unfaulted phase).

Key classifier signature
------------------------
  Ia : elevated ~10× load
  Ib : elevated ~10× load, roughly 180° out of phase with Ia
  Ic : near pre-fault load level
  I0 : ≈ 0   (no ground path)
  I2 : significant (unbalanced phases → negative sequence)

Sequence:  50P trip in ~1 cycle.  No reclose.

Record: 200 ms at 1920 Hz
"""

import os
import numpy as np

FS        = 1920
F0        = 60
T_TOTAL   = 0.200   # s

I_LOAD    = 80.0    # pre-fault peak load current, A
I_FAULT   = 820.0   # LL fault current peak, A  (Ia = −Ib)
V_PEAK    = 10_180.0  # 12.47 kV L-L → ~7200 V L-N RMS × √2

T_FAULT   = 0.050   # fault inception
T_TRIP    = 0.067   # trip (50P, ~1 cycle = 16.7 ms operate time)

# A_LL fault current leads Va by ~30° (theoretical for LL with Z1=Z2)
FAULT_LEAD = np.radians(30)

A_CURRENT  = I_FAULT * 1.2 / 32767
A_VOLTAGE  = V_PEAK  * 1.1 / 32767
TAU_DC     = 10.0 / (2 * np.pi * F0)


def generate():
    n  = int(FS * T_TOTAL)
    t  = np.arange(n) / FS
    w  = 2 * np.pi * F0
    tha, thb, thc = 0.0, -2 * np.pi / 3, 2 * np.pi / 3

    ia  = np.zeros(n); ib  = np.zeros(n); ic  = np.zeros(n)
    van = np.zeros(n); vbn = np.zeros(n); vcn = np.zeros(n)

    for i, ti in enumerate(t):
        phase_a = w * ti + tha
        phase_b = w * ti + thb
        phase_c = w * ti + thc

        if ti < T_FAULT:
            ia[i]  = I_LOAD  * np.sin(phase_a)
            ib[i]  = I_LOAD  * np.sin(phase_b)
            ic[i]  = I_LOAD  * np.sin(phase_c)
            van[i] = V_PEAK  * np.sin(phase_a)
            vbn[i] = V_PEAK  * np.sin(phase_b)
            vcn[i] = V_PEAK  * np.sin(phase_c)

        elif ti < T_TRIP:
            dt = ti - T_FAULT
            # Fault current with DC offset decaying on A
            dc_a = I_FAULT * 0.6 * np.exp(-dt / TAU_DC)
            ia[i]  =  I_FAULT * np.sin(phase_a + FAULT_LEAD) + dc_a
            ib[i]  = -I_FAULT * np.sin(phase_a + FAULT_LEAD) - dc_a
            ic[i]  =  I_LOAD  * np.sin(phase_c)
            # Va and Vb pulled to a common midpoint (~50% of normal)
            van[i] = V_PEAK * 0.50 * np.sin(phase_a - np.radians(60))
            vbn[i] = V_PEAK * 0.50 * np.sin(phase_a - np.radians(60))  # same as Va
            vcn[i] = V_PEAK * 1.05 * np.sin(phase_c)

        else:
            # Post-trip — all dead
            ia[i] = ib[i] = ic[i] = 0.0
            van[i] = vbn[i] = vcn[i] = 0.0

    _in = ia + ib + ic

    trip_sig = (t >= T_TRIP).astype(int)
    p50p_sig = ((t >= T_FAULT) & (t < T_TRIP)).astype(int)

    def _raw(sig, scale):
        return np.clip(np.round(sig / scale), -32767, 32767).astype(int)

    analog_raw  = [_raw(ia, A_CURRENT), _raw(ib, A_CURRENT),
                   _raw(ic, A_CURRENT), _raw(_in, A_CURRENT),
                   _raw(van, A_VOLTAGE), _raw(vbn, A_VOLTAGE), _raw(vcn, A_VOLTAGE)]
    digital_raw = [trip_sig, p50p_sig]
    ts_us       = (t * 1e6).astype(int)

    return analog_raw, digital_raw, ts_us, n


def write_cfg(out_dir):
    n = int(FS * T_TOTAL)
    lines = [
        "Maple Ave Feeder,SEL-351-LL-Test,1999",
        "9,7A,2D",
        f"1,IA,A,,A,{A_CURRENT:.8f},0.0,0,-32767,32767,600,5,P",
        f"2,IB,B,,A,{A_CURRENT:.8f},0.0,0,-32767,32767,600,5,P",
        f"3,IC,C,,A,{A_CURRENT:.8f},0.0,0,-32767,32767,600,5,P",
        f"4,IN,N,,A,{A_CURRENT:.8f},0.0,0,-32767,32767,600,5,P",
        f"5,VAN,A,,V,{A_VOLTAGE:.8f},0.0,0,-32767,32767,10180,1,P",
        f"6,VBN,B,,V,{A_VOLTAGE:.8f},0.0,0,-32767,32767,10180,1,P",
        f"7,VCN,C,,V,{A_VOLTAGE:.8f},0.0,0,-32767,32767,10180,1,P",
        "1,TRIP,A,,0",
        "2,50P,A,,0",
        "60",
        "1",
        f"{FS},{n}",
        "01/06/2026,14:30:00.000000",
        "01/06/2026,14:30:00.050000",
        "ASCII",
        "1",
    ]
    path = os.path.join(out_dir, "test_ll.cfg")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"Written: {path}")


def write_dat(out_dir, analog_raw, digital_raw, ts_us, n):
    path = os.path.join(out_dir, "test_ll.dat")
    with open(path, "w") as fh:
        for i in range(n):
            row = [str(i + 1), str(int(ts_us[i]))]
            row += [str(int(ch[i])) for ch in analog_raw]
            row += [str(int(ch[i])) for ch in digital_raw]
            fh.write(",".join(row) + "\r\n")
    print(f"Written: {path}")


def main():
    out_dir = os.path.join(os.path.dirname(__file__), "test_ll")
    os.makedirs(out_dir, exist_ok=True)
    analog_raw, digital_raw, ts_us, n = generate()
    write_cfg(out_dir)
    write_dat(out_dir, analog_raw, digital_raw, ts_us, n)
    print()
    print("Run:")
    print(f"  python3 main.py {out_dir}/test_ll.cfg --report --phasor-plot --save-plots")


if __name__ == "__main__":
    main()
