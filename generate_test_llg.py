#!/usr/bin/env python3
"""
generate_test_llg.py — Synthetic A-B double line-to-ground fault on a 12.47 kV feeder.

Physics
-------
An LLG (double line-to-ground) fault involves two phases and the ground return.
It can be thought of as an LL fault that subsequently involves ground — or a
simultaneous SLG on two phases.

Key characteristics vs LL:
  - Ground return path exists → I0 ≠ 0
  - Fault current on faulted phases (Ia, Ib) is higher than LL because the
    ground path adds a parallel impedance (Z0 in parallel with Z2)
  - Unfaulted phase (Vc) rises above normal: "overvoltage on the unfaulted phase"
    due to neutral shift.  On an effectively grounded system (X0/X1 < 3), this
    is 1.1–1.3 pu.  On an ungrounded/resonant-grounded system it can approach
    1.73 pu (full L-L voltage on C).

Key classifier signature
------------------------
  Ia : elevated (combined SLG + LL component)
  Ib : elevated (out of phase with Ia, but not 180° — shifted by ~60°)
  Ic : near pre-fault load level
  IN : large (= Ia + Ib + Ic ≠ 0)
  I0 : significant
  I2 : significant
  Va : collapses (faulted)
  Vb : collapses (faulted)
  Vc : elevated to ~1.15 pu (neutral shift on grounded-wye 12.47 kV)

Sequence:  50P + 50G trip in ~1 cycle.  No reclose.

Record: 200 ms at 1920 Hz
"""

import os
import numpy as np

FS        = 1920
F0        = 60
T_TOTAL   = 0.200

I_LOAD    = 80.0
I_FAULT_A = 900.0   # A-phase fault current peak
I_FAULT_B = 750.0   # B-phase fault current peak (slightly less — asymmetric LLG)
V_PEAK    = 10_180.0

T_FAULT   = 0.050
T_TRIP    = 0.067   # 50P/50G trip in ~1 cycle

A_CURRENT  = max(I_FAULT_A, I_FAULT_B) * 1.2 / 32767
A_VOLTAGE  = V_PEAK * 1.3 / 32767   # extra headroom for Vc overvoltage
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
            dc_a =  I_FAULT_A * 0.7 * np.exp(-dt / TAU_DC)
            dc_b = -I_FAULT_B * 0.4 * np.exp(-dt / TAU_DC)

            # A-phase: large fault current toward ground
            ia[i] = I_FAULT_A * np.sin(pa + np.radians(15)) + dc_a

            # B-phase: fault current toward ground, shifted ~60° from A (LLG characteristic)
            # Not exactly anti-phase with A (that would be pure LL)
            ia_ang = pa + np.radians(15)
            ib[i] = I_FAULT_B * np.sin(ia_ang - np.radians(60)) + dc_b

            # C-phase: normal load — unfaulted phase
            ic[i] = I_LOAD * np.sin(pc)

            # Faulted voltages collapse; ground potential rises
            van[i] = V_PEAK * 0.08 * np.sin(pa)   # A nearly zero (bolted to ground)
            vbn[i] = V_PEAK * 0.10 * np.sin(pb)   # B nearly zero
            # Vc overvoltage — neutral shift on effectively grounded 12.47 kV
            vcn[i] = V_PEAK * 1.15 * np.sin(pc)

        else:
            ia[i] = ib[i] = ic[i] = 0.0
            van[i] = vbn[i] = vcn[i] = 0.0

    _in = ia + ib + ic  # large residual (ground current) during fault

    trip_sig = (t >= T_TRIP).astype(int)
    p50p_sig = ((t >= T_FAULT) & (t < T_TRIP)).astype(int)
    p50g_sig = p50p_sig.copy()  # ground element also picks up

    def _raw(sig, scale):
        return np.clip(np.round(sig / scale), -32767, 32767).astype(int)

    analog_raw  = [_raw(ia, A_CURRENT), _raw(ib, A_CURRENT),
                   _raw(ic, A_CURRENT), _raw(_in, A_CURRENT),
                   _raw(van, A_VOLTAGE), _raw(vbn, A_VOLTAGE), _raw(vcn, A_VOLTAGE)]
    digital_raw = [trip_sig, p50p_sig, p50g_sig]
    ts_us       = (t * 1e6).astype(int)

    return analog_raw, digital_raw, ts_us, n


def write_cfg(out_dir):
    n = int(FS * T_TOTAL)
    lines = [
        "Maple Ave Feeder,SEL-351-LLG-Test,1999",
        "10,7A,3D",
        f"1,IA,A,,A,{A_CURRENT:.8f},0.0,0,-32767,32767,600,5,P",
        f"2,IB,B,,A,{A_CURRENT:.8f},0.0,0,-32767,32767,600,5,P",
        f"3,IC,C,,A,{A_CURRENT:.8f},0.0,0,-32767,32767,600,5,P",
        f"4,IN,N,,A,{A_CURRENT:.8f},0.0,0,-32767,32767,600,5,P",
        f"5,VAN,A,,V,{A_VOLTAGE:.8f},0.0,0,-32767,32767,10180,1,P",
        f"6,VBN,B,,V,{A_VOLTAGE:.8f},0.0,0,-32767,32767,10180,1,P",
        f"7,VCN,C,,V,{A_VOLTAGE:.8f},0.0,0,-32767,32767,10180,1,P",
        "1,TRIP,A,,0",
        "2,50P,A,,0",
        "3,50G,N,,0",
        "60",
        "1",
        f"{FS},{n}",
        "01/06/2026,14:40:00.000000",
        "01/06/2026,14:40:00.050000",
        "ASCII",
        "1",
    ]
    path = os.path.join(out_dir, "test_llg.cfg")
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"Written: {path}")


def write_dat(out_dir, analog_raw, digital_raw, ts_us, n):
    path = os.path.join(out_dir, "test_llg.dat")
    with open(path, "w") as fh:
        for i in range(n):
            row = [str(i + 1), str(int(ts_us[i]))]
            row += [str(int(ch[i])) for ch in analog_raw]
            row += [str(int(ch[i])) for ch in digital_raw]
            fh.write(",".join(row) + "\r\n")
    print(f"Written: {path}")


def main():
    out_dir = os.path.join(os.path.dirname(__file__), "test_llg")
    os.makedirs(out_dir, exist_ok=True)
    analog_raw, digital_raw, ts_us, n = generate()
    write_cfg(out_dir)
    write_dat(out_dir, analog_raw, digital_raw, ts_us, n)
    print()
    print("Run:")
    print(f"  python3 main.py {out_dir}/test_llg.cfg --report --phasor-plot --save-plots")


if __name__ == "__main__":
    main()
