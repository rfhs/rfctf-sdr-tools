# SilentDisco

A multi-channel FM receiver for the DEFCON 34 RF CTF **Silent Disco** transmitters
(TX1345 A-E, or Ultra-900 U9 with `-u`). One HackRF One captures the entire
908-927.5 MHz band in a single 20 M$/s stream; up to 10 selected channels are
FM-demodulated simultaneously in software and mixed into one stereo audio
output, with runtime per-channel mute/unmute and master volume. A fully
simulated FM source is included for offline validation without a HackRF or
transmitters.

## Status (as of last handoff)

- Multi-channel DSP path is **working end-to-end** in simulation: 1, 2, 3 and 5
  simultaneous chains all decode their distinct tones cleanly
  (`tests/sim_test.c` reports PASS up to 5 chains).
- **Audio cleanup pass (latest):** the chain now matches what SDR++ does —
  60 dB Kaiser channel filter, 15 kHz audio LPF after demod (kills the
  discriminator hiss, 19 kHz pilot and 38 kHz subcarrier), a per-chain audio
  AGC (voice was previously ~40 dB under full scale with no leveling), and a
  kf default derived from `--dev` using liquid's verified kf = dev/fs_band
  convention. See "Audio cleanup" under "Bugs found and fixed" below.
- Verified against live RF captures from the DEF CON environment: decode is
  correct and the residual static on all channels traces to the elevated
  in-channel RF noise floor from 39+ co-active carriers, not the DSP (see the
  "high noise floor" entry below). While away from the transmitter area, run
  `sdisco` with channels selected and listen; each channel's message should be
  audible and routes to the correct slot.
- **Dropout/crackle fixes (latest):** the DSP thread no longer discards
  non-multiple-of-40 IQ remainders (that was the ~0.35 % tone offset — now
  exactly 400.0 Hz in `sim_test` — and a constant live crackle), and the
  HackRF-side IQ ring grew from 3.3 ms to ~26 ms with dropped samples now
  counted (`IQDROPS` in the diag line) instead of silently lost.

## Dependencies

- liquid-dsp (Homebrew: `brew install liquid-dsp`)
- portaudio (`brew install portaudio`)
- libhackrf (`brew install hackrf`)

Build with CMake (macOS; paths assume `/opt/homebrew`):

```sh
cmake -S . -B build
cmake --build build
```

Produces `build/sdisco` (main program) and `build/sim_test` (unit/sim test).

## Running

### Simulated FM source (no HackRF)

```sh
./build/sdisco -s -c A1,B3,E5
```

Each selected channel is synthesised as an FM transmitter at its true RF
frequency, offset relative to the receiver LO, carrying a unique tone
(channel i -> 400*(i+1) Hz) so that mixing and routing are verifiable by ear
or by `sim_test`.

### Real HackRF

```sh
./build/sdisco -c A1,B3,E5 --lna 30 --vga 40 --amp
```

`-l` forces the LO; otherwise the LO starts at the midpoint of the selected
channels and is automatically nudged (up to ±5 MHz) so every channel lands
at least ~1 MHz away from baseband DC — the HackRF has a strong DC spur at
0 Hz that otherwise turns the demod into static. The whole selection must
fit within 20 MHz (the 45 channels span 19.4 MHz so the full band fits in
one capture).

### CLI options

```
-c, --channels <list>   comma/space separated channel names, max 10 (MAX_DEMOD)
-l, --lo <Hz>           HackRF LO center (default = auto, nudged off the DC spur)
-r, --rate <Hz>         capture rate (default = auto: smallest of
                        8/10/12.5/16/20 Msps covering the selection)
-k, --kf <float>        FM demod factor (default: derived from --dev)
-v, --volume <float>    output volume (default 1.0)
    --deemph <us>       de-emphasis tau in us (0=off, 50, 75; default 75)
    --dev <kHz>         assumed FM deviation, sets default kf (default 200)
    --bw <kHz>          channel filter width (default 220)
    --lpf <kHz>         audio low-pass cutoff (default 15)
    --level <f>         audio AGC target level (default 0.3)
    --no-agc            disable the audio AGC
    --lna <db>          HackRF LNA gain 0-40 (default 20)
    --vga <db>          HackRF VGA gain 0-62 (default 40)
    --amp               enable HackRF amp (default off)
-s, --sim               run simulated FM source (no HackRF)
-h, --help
```

### If it sounds bad, try these first

```sh
./build/sdisco -u -c A3                  # defaults are the clean path
./build/sdisco -u -c A3 --deemph 0       # TX may not use pre-emphasis
./build/sdisco -u -c A3 --deemph 50      # ... or the EU 50 us standard
./build/sdisco -u -c A3 --bw 260         # distorted on loud peaks: widen IF
./build/sdisco -u -c A3 --no-agc -k 0.15 # manual level control
```

### Runtime keys (terminal)

```
1-9    toggle mute/unmute for channel 1..9 (in selection order)
0      toggle all channels
+/-    raise/lower master volume
q      quit
```

While running, a diagnostic line updates every ~2 s:
`RATE <Hz>  IQ <0-1>  A1 <level>  C5 <level> …` — RATE is the audio
production rate (should sit at ~48000 live; below that means the DSP is
falling behind and the sound card races through what arrives), IQ is the
mean |IQ| of the HackRF stream (RF presence / gain), followed by each
selected channel's demod RMS level. A channel with a signal shows a healthy
level; a silent one reads ~0. Good for confirming a transmitter is up before
you tweak de-emphasis. Drop/underrun counters appear only when nonzero:

- `IQDROPS n` — RF samples lost because the DSP thread fell behind (the
  HackRF ring is ~26 ms deep). n frozen at startup is benign; climbing means
  the CPU cannot sustain 20 Msps — deselect channels.
- `AUDUND n` — PortAudio asked for frames the audio ring did not have
  (starvation; audible as choppy/robotic dropouts).  Should stay at zero
  after priming; if it climbs, the DSP is not sustaining 48 kHz of audio
  (check RATE).
- `AUDDROPS n` — mixed audio the output ring could not hold (overflow).

## Channel map

5 transmitters (A–E) x 9 slots (1–9), row = transmitter, col = slot.

|  TX  |  1    |  2    |  3    |  4    |  5    |  6    |  7    |  8    |  9    |
|------|-------|-------|-------|-------|-------|-------|-------|-------|-------|
|  A   | 908.1 | 909.9 | 911.8 | 913.6 | 915.1 | 920.7 | 922.8 | 924.7 | 926.2 |
|  B   | 908.4 | 910.3 | 912.2 | 913.9 | 915.5 | 921.2 | 923.1 | 925.0 | 926.5 |
|  C   | 908.7 | 910.8 | 912.7 | 914.2 | 915.8 | 921.5 | 923.4 | 925.3 | 926.8 |
|  D   | 909.2 | 911.1 | 913.0 | 914.5 | 920.1 | 921.9 | 923.8 | 925.6 | 927.1 |
|  E   | 909.5 | 911.4 | 913.3 | 914.8 | 920.4 | 922.3 | 924.2 | 925.9 | 927.5 |

Source: `src/channel.c:chan_tbl`, from the TX1345-A..E datasheet.

### Ultra-900 (U9) map

The same 920-927 MHz band is used by the Ultra-900 US headphone units
(HP2310-U). Selectable with `-u` / `--u9`; channels are named `CH1..CH10`
plus three alternates `A1..A3` (note the A1-A3 frequencies differ from the
TX1345 map - they are non-overlapping quieter alternates):

| Channel | MHz  | Color    | Channel | MHz  | Color    |
|---------|------|----------|---------|------|----------|
| CH1     | 920.1| Blue     | CH6     | 922.8| Turquoise|
| CH2     | 920.7| Red      | CH7     | 923.4| Baby Blue|
| CH3     | 921.2| Green    | CH8     | 924.2| Orange   |
| CH4     | 921.9| Purple   | CH9     | 924.7| Pink     |
| CH5     | 922.3| Yellow   | CH10    | 925.9| Mint     |
| A1      | 920.5| (alt)    | A2      | 922.4| (alt)    |
| A3      | 926.7| (alt)                         |        |          |

Source: `src/channel.c:u9_tbl`, from the Ultra-900 (U9) US unit datasheet.

## Architecture

```
HackRF RX callback     DSP: 1 reader/mixer thread          PortAudio callback
┌─────────────────┐    + 1 worker thread per channel    ┌────────────────────┐
│ int8 IQ -> float│    ┌──────────────────────────────┐ │ ring → stereo out  │
│ complex, N Msps │ →  │ ring → chunk broadcast:      │ │ (L=R mono, volume  │
│ → IQ ring       │    │  per worker: NCO mix →       │ │  applied)          │
└─────────────────┘    │  FIR decim → freqdem →       │ └────────────────────┘
                       │  de-emphasis → 48 kHz        │
                       │  msresamp → 15 kHz LPF → AGC │
                       │  reader sums chains          │
                       └──────────────────────────────┘
```

- `src/hackrx.c` — HackRF wrapper: int8 I/Q -> liquid float-complex, 4096
  samples at a time into a shared input ring.
- `src/dsp.c/h` — the DSP engine. A reader thread drains the IQ ring in
  whole multiples of the decimation factor and broadcasts each chunk to one
  worker thread per channel (a single thread could not sustain 20 Msps with
  several channels: the per-channel FIR decimator and NCO mix are ~one core
  each there). Per chain, on its own thread:
  - `nco_crcf` (LIQUID_VCO, table-driven) mix-down of `(chan_freq - lo)` to DC
  - `firdecim_crcf` decimate fs_in -> 500 kHz (M = fs_in/500 kHz, computed
    at runtime), Kaiser windowed-sinc, cutoff ±110 kHz (`--bw`), 60 dB
    stopband; rejects the 200/400 kHz-spaced neighbours that the old 40 dB
    Cheby2 IIR let through as crosstalk
  - `freqdem` FM discriminator (kf from `--dev`, or `-k` explicitly; liquid's
    convention is kf = deviation/fs_band, verified empirically)
  - one-pole de-emphasis, tau from `--deemph` 0/50/75 µs (default 75)
  - `msresamp_rrrf` 500 kHz -> 48 kHz
  - audio low-pass (`firfilt_rrrf` Kaiser, `--lpf` 15 kHz default, 60 dB):
    strips the discriminator's high-frequency hiss (FM noise rises with f²),
    the 19 kHz stereo pilot and 38 kHz subcarrier remnants
  - audio AGC (on by default, `--no-agc` to disable): peak-envelope follower,
    ~10 ms attack / ~150 ms release, target `--level` 0.3, gain clamped to
    [-12, +40] dB and smoothed; levels the voice regardless of RF gain and
    the transmitter's actual deviation
  - per-chain gain multiply (mute/volume); the reader then sums all active
    chains sample-by-sample into one mono output ring.
- `src/audio.c/h` — PortAudio output: pulls mono ring, applies master volume,
  duplicates to L/R (stereo) as float32 at 48 kHz, 1024-frame callback at
  high-latency setting (glitch-free over low-latency: it is a listening
  tool). Playback primes first: the callback outputs silence until the ring
  holds a ~0.34 s cushion (`PRIME_FRAMES`), because in live mode the producer
  is paced by the RF clock and can never recover a cushion once drained —
  without priming the ring hovers near empty and every scheduling jitter is
  an audible underrun. Frames the ring could not supply are counted in
  `aud.underruns`.
- `src/ring.c/h` — lock-protected single-producer/single-consumer ring used by
  both HackRF and sim sources, and between DSP and audio.

The capture rate is picked at runtime: the smallest of 8/10/12.5/16/20 Msps
that covers the selected span (`-r` forces it). The decimation factor is
chosen so the post-decimation band rate is always 500 kHz, keeping every
downstream stage identical at any capture rate. A lower rate matters
because a USB link that cannot sustain the capture drops samples
on-device — invisible to every in-process counter and audible as the audio
RATE sagging below 48 kHz. DSP timing constants live in `src/dsp.h`:
`DSP_FS_IN=20e6` (max), `DSP_FS_BAND=500e3`, `DSP_CHUNK=4000`,
`DSP_FS_AUD=48e3`.

## Testing

`tests/sim_test.c` validates the DSP chain offline, no RF and no PortAudio:

```sh
cmake --build build
./build/sim_test 5    # 5 channels
```

For each selected channel it generates an FM carrier at the channel's RF
frequency carrying the 400*(i+1) Hz tone, runs it through the `dsp` engine
at 20 M$/s, reads the mixed audio, then band-peak-searches the DFT around each
expected tone. A channel passes when a peak within 1% of the expected
frequency exists with amplitude > 0.02. `RESULT: PASS` is printed at the end.
Run with different channel counts (1, 2, 3, 5) to exercise the mix path.

## Known issues / things to pick up next

1. **Fix the `sim_thread` static buffers** (they are declared `static` inside
   the loop in `src/main.c`; fine for a single sim, but they are not
   thread-local and would collide if ever instantiated twice).
2. **Cleanup / hardening:** remove the leftover `dump.raw` at the repo root
   (test trace) before shipping; check `build_dir/` is stale vs `build/`.
3. **Validate against real signals** — with a HackRF and the TX1345 units
   present, listen for N distinct voices mapped to the right slots.

## Bugs found and fixed (read before touching signal chain)

- **NCO reset order.** `nco_crcf_reset()` after `nco_crcf_set_frequency()`
  zeroes the LO offset, so every channel except the one exactly at DC came out
  demodulating 0 Hz (i.e., garbage). Fix: reset first, then set frequency
  (`dsp_set_chain` in `src/dsp.c`).
- **Integer underflow on channel offset.** `(freq - lo)` computed in `uint64_t`
  underflows when the channel is below the LO, breaking carrier generation.
  Fix: cast both to `double` BEFORE subtracting (`src/main.c:sim_thread`,
  `tests/sim_test.c`).
- **float time-step in the synthesizer.** Using `(float)t` in the message
  tone rounding at 20 M$/s (dt=50ns) freezes phase steps and corrupts the
  tone. Fix: keep `t` a `double` everywhere in the simulators (`sim_thread`,
  `sim_test`); the per-sample FM deviation still uses float arithmetic, only
  time and phase accumulation are double.
- **Carrier on the HackRF DC spur (static, single channel).** An LO tuned to
  exactly a channel puts the carrier at baseband 0 Hz, right on the HackRF's
  strong DC bias spur. Fix: `pick_lo_clear()` nudges the LO ±1.5–5 MHz so the
  carrier lands ≥1 MHz from DC (single D8=925.6 → LO 927.1 → −1.5 MHz
  baseband); `dsp_thread` also subtracts the chunk's complex DC component
  before the NCO mix.
- **Static on ALL channels beaten down by in-band noise.** With 39+ FM
  carriers packed in the 19.4 MHz span + strong dual-channel RF, the in-channel
  noise floor at DEF CON is elevated (audio SNR ceiling ~20–25 dB), not a DSP
  bug: identical static measured via an independent SciPy demod of raw HackRF
  IQ, and the synth loop through the full C chain gives 70+ dB SNR / 0 clicks.
  Evidence: varying IF width (110→220 kHz), LO nudges, DC-offset compensation,
  and residuals at −2.5k…−500 Hz all leave audio SNR flat. Fix so far: default
  LNA lowered 30→20 dB, VGA 40 dB (measured +3–8 dB audio SNR at gain 20/40 vs
  30/40 in-environment).
- **Muffled / over-rolled-off voice.** The old code forced a 75 µs
  de-emphasis; if the TX1345 unit transmits without pre-emphasis (or uses
  50 µs), the HF is cut and speech sounds muffled. Fix: `--deemph <0|50|75>`
  µs (default 75). Try `--deemph 0` for a no-pre-emphasis transmitter, or
  `--deemph 50` for the EU standard.
- **Crackle/static from silently dropped IQ samples ("sounds like a sample
  rate mismatch").** Two drop sites, both fixed:
  1. `dsp_thread` read up to `DSP_CHUNK` from the IQ ring and used
     `got / DSP_DECIM_M`, discarding the `got % 40` remainder on every short
     read.  With the HackRF's 4096-sample pushes the leftover was 96 samples,
     of which 16 were thrown away — ~0.4 % of the stream lost in little
     jumps, audible as constant crackle and measurable as the old
     ~0.35 % tone offset in `sim_test` (401.6 Hz for a 400 Hz tone; now
     exactly 400.0).  Fix: only consume whole multiples of `DSP_DECIM_M`,
     leaving the remainder in the ring for the next pass.
  2. The HackRF-side IQ ring was only 2^16 complex samples (3.3 ms at
     20 Msps) and `rx_callback` ignored short writes, so any DSP scheduling
     hiccup dropped RF silently.  Fix: ring raised to 2^19 (~26 ms),
     `hackrx_open` now zero-initialises the struct (the uninitialised
     `hx.error` also caused a phantom "hackrf error seen" exit in `--sim`
     mode), and overflows are counted in `hx.drops` — the diag line shows
     `IQDROPS n` if any occur.  The RF stream is also started only after the
     DSP thread and PortAudio are running; previously ~64 ms of RF overflowed
     the ring during `Pa_Initialize()` at every startup.
- **Chronic PortAudio underruns in live mode (~2 Hz click, "fast" voices,
  AUDUND climbing).** The audio ring started empty and the producer is paced
  by the real-time RF clock, so it could never get ahead of the sound card:
  the ring hovered near zero forever and every burst-phase jitter dropped
  frames.  The sim never showed it because the sim producer runs at CPU
  speed and keeps the ring full.  Fix: playback primes on a ~0.34 s cushion
  before draining (and re-primes after a full drain), and the diag line now
  prints the audio production `RATE` so a rate problem is visible as a
  number instead of a hunch.
- **Audio RATE sagging below 48 kHz (choppy "fast" audio, dropouts every
  ~2 s).** Two distinct causes, both fixed:
  1. *Capture rate too high for the USB link.* At 20 Msps (40 MB/s) the
     link dropped samples on the HackRF itself — invisible to every
     in-process counter.  Fix: the capture rate is now the smallest of
     8/10/12.5/16/20 Msps that covers the selection (a single channel runs
     at 8 Msps), `-r` forces it, and a one-time warning prints if the live
     audio RATE sags below 47 kHz.
  2. *Single DSP thread.* The per-channel FIR decimator + NCO are ~one core
     each at 20 Msps; one thread topped out near 5 channels.  Fix: one
     worker thread per channel fed by a chunk broadcast from the reader
     thread (10 channels at 20 Msps now holds RATE 48000 in sim).  The NCO
     also moved to liquid's table-driven `LIQUID_VCO`.
  The sim got matching fixes: it is paced to wall-clock real time and its
  (exactly periodic, 2.5 ms) multi-tone baseband is rendered once and
  looped, so it behaves like a live source without the CPU cost.
- **Audio cleanup (garbled voice behind static).** Three gaps vs what SDR++
  does, all in the demod chain, all fixed:
  1. *No audio low-pass at all.* The full ~250 kHz-wide discriminator output
     went straight into the resampler; FM discriminator noise rises with f²,
     so the 15-24 kHz hiss band landed on top of the voice (that was most of
     the "static"), along with the 19 kHz stereo pilot. Fix: Kaiser FIR LPF
     at 15 kHz (`--lpf`), applied at 48 kHz after the resampler, 60 dB stop;
     measured 83 dB rejection of a 20 kHz tone with the voice band intact.
  2. *Weak channel filter.* The order-6 Cheby2 IIR decimator had a 40 dB
     stopband, so loud FM neighbours 400 kHz away leaked in as crosstalk.
     Fix: Kaiser windowed-sinc FIR decimator, cutoff ±110 kHz (`--bw`),
     60 dB stopband.
  3. *No AGC, arbitrary kf.* Raw demod output sat at ~0.004 (~-48 dBFS) with
     no leveling, hence "barely audible". Also, liquid's kf convention is
     kf = deviation/fs_band (verified with a matched freqmod/freqdem pair),
     not 2*pi*dev/fs. Fix: default kf derived from `--dev 75` (-> 0.15), and
     a per-chain peak-envelope AGC (`--level`, `--no-agc`) that levels the
     voice to ~0.3 regardless of RF gain or actual deviation.