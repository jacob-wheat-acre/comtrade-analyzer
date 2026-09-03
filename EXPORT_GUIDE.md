# Getting event files out of the relay

A one-page answer to "what do I export, and why are there two files?"

Written for the COMTRADE Analyzer, but nothing here is specific to it — this is
what the standard says and what it costs you on a slow remote link.

Reference: **IEEE Std C37.111-2013 / IEC 60255-24:2013**, *Common format for
transient data exchange (COMTRADE) for power systems*. Clause numbers below
point into it.

---

## 1. Why one event is several files

A COMTRADE record is a **set of up to four files that share one basename** and
differ only by extension (clause 5.1). Getting one without the other is like
having a chart with no axis labels.

| Extension | Required? | What it is |
|---|---|---|
| `.CFG` | **Yes** | The configuration. Channel names, units, the scaling factors that turn stored integers into amps and volts, sample rate, line frequency, start and trigger timestamps, and which format the `.DAT` is in. Plain text. |
| `.DAT` | **Yes** | The samples. One row per sample: sample number, timestamp, every analog channel, every status (digital) channel. ASCII or binary. |
| `.HDR` | No | Free-form narrative text written by whoever made the record. Not machine-readable and not needed. |
| `.INF` | No | Extra structured information in a `.INI`-like format. Optional by design. |

So `BROO2733B - RCL 391-149.CFG` and `BROO2733B - RCL 391-149.DAT` are **one
event**, not two. Copy them together, keep the names identical, and don't
rename one without the other.

> The `.DAT` is meaningless on its own. It holds raw integers; the multiplier
> and offset that turn them into engineering units live in the `.CFG`
> (clause 5.4). A `.DAT` with no `.CFG` cannot be read by anything.

**One-file alternative:** the 2013 revision added `.CFF`, a single file
carrying all four sections (clause 10). If your export offers it, it is easier
to move around and this tool reads it. It is the same data.

---

## 2. Which export format to pick

SEL's export menu typically offers a revision (1999 / 2013), an encoding
(ASCII / Binary / Binary32 / Float32), and a sample rate.

### Recommended: **COMTRADE 2013, Binary, 16 samples/cycle, raw**

#### Binary, not ASCII — this is your download-speed problem

The standard itself says it: *"It is strongly recommended to use the binary,
binary32, or float32 formats for large data files"* (clause 8.3).

Binary stores each analog sample in a fixed 2 bytes (clause 8.6). ASCII writes
it as decimal digits plus a comma — typically 5 to 7 bytes, and more for large
values. Same information, several times the bytes.

Bytes per sample scan, from clause 8.6:

```
(Ak × N) + (2 × INT(Dm / 16)) + 4 + 4

  Ak = analog channels          N  = 2 for Binary, 4 for Binary32/Float32
  Dm = status channels          4+4 = sample number + timestamp
```

For a typical feeder relay record — 8 analog channels, 32 status bits, 16
samples/cycle, 2 seconds at 60 Hz (1920 sample scans):

| Encoding | Bytes per scan | `.DAT` size | vs ASCII |
|---|---|---|---|
| **Binary** | 8×2 + 2×2 + 8 = **28** | **53 KB** | **4.5× smaller** |
| Binary32 / Float32 | 8×4 + 2×2 + 8 = **44** | 83 KB | 2.9× smaller |
| ASCII | ~127 | 238 KB | — |

The status channels are what makes ASCII so much worse than it looks: 32 status
bits cost 4 bytes in binary and 64 bytes in ASCII, because each one is written
as a digit and a comma. The more channels the relay records, the wider the gap.

Over a slow SCADA link, pulling a few hundred events, that is the difference
between minutes and a coffee break — and **the values are identical**. Nothing
is lost; only the encoding changes.

Binary32 and Float32 exist for recorders with more than 16 bits of resolution.
A distribution relay's A/D is 12–16 bit, so they buy you nothing but size.
Plain **Binary** is the right choice. (This tool reads all four, so a folder
that already has Binary32 or Float32 in it is fine — just don't choose them.)

#### 1999 or 2013 — either works

The 2013 revision adds the `.CFF` single-file option, the 32-bit data types,
and two extra `.CFG` lines carrying time-zone and time-quality information
(clause 7.4.11, 7.4.12). None of that changes the waveform. Pick **2013** if
offered, because the time-zone line is genuinely useful when you are comparing
records from relays whose clocks may not agree — but 1999 loses you nothing
this tool needs.

#### 16 samples/cycle, and why 4 is not enough

Annex B lists the standard rates: 4, 8, 16, 32, 64, 128 samples/cycle.

The limit is Nyquist. At **4 samples/cycle** (240 Hz on a 60 Hz system) the
highest frequency the record can represent is 120 Hz — the second harmonic —
and the anti-alias filter in front of it has to cut everything above that.
Annex B.1 states it directly for this rate: a 240 Hz sampling frequency *"must
be obtained using a filter with a cutoff frequency of 120 Hz to avoid
aliasing."* You cannot compute a meaningful phasor, DC offset or fault
inception instant from that.

| Rate | Nyquist (60 Hz system) | Verdict for event analysis |
|---|---|---|
| 4/cycle | 2nd harmonic | **Too few.** Phasors and inception timing will be wrong. |
| 8/cycle | 4th harmonic | Workable minimum. |
| **16/cycle** | 8th harmonic | **Recommended.** Everything this tool computes, at half the size of 32. |
| 32/cycle | 16th harmonic | Fine, and twice the bytes. Use if it is already the standard here. |
| 128/cycle | 64th harmonic | For harmonics and travelling-wave work. Not this. |

This tool computes one-cycle RMS, DFT phasors, symmetrical components, fault
inception and trip time. All of those need roughly 8–16 samples/cycle to be
right, and gain nothing above that.

#### Raw, not filtered

"Filtered" data is the output of the relay's own digital filter — usually a
cosine or Fourier filter that strips DC and harmonics to leave the fundamental,
because that is what the protection element needs to make a decision.

That filter removes exactly two things this analysis measures:

- **DC offset** — the decaying exponential that tells you where on the voltage
  wave the fault struck, and which drives CT saturation.
- **The fault inception edge** — the filter has a group delay of roughly a
  cycle and smears the step, so the inception instant and therefore the
  computed clearing time both move.

Annex B.1 makes the general point: *"the effect of the anti-aliasing filter
cannot be removed"*. Once it is filtered, it stays filtered — you cannot
recover the raw waveform later. **Export raw and filter afterwards if you want
to; the reverse is not possible.**

---

## 3. If a file will not open

Things that are real, legal COMTRADE and still trip up readers. This tool
handles all of them, but they are worth knowing when a different program
chokes:

- **The date order is not what the standard says.** C37.111 clause 7.4.8
  specifies `dd/mm/yyyy`. SEL writes `mm/dd/yyyy`. Read as the standard, an
  American date like `11/19/2023` gives month 19 and the file simply refuses
  to open.
- **The timestamp column may be zeros.** Clause 7.4.7 makes the `.DAT`
  timestamp *non-critical* when the `.CFG` carries a sample rate, and says
  the sample rate is *preferred* for precise timing. A relay is entitled to
  fill the column with zeros. Read time from the sample rate.
- **The date may be blank or all zeros.** Clause 7.4.8 permits it explicitly.
  The record still analyses; it just cannot be placed on a timeline.
- **`nrates = 0` does not mean "no sample rate line".** It means the sample
  period is not fixed, and a line still follows (clause 7.4.7). Skipping it
  puts the reader one line out of step for the rest of the file.
- **A missing analog value is written as an empty field** (`,,`) in ASCII, or
  as the most-negative value in binary (clause 8.4, 8.6). Neither is corruption.

---

## 4. What to ask for

If you are specifying the export once for everybody:

> **COMTRADE, 2013 revision, Binary encoding, 16 samples per cycle, raw
> (unfiltered), with at least 2 cycles of pre-fault data. Deliver the `.CFG`
> and `.DAT` together with matching filenames.**

The pre-fault requirement is the one that gets forgotten and cannot be fixed
afterwards. Every quantity here is measured *against* the pre-fault state:
load current, the voltage the fault collapsed from, the inception instant
itself. A record that starts at the fault has nothing to compare to.
