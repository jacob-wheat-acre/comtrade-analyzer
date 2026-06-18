#!/usr/bin/env python3
"""
generate_test_3ph.py — Synthetic three-phase balanced fault on a 12.47 kV feeder.

Physics
-------
A balanced three-phase fault is the most severe but least common fault type.
All three phases are equally involved; the fault is symmetric.

  I0 = 0  (no ground return path in a bolted 3PH fault)
  I2 = 0  (symmetry → no negative-sequence current)
  I1 = fault current / √3  (all current is positive-sequence)

Voltage: all three phases collapse toward zero.  In a bolted 3PH fault at the
bus the voltages go to zero; for a fault partway down the feeder the bus voltage
dips proportionally.  Here we simulate a fault at ~2 miles (Va/Vb/Vc collapse
to 15% of normal, representing a stiff source and high fault current).

Key classifier signature
------------------------
  Ia, Ib, Ic : all elevated ~15×, 120° apart (symmetric)
  I0 : ≈ 0
  I2 : ≈ 0
  Va, Vb, Vc : all collapse to ~15% of normal

Sequence:  50P trip in ~1 cycle.  No reclose.

Record: 200 ms at 1920 Hz
"""

import os
import numpy as np

FS        = 1920
F0        = 60
T_TOTAL   = 0.200

I_LOAD    = 80.0
I_FAULT   = 1_400.0   # 3PH fault peak  (RMS ≈ 990 A — typical close-in 3PH)
V_PEAK    = 10_180.0

T_FAULT   = 0.050
T_TRIP    = 0.067     # 50P, ~1 cycle

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
        pa = w * ti + tha
        pb = w * ti + thb
        pc = w * ti + thc

        if ti < T_FAULT:
            ia[i]  = I_LOAD  * np.sin(pa)
            ib[i]  = I_LOAD  * np.sin(pb)
            ic[i]  = I_LOAD  * np.sin(pc)
            van[i] = V_PEAK  * np.sin(pa)
            vbn[i] = V_PEAK  * np.sin(pb)
            vcn[i] = V_PEAK  * np.sin(pc)

        elif ti < T_TRIP:
            dt = ti - T_FAULT
            # Balanced fault currents with maximum DC offset on phase A
            # (worst-case scenario for breaker interruption — point-on-wave at Va=0)
            dc_a = I_FAULT * 1.0 * np.exp(-dt / TAU_DC)   # maximum DC offset
            dc_b = I_FAULT * 0.5 * np.exp(-dt / TAU_DC)   # partial offset on B
            dc_c = 0.0                                      # C-phase near zero at inception
            ia[i]  = I_FAULT * np.sin(pa) + dc_a
            ib[i]  = I_FAULT * np.sin(pb) + dc_b
            ic[i]  = I_FAULT * np.sin(pc) + dc_c
            # All three voltages collapse (bolted 3PH → ~15% residual from feeder Z)
            van[i] = V_PEAK * 0.15 * np.sin(pa)
            vbn[i] = V_PEAK * 0.15 * np.sin(pb)
            vcn[i] = V_PEAK * 0.15 * np.sin(pc)

        else:
            ia[i] = ib[i] = ic[i] = 0.0
            van[i] = vbn[i] = vcn[i] = 0.0

    _in = ia + ib + ic  # ≈ 0 for balanced 3PH fault (no ground current)

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
        "Maple Ave Feeder,SEL-351-3PH-Test,1999",
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
        "01/06/2026,14:35:00.000000",
        "01/06/2026,14:35:00.050000",
        "ASCII",
        "1",
    ]
    path = os.path.join(out_dir, "test_3ph.cfg")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"Written: {path}")


def write_dat(out_dir, analog_raw, digital_raw, ts_us, n):
    path = os.path.join(out_dir, "test_3ph.dat")
    with open(path, "w") as fh:
        for i in range(n):
            row = [str(i + 1), str(int(ts_us[i]))]
            row += [str(int(ch[i])) for ch in analog_raw]
            row += [str(int(ch[i])) for ch in digital_raw]
            fh.write(",".join(row) + "\r\n")
    print(f"Written: {path}")


def main():
    out_dir = os.path.join(os.path.dirname(__file__), "test_3ph")
    os.makedirs(out_dir, exist_ok=True)
    analog_raw, digital_raw, ts_us, n = generate()
    write_cfg(out_dir)
    write_dat(out_dir, analog_raw, digital_raw, ts_us, n)
    print()
    print("Run:")
    print(f"  python3 main.py {out_dir}/test_3ph.cfg --report --phasor-plot --save-plots")


if __name__ == "__main__":
    main()
