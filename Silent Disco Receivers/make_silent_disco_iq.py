#!/usr/bin/env python3
#
# make_silent_disco_iq.py - generate a synthetic silent disco IQ capture.
#
# Silent disco transmitters are analogue FM, one carrier per audio channel,
# carrying a broadcast-style stereo multiplex (L+R baseband, 19 kHz pilot,
# L-R on a 38 kHz double-sideband suppressed-carrier subcarrier) with
# pre-emphasis. This script builds exactly that, for as many carriers as you
# ask for, and writes the sum to a complex float32 file.
#
# Purpose: let anyone verify silent_disco_rx.py and the .grc flowgraph
# without owning a silent disco rig. Each channel carries a different tone, so
# a correct decode is provable and a channel mix-up is visible:
#
#     channel k -> left = tone_k Hz, right = 2 * tone_k Hz
#
# This writes a file. It does not transmit.
#
# SPDX-License-Identifier: BSD-3-Clause

import argparse
import math
import sys

from gnuradio import analog
from gnuradio import blocks
from gnuradio import gr

# Tones are chosen to be mutually non-harmonic within a channel's own pair so
# that left (tone) and right (2 x tone) stay distinguishable across channels.
DEFAULT_TONES = [400.0, 700.0, 1100.0, 1700.0, 2300.0, 2900.0, 3700.0, 4300.0]


class silent_disco_source(gr.top_block):
    def __init__(self, offsets, tones, samp_rate, deviation, tau,
                 stereo, levels, noise_amp, nsamples, out_path):
        gr.top_block.__init__(self, "Silent Disco Test Signal")

        combiner = blocks.add_vcc(1)

        for idx, offset in enumerate(offsets):
            tone = tones[idx % len(tones)]
            level = levels[idx % len(levels)]

            # Audio is generated directly at the IQ rate. A 400 Hz tone at
            # 2.4 Msps is exact and needs no interpolation filter, which keeps
            # the reference signal free of anything the receiver could blame.
            left = analog.sig_source_f(samp_rate, analog.GR_COS_WAVE,
                                       tone, 0.5, 0, 0)
            right = analog.sig_source_f(samp_rate, analog.GR_COS_WAVE,
                                        2.0 * tone, 0.5, 0, 0)

            pre_l = analog.fm_preemph(fs=samp_rate, tau=tau, fh=-1.0)
            pre_r = analog.fm_preemph(fs=samp_rate, tau=tau, fh=-1.0)
            self.connect(left, pre_l)
            self.connect(right, pre_r)

            sum_lr = blocks.add_ff(1)
            self.connect(pre_l, (sum_lr, 0))
            self.connect(pre_r, (sum_lr, 1))
            mono_gain = blocks.multiply_const_ff(0.45)
            self.connect(sum_lr, mono_gain)

            if stereo:
                diff_lr = blocks.sub_ff(1)
                self.connect(pre_l, (diff_lr, 0))
                self.connect(pre_r, (diff_lr, 1))

                # Broadcast convention: the pilot is the half-frequency of
                # the subcarrier with their zero crossings aligned, so both
                # are sines. Getting this wrong does not break the decode, it
                # silently swaps left and right, which is worth knowing if you
                # ever build one of these.
                subcarrier = analog.sig_source_f(samp_rate, analog.GR_SIN_WAVE,
                                                 38000.0, 1.0, 0, 0)
                pilot = analog.sig_source_f(samp_rate, analog.GR_SIN_WAVE,
                                            19000.0, 0.10, 0, 0)
                dsb = blocks.multiply_ff(1)
                self.connect(diff_lr, (dsb, 0))
                self.connect(subcarrier, (dsb, 1))
                stereo_gain = blocks.multiply_const_ff(0.45)
                self.connect(dsb, stereo_gain)

                mpx = blocks.add_ff(1)
                self.connect(mono_gain, (mpx, 0))
                self.connect(stereo_gain, (mpx, 1))
                self.connect(pilot, (mpx, 2))
            else:
                mpx = mono_gain

            modulator = analog.frequency_modulator_fc(
                2.0 * math.pi * deviation / samp_rate)
            rotator = blocks.rotator_cc(2.0 * math.pi * offset / samp_rate)
            amplitude = blocks.multiply_const_cc(level)
            self.connect(mpx, modulator, rotator, amplitude,
                         (combiner, idx))

        if noise_amp > 0.0:
            noise = analog.noise_source_c(analog.GR_GAUSSIAN, noise_amp, 42)
            noisy = blocks.add_vcc(1)
            self.connect(combiner, (noisy, 0))
            self.connect(noise, (noisy, 1))
            tail = noisy
        else:
            tail = combiner

        head = blocks.head(gr.sizeof_gr_complex, nsamples)
        sink = blocks.file_sink(gr.sizeof_gr_complex, out_path, False)
        sink.set_unbuffered(False)
        self.connect(tail, head, sink)


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Generate a synthetic silent disco IQ capture "
                    "(complex float32). Writes a file, does not transmit.")
    p.add_argument("--out", required=True,
                   help="output file, complex float32 interleaved")
    p.add_argument("--samp-rate", type=float, default=2.4e6,
                   help="IQ sample rate in Hz (default 2.4e6)")
    p.add_argument("--offsets",
                   default="-1050e3,-550e3,150e3,550e3,1050e3",
                   help="comma separated carrier offsets in Hz from the "
                        "centre of the capture. The default reproduces Quiet "
                        "Events Ultra 900 channels 2 to 6 (920.7, 921.2, "
                        "921.9, 922.3, 922.8 MHz) as seen by a receiver "
                        "centred on 921.75 MHz, which is what "
                        "silent_disco_rx.py tunes to by default")
    p.add_argument("--tones", default="",
                   help="comma separated left-channel tone per carrier in Hz; "
                        "default picks distinct tones automatically")
    p.add_argument("--levels", default="1.0",
                   help="comma separated amplitude per carrier, recycled if "
                        "shorter than the carrier list")
    p.add_argument("--deviation", type=float, default=50e3,
                   help="peak FM deviation in Hz (default 50e3)")
    p.add_argument("--deemph", type=float, default=75.0,
                   help="pre-emphasis time constant in microseconds, "
                        "75 in the US and 50 in Europe (default 75)")
    p.add_argument("--mono", action="store_true",
                   help="omit the pilot and the L-R subcarrier")
    p.add_argument("--noise", type=float, default=0.0,
                   help="Gaussian noise amplitude added to the sum "
                        "(default 0.0)")
    p.add_argument("--seconds", type=float, default=4.0,
                   help="capture length in seconds (default 4.0)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    samp_rate = int(args.samp_rate)
    offsets = [float(x) for x in args.offsets.split(",") if x.strip()]
    levels = [float(x) for x in args.levels.split(",") if x.strip()]
    if args.tones.strip():
        tones = [float(x) for x in args.tones.split(",") if x.strip()]
    else:
        tones = DEFAULT_TONES

    if len(offsets) > len(tones):
        print("error: %d carriers but only %d distinct tones; pass --tones"
              % (len(offsets), len(tones)), file=sys.stderr)
        return 2

    span = max(offsets) - min(offsets)
    if span + 300e3 > samp_rate:
        print("warning: carriers span %.3f MHz, which does not fit cleanly "
              "in a %.3f MHz capture" % (span / 1e6, samp_rate / 1e6),
              file=sys.stderr)

    nsamples = int(samp_rate * args.seconds)
    tb = silent_disco_source(
        offsets=offsets,
        tones=tones,
        samp_rate=samp_rate,
        deviation=args.deviation,
        tau=args.deemph * 1e-6,
        stereo=not args.mono,
        levels=levels,
        noise_amp=args.noise,
        nsamples=nsamples,
        out_path=args.out,
    )

    print("writing %s" % args.out)
    print("  sample rate   %d Hz" % samp_rate)
    print("  length        %.2f s (%d complex samples, %.1f MB)"
          % (args.seconds, nsamples, nsamples * 8 / 1e6))
    print("  deviation     %.0f Hz peak" % args.deviation)
    print("  multiplex     %s" % ("mono" if args.mono else
                                  "stereo (19 kHz pilot, 38 kHz L-R)"))
    for idx, offset in enumerate(offsets):
        print("  channel %d     offset %+9.1f kHz   left %.0f Hz   "
              "right %.0f Hz   level %.2f"
              % (idx, offset / 1e3, tones[idx], 2 * tones[idx],
                 levels[idx % len(levels)]))

    tb.run()
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
