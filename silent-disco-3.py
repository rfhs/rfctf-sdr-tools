#!/usr/bin/env python3
"""
silent_disco_multi_monitor.py

RFCTF Silent Disco multi-channel monitor.

Captures ONE wideband IQ stream from a HackRF and simultaneously
demodulates + records every channel in CHANNELS to its own WAV file.
Optionally also plays one or several selected channels live through the
speakers, mixed together -- audition each solo first, then combine the
ones you want to hear at once.

Add channels by appending to CHANNELS below -- the hardware center
frequency and per-channel mixing offsets are computed automatically
from whatever is in the list, so you don't need to hand-tune anything
else when you add channel 3, 4, 5.

Usage:
    python3 silent_disco_multi_monitor.py --samp-rate 8000000
    python3 silent_disco_multi_monitor.py --samp-rate 8000000 --monitor 0
    python3 silent_disco_multi_monitor.py --samp-rate 8000000 --monitor 0,2,4
    python3 silent_disco_multi_monitor.py --list

Requirements: gnuradio (with gr-osmosdr, audio, filter, analog blocks).
"""

import argparse
import os
import sys
import time

from fractions import Fraction

from gnuradio import gr, blocks, filter, analog, audio
from gnuradio.filter import firdes
from gnuradio.fft import window
import osmosdr

# --- Known channels. Fill in channel 3, 4, 5 as you confirm them in gqrx. ----
# mode: "nfm" for narrowband FM, "wfm" for wideband FM (check gqrx's Mode field)
# cutoff/transition: half of whatever gqrx's Filter width showed for that
#   channel (e.g. gqrx "User (240 k)" -> cutoff ~120000, since GNU Radio's
#   Low Pass Filter cutoff is one-sided around a complex-baseband channel)
CHANNELS = [
    {"name": "Channel 1", "freq": 924503000, "mode": "wfm", "cutoff": 75_000, "transition": 25_000},
    {"name": "Channel 2", "freq": 926695000, "mode": "wfm", "cutoff": 120_000, "transition": 40_000},
    {"name": "Channel 3", "freq": 921201000, "mode": "wfm", "cutoff": 75_000, "transition": 25_000},
    {"name": "Channel 4", "freq": 921902000, "mode": "wfm", "cutoff": 75_000, "transition": 25_000},
    {"name": "Channel 5", "freq": 922799000, "mode": "wfm", "cutoff": 75_000, "transition": 25_000},
]

# Gains confirmed working in gqrx (AMP, LNA, VGA)
RF_GAIN, IF_GAIN, BB_GAIN = 0, 16, 8

TARGET_CHANNEL_RATE = 480_000  # matches your earlier confirmed-working chain
AUDIO_RATE = 48_000


class SilentDiscoMultiMonitor(gr.top_block):
    def __init__(self, channels, samp_rate, hw_center, device_args="", record_dir="."):
        gr.top_block.__init__(self, "Silent Disco Multi-Channel Monitor")

        self.channels = channels
        self.samp_rate = samp_rate

        self.source = osmosdr.source(args=device_args)
        self.source.set_sample_rate(samp_rate)
        self.source.set_center_freq(hw_center, 0)
        self.source.set_gain(RF_GAIN, 0)
        self.source.set_if_gain(IF_GAIN, 0)
        self.source.set_bb_gain(BB_GAIN, 0)
        self.source.set_dc_offset_mode(1, 0)  # automatic, same as gqrx's DC remove

        decim = max(1, int(samp_rate // TARGET_CHANNEL_RATE))
        channel_rate = samp_rate / decim

        self.gates = []
        gated_outputs = []

        for i, ch in enumerate(channels):
            offset = ch["freq"] - hw_center
            if abs(offset) + ch["cutoff"] + ch["transition"] > samp_rate / 2:
                raise ValueError(
                    f"{ch['name']} ({ch['freq']} Hz) doesn't fit inside a "
                    f"{samp_rate/1e6:.2f} Msps capture centered at {hw_center} Hz. "
                    f"Increase --samp-rate or check the channel frequency."
                )

            taps = firdes.low_pass(1.0, samp_rate, ch["cutoff"], ch["transition"], window.WIN_HAMMING)
            xlate = filter.freq_xlating_fir_filter_ccc(decim, taps, offset, samp_rate)
            self.connect(self.source, xlate)

            # The coarse decimation above rarely lands on a rate that's a
            # clean multiple of AUDIO_RATE (nbfm_rx/wfm_rcv both require an
            # exact integer relationship). Rational-resample from whatever
            # channel_rate we actually got to exactly TARGET_CHANNEL_RATE,
            # same role as the Rational Resampler block in the GRC version.
            ratio = Fraction(TARGET_CHANNEL_RATE, int(channel_rate)).limit_denominator(1000)
            resampler = filter.rational_resampler_ccc(interpolation=ratio.numerator,
                                                        decimation=ratio.denominator)
            self.connect(xlate, resampler)
            exact_rate = channel_rate * ratio.numerator / ratio.denominator

            if ch["mode"] == "wfm":
                audio_decim = max(1, int(round(exact_rate / AUDIO_RATE)))
                demod = analog.wfm_rcv(quad_rate=exact_rate, audio_decimation=audio_decim)
            else:
                demod = analog.nbfm_rx(audio_rate=AUDIO_RATE, quad_rate=exact_rate, tau=75e-6, max_dev=5e3)
            self.connect(resampler, demod)

            safe_name = ch["name"].replace(" ", "_")
            wav_path = os.path.join(record_dir, f"{i + 1:02d}_{safe_name}.wav")
            wav_sink = blocks.wavfile_sink(wav_path, 1, AUDIO_RATE, blocks.FORMAT_WAV, blocks.FORMAT_PCM_16)
            self.connect(demod, wav_sink)

            gate = blocks.multiply_const_ff(0.0)  # muted by default; unmuted via set_monitor()
            self.connect(demod, gate)
            self.gates.append(gate)
            gated_outputs.append(gate)

            print(f"[{i}] {ch['name']}: {ch['freq']/1e6:.4f} MHz (offset {offset:+} Hz, {ch['mode']}) "
                  f"-> {wav_path}")

        self.adder = blocks.add_vff(1)
        for i, g in enumerate(gated_outputs):
            self.connect(g, (self.adder, i))
        self.audio_sink = audio.sink(AUDIO_RATE, "", True)
        self.connect(self.adder, self.audio_sink)

    def set_mix(self, indices):
        """Unmute the given channel index or indices for live listening,
        summed together. Accepts a single int or an iterable of ints.
        Gain per active channel is normalized by how many are active, so
        mixing several at once doesn't clip. All channels keep recording
        to their WAV files regardless of what's being monitored live."""
        if isinstance(indices, int):
            indices = [indices]
        active = sorted(set(i for i in indices if 0 <= i < len(self.gates)))
        n = max(1, len(active))
        for i, g in enumerate(self.gates):
            g.set_k(1.0 / n if i in active else 0.0)
        return active

    # Back-compat alias for the old single-channel name.
    def set_monitor(self, index):
        return self.set_mix(index)


def main():
    parser = argparse.ArgumentParser(description="RFCTF Silent Disco multi-channel monitor/recorder")
    parser.add_argument("--samp-rate", type=int, default=8_000_000,
                         help="HackRF sample rate in Hz. Start modest and increase gradually; "
                              "very high rates (e.g. 20M) may overrun on some systems.")
    parser.add_argument("--center", type=int, default=None,
                         help="Override the hardware center frequency (Hz). Defaults to the "
                              "midpoint of all configured channels.")
    parser.add_argument("--device-args", default="", help="gr-osmosdr device args, e.g. hackrf=0")
    parser.add_argument("--record-dir", default=".", help="Directory to write per-channel WAV files")
    parser.add_argument("--monitor", type=str, default=None,
                         help="Channel index, or comma-separated indices to mix, to play live "
                              "through the speakers (e.g. --monitor 0,2,4)")
    parser.add_argument("--list", action="store_true", help="List configured channels and exit")
    args = parser.parse_args()

    if args.list or not CHANNELS:
        for i, ch in enumerate(CHANNELS):
            print(f"[{i}] {ch['name']}: {ch['freq']/1e6:.4f} MHz ({ch['mode']})")
        if args.list:
            sys.exit(0)

    if len(CHANNELS) < 5:
        print(f"Warning: only {len(CHANNELS)} channel(s) configured; challenge requires 5.",
              file=sys.stderr)

    freqs = [c["freq"] for c in CHANNELS]
    hw_center = args.center if args.center is not None else (min(freqs) + max(freqs)) // 2

    os.makedirs(args.record_dir, exist_ok=True)

    tb = SilentDiscoMultiMonitor(CHANNELS, args.samp_rate, hw_center,
                                  device_args=args.device_args, record_dir=args.record_dir)
    if args.monitor:
        indices = [int(x) for x in args.monitor.split(",") if x.strip().isdigit()]
        active = tb.set_mix(indices)
        print(f"-> live monitor: {', '.join(CHANNELS[i]['name'] for i in active) or 'none'}")

    tb.start()
    print("\nRecording all channels.\n"
          "Type one index to solo it, or comma-separated indices to mix them "
          "(e.g. 0,2,4). Type 'q' to stop.\n"
          "Tip: audition each channel solo first, then combine the ones you want.\n")
    try:
        while True:
            cmd = input("Channel(s) to listen to (or q): ").strip()
            if cmd.lower() in ("q", "quit", "exit"):
                break
            indices = [int(x) for x in cmd.split(",") if x.strip().isdigit()]
            if not indices:
                print("Enter a channel index, e.g. 1 or 0,2,4.")
                continue
            active = tb.set_mix(indices)
            if active:
                print(f"-> now listening to: {', '.join(CHANNELS[i]['name'] for i in active)}")
            else:
                print("No valid channel indices in that input; nothing changed.")
    except KeyboardInterrupt:
        pass
    finally:
        tb.stop()
        tb.wait()


if __name__ == "__main__":
    main()
