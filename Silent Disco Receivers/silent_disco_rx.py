#!/usr/bin/env python3
#
# silent_disco_rx.py - demodulate several silent disco channels at once.
#
# The DEF CON silent disco is a Quiet Events system: analogue FM, stereo, one
# carrier per audio channel, in the US 902-928 MHz ISM band. A headset is just
# an FM receiver that retunes when you press the channel button, and the LED
# colour tells you which carrier it is on. Nothing stops one SDR from
# demodulating every carrier inside its capture at the same time.
#
#   silent_disco_rx.py --plan ultra900 --channel blue
#       play one channel through the sound card
#
#   silent_disco_rx.py --plan ultra900 --record --record-dir wav
#       demodulate every channel that fits in the capture, concurrently,
#       one WAV file each
#
#   silent_disco_rx.py --source file:capture.cf32 --plan ultra900 --record
#       the same thing offline, against a recorded capture
#
# Receive only. Nothing in this file transmits.
#
# SPDX-License-Identifier: BSD-3-Clause

import argparse
import os
import sys
from fractions import Fraction

from gnuradio import analog
from gnuradio import audio
from gnuradio import blocks
from gnuradio import filter as gr_filter
from gnuradio import gr
from gnuradio.fft import window
from gnuradio.filter import firdes

AUDIO_RATE = 48000

# The stereo multiplex puts L+R below 15 kHz, the pilot at 19 kHz and L-R on a
# 38 kHz subcarrier. Cutting at 15 kHz is what keeps the pilot out of the audio.
AUDIO_PASS_HZ = 15000
AUDIO_STOP_HZ = 17000

# Quiet Events "Ultra 900" plan, used on the main and creator stages. The LED
# colour on a headset is the channel, so a headset lying on a chair tells you
# the frequency without scanning for it.
# Source: quietevents.com Ultra1 900 (10CH) headphone and Ultra 900 Mobile
# Transmitter (13CH) product pages.
ULTRA900 = [
    ("1", "red", 920.1e6),
    ("2", "yellow", 920.7e6),
    ("3", "green", 921.2e6),
    ("4", "purple", 921.9e6),
    ("5", "blue", 922.3e6),
    ("6", "turquoise", 922.8e6),
    ("7", "white", 923.4e6),
    ("8", "orange", 924.2e6),
    ("9", "pink", 924.7e6),
    ("10", "mint", 925.9e6),
    ("A1", "", 920.5e6),
    ("A2", "", 922.4e6),
    ("A3", "", 926.7e6),
]

# Quiet Events "45 Max" plan, used at village and community locations.
# Source: quietevents.com 45 Max Transmitter (45CH) product page.
MAX45 = [f * 1e6 for f in (
    908.1, 908.4, 908.7, 909.2, 909.5, 909.9, 910.3, 910.8, 911.1, 911.4,
    911.8, 912.2, 912.7, 913.0, 913.3, 913.6, 913.9, 914.2, 914.5, 914.8,
    915.1, 915.5, 915.8, 920.1, 920.4, 920.7, 921.2, 921.5, 921.9, 922.3,
    922.8, 923.1, 923.4, 923.8, 924.2, 924.7, 925.0, 925.3, 925.6, 925.9,
    926.2, 926.5, 926.8, 927.1, 927.5)]

CHANNEL_PLANS = {
    # The ten colour channels. Spacing runs 400 kHz to 1.2 MHz, never uniform.
    "ultra900": [f for _, c, f in ULTRA900 if c],
    # Including the three auxiliary channels. A1 sits 200 kHz from yellow and
    # A2 sits 100 kHz from blue, closer than one FM channel is wide, so these
    # are alternatives to the colours rather than additions to them.
    "ultra900-all": [f for _, _, f in ULTRA900],
    "max45": MAX45,
}

# Every name a channel can be called by, for --channel.
CHANNEL_ALIASES = {}
for _ch, _colour, _f in ULTRA900:
    CHANNEL_ALIASES[_ch.lower()] = _f
    CHANNEL_ALIASES["ch" + _ch.lower()] = _f
    if _colour:
        CHANNEL_ALIASES[_colour] = _f


def channel_label(freq):
    for ch, colour, f in ULTRA900:
        if abs(f - freq) < 1e3:
            return "%s %s" % (ch, colour) if colour else ch
    return ""


def choose_rates(samp_rate, chan_bw, audio_rate):
    """Pick channel and audio decimations for this capture.

    Returns (chan_decim, demod_rate, audio_decim, resamp), where resamp is a
    Fraction to apply after demodulation or None if the rates already land on
    audio_rate exactly.
    """
    # Demodulate at roughly 2.4x the channel bandwidth: wide enough to keep the
    # channel filter's transition band away from the FM sidebands, narrow
    # enough that the audio filter stays cheap.
    chan_decim = max(1, int(round(samp_rate / (2.4 * chan_bw))))
    while chan_decim > 1 and samp_rate / chan_decim < 1.5 * chan_bw:
        chan_decim -= 1
    demod_rate = samp_rate / chan_decim

    audio_decim = max(1, int(round(demod_rate / audio_rate)))
    intermediate = demod_rate / audio_decim
    resamp = Fraction(audio_rate / intermediate).limit_denominator(2000)
    return chan_decim, demod_rate, audio_decim, (None if resamp == 1 else resamp)


def choose_center(freqs, usable, dc_guard):
    """Pick the tuner centre that catches the most channels.

    A silent disco plan can be wider than any one SDR: the Quiet Events 45 Max
    plan spans 908 to 927.5 MHz, eight times an RTL-SDR's usable bandwidth.
    Cover the largest group rather than failing, and keep the centre off any
    carrier so nothing lands under the receiver's DC spike.
    """
    best = None
    for lo in freqs:
        center = lo + usable
        caught = [f for f in freqs if abs(f - center) <= usable + 1.0]
        if not caught:
            continue
        center = (min(caught) + max(caught)) / 2.0
        caught = [f for f in freqs if abs(f - center) <= usable + 1.0]
        for center in _dc_clear(center, caught, dc_guard):
            caught2 = [f for f in freqs if abs(f - center) <= usable + 1.0]
            score = (len(caught2), -abs(center - (min(caught2) + max(caught2)) / 2.0))
            if best is None or score > best[0]:
                best = (score, center)
    return best[1] if best else freqs[0]


def _dc_clear(center, caught, dc_guard):
    """Candidate centres that keep every carrier clear of DC."""
    if not caught or min(abs(f - center) for f in caught) >= dc_guard:
        return [center]
    nearest = min(caught, key=lambda f: abs(f - center))
    return [nearest + dc_guard, nearest - dc_guard]


class silent_disco_rx(gr.top_block):
    def __init__(self, args, freqs, center):
        gr.top_block.__init__(self, "Silent Disco Receiver")

        samp_rate = args.samp_rate
        chan_bw = args.channel_bw
        chan_decim, demod_rate, audio_decim, resamp = choose_rates(
            samp_rate, chan_bw, AUDIO_RATE)

        self.demod_rate = demod_rate
        self.audio_decim = audio_decim
        self.chan_decim = chan_decim
        self.resamp = resamp

        source = self._build_source(args, center)

        # One channel filter, shared by every branch. Blackman-Harris rather
        # than Hamming: the plan puts strong and weak transmitters 400 kHz
        # apart, and Hamming's 53 dB stopband is not enough to keep a loud
        # neighbour out of a quiet channel.
        chan_taps = firdes.low_pass(1.0, samp_rate, chan_bw / 2.0,
                                    chan_bw / 4.0,
                                    window.WIN_BLACKMAN_HARRIS, 6.76)
        self.chan_taps = chan_taps

        tau = args.deemph * 1e-6
        self.branches = []

        for idx, freq in enumerate(freqs):
            # Only build a branch that something is listening to. Playing one
            # channel should not cost the CPU of demodulating all ten.
            if not args.record and idx != args.play_index:
                continue

            xlate = gr_filter.freq_xlating_fir_filter_ccf(
                chan_decim, chan_taps, freq - center, samp_rate)

            if args.stereo:
                demod = analog.wfm_rcv_pll(demod_rate, audio_decim, tau)
                nchan = 2
            else:
                # Mono recovers L+R, which is what a stereo multiplex carries
                # below 15 kHz, so this works on mono and stereo transmitters
                # alike. The 15 kHz audio filter is what keeps the 19 kHz
                # pilot and the 38 kHz subcarrier out of the recording.
                demod = analog.fm_demod_cf(
                    channel_rate=demod_rate, audio_decim=audio_decim,
                    deviation=args.deviation, audio_pass=AUDIO_PASS_HZ,
                    audio_stop=AUDIO_STOP_HZ, gain=1.0, tau=tau)
                nchan = 1

            self.connect(source, xlate, demod)

            tails = []
            for port in range(nchan):
                node = (demod, port)
                if resamp is not None:
                    rr = gr_filter.rational_resampler_fff(
                        interpolation=resamp.numerator,
                        decimation=resamp.denominator,
                        taps=[], fractional_bw=0.0)
                    self.connect(node, rr)
                    node = rr
                if args.agc:
                    # Quiet Events do not publish their deviation and there is
                    # no FCC grant under the brand, so level the audio instead
                    # of guessing the number.
                    agc = analog.agc2_ff(0.4, 1e-3, args.level, 1.0)
                    agc.set_max_gain(4000.0)
                    self.connect(node, agc)
                    node = agc
                vol = blocks.multiply_const_ff(args.volume)
                self.connect(node, vol)
                tails.append(vol)

            if args.record:
                label = channel_label(freq).replace(" ", "_")
                name = "%s_%02d_%.4fMHz%s.wav" % (
                    args.prefix, idx, freq / 1e6,
                    "_" + label if label else "")
                wav = blocks.wavfile_sink(
                    os.path.join(args.record_dir, name), nchan, AUDIO_RATE,
                    blocks.FORMAT_WAV, blocks.FORMAT_PCM_16, False)
                for port, tail in enumerate(tails):
                    self.connect(tail, (wav, port))

            if args.play_index is not None and idx == args.play_index:
                sink = audio.sink(AUDIO_RATE, args.audio_device, True)
                for port, tail in enumerate(tails):
                    self.connect(tail, (sink, port))

            self.branches.append(tails)

    def _build_source(self, args, center):
        if args.source.startswith("file:"):
            src = blocks.file_source(gr.sizeof_gr_complex, args.source[5:],
                                     args.repeat, 0, 0)
            node = src
            if args.seconds > 0:
                head = blocks.head(gr.sizeof_gr_complex,
                                   int(args.samp_rate * args.seconds))
                self.connect(node, head)
                node = head
            if args.play_index is not None:
                # Recording wants the file to run as fast as the CPU allows.
                # Playing it needs the stream paced to real time.
                thr = blocks.throttle(gr.sizeof_gr_complex, args.samp_rate,
                                      True)
                self.connect(node, thr)
                node = thr
            return node

        from gnuradio import soapy
        src = soapy.source("driver=%s" % args.source, "fc32", 1,
                           args.device_args, "bufflen=16384", [""], [""])
        src.set_sample_rate(0, args.samp_rate)
        src.set_frequency(0, center)
        src.set_frequency_correction(0, args.ppm)
        src.set_gain_mode(0, args.hw_agc)
        if not args.hw_agc:
            try:
                src.set_gain(0, "TUNER", args.gain)
            except Exception:
                src.set_gain(0, args.gain)
        if args.seconds > 0:
            head = blocks.head(gr.sizeof_gr_complex,
                               int(args.samp_rate * args.seconds))
            self.connect(src, head)
            return head
        return src


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Demodulate silent disco channels, one or all at once.",
        epilog="Receive only. This program does not transmit.")

    g = p.add_argument_group("what to listen to")
    g.add_argument("--plan", default="ultra900",
                   help="named channel plan: %s (default ultra900)"
                        % ", ".join(sorted(CHANNEL_PLANS)))
    g.add_argument("--freqs", default=None,
                   help="comma separated channel frequencies in MHz or Hz, "
                        "overrides --plan")
    g.add_argument("--list-plans", action="store_true",
                   help="print the built in channel plans and exit")
    g.add_argument("--center", type=float, default=None,
                   help="tuner centre in Hz, default catches the most channels")

    g = p.add_argument_group("what to do with it")
    g.add_argument("--channel", default=None,
                   help="play one channel through the sound card, named by "
                        "colour (blue), by number (5, ch5, A2) or in MHz")
    g.add_argument("--record", action="store_true",
                   help="demodulate every channel in the capture at once and "
                        "write one WAV file each")
    g.add_argument("--record-dir", default=".",
                   help="directory for the WAV files (default .)")
    g.add_argument("--prefix", default="silent_disco",
                   help="WAV filename prefix (default silent_disco)")
    g.add_argument("--audio-device", default="",
                   help="ALSA device for playback (default system default)")

    g = p.add_argument_group("radio")
    g.add_argument("--source", default="rtlsdr",
                   help="SoapySDR driver (rtlsdr, hackrf, airspy, bladerf, "
                        "lime, uhd, plutosdr) or file:PATH for a complex "
                        "float32 capture (default rtlsdr)")
    g.add_argument("--device-args", default="",
                   help="extra SoapySDR device arguments")
    g.add_argument("--samp-rate", type=float, default=2.4e6,
                   help="capture sample rate in Hz (default 2.4e6)")
    g.add_argument("--gain", type=float, default=40.0,
                   help="RF gain in dB (default 40)")
    g.add_argument("--hw-agc", action="store_true",
                   help="enable the tuner's own AGC")
    g.add_argument("--ppm", type=float, default=0.0,
                   help="frequency correction in PPM")
    g.add_argument("--dc-guard", type=float, default=60e3,
                   help="keep every carrier at least this far from the tuner "
                        "centre, dodging the DC spike (default 60e3)")
    g.add_argument("--repeat", action="store_true", help="loop a file source")
    g.add_argument("--seconds", type=float, default=0.0,
                   help="stop after this many seconds, 0 runs until "
                        "interrupted (default 0)")

    g = p.add_argument_group("demodulator")
    g.add_argument("--channel-bw", type=float, default=200e3,
                   help="per channel bandwidth in Hz (default 200e3)")
    g.add_argument("--deviation", type=float, default=75e3,
                   help="peak FM deviation in Hz. Used only with --no-agc, "
                        "and only in mono: the stereo path uses GNU Radio's "
                        "wfm_rcv_pll, which fixes deviation at 75 kHz "
                        "internally and ignores this (default 75e3)")
    g.add_argument("--deemph", type=float, default=75.0,
                   help="de-emphasis time constant in microseconds, 75 in the "
                        "US and 50 in Europe (default 75)")
    g.add_argument("--stereo", action="store_true",
                   help="decode the stereo multiplex into L and R. The "
                        "default mono decode recovers L+R and works on both "
                        "mono and stereo transmitters")
    g.add_argument("--no-agc", dest="agc", action="store_false",
                   help="skip the audio AGC and scale by --deviation instead")
    g.add_argument("--level", type=float, default=0.35,
                   help="audio AGC target level (default 0.35)")
    g.add_argument("--volume", type=float, default=1.0,
                   help="output gain applied last (default 1)")

    return p.parse_args(argv)


def resolve_freqs(args):
    if args.freqs:
        out = []
        for tok in args.freqs.split(","):
            tok = tok.strip()
            if tok:
                val = float(tok)
                out.append(val if val > 1e6 else val * 1e6)
        return sorted(out)
    if args.plan not in CHANNEL_PLANS:
        print("error: unknown plan %r, try --list-plans" % args.plan,
              file=sys.stderr)
        return None
    return sorted(CHANNEL_PLANS[args.plan])


def resolve_channel(text):
    key = text.strip().lower()
    if key in CHANNEL_ALIASES:
        return CHANNEL_ALIASES[key]
    try:
        val = float(key)
    except ValueError:
        return None
    return val if val > 1e6 else val * 1e6


def main(argv=None):
    args = parse_args(argv)

    if args.list_plans:
        for name in sorted(CHANNEL_PLANS):
            freqs = sorted(CHANNEL_PLANS[name])
            print("%-14s %2d channels  %.1f - %.1f MHz"
                  % (name, len(freqs), freqs[0] / 1e6, freqs[-1] / 1e6))
            for f in freqs:
                label = channel_label(f)
                print("                 %9.4f MHz  %s" % (f / 1e6, label))
        return 0

    all_freqs = resolve_freqs(args)
    if all_freqs is None:
        return 2

    if not args.record and args.channel is None:
        print("error: nothing to do. Give --channel NAME to play one channel "
              "or --record to write every channel to WAV.", file=sys.stderr)
        return 2

    # The FM demodulator builds an audio low-pass with a 15 kHz passband and a
    # 17 kHz stop. If the channel rate is too low to hold that, the filter
    # designer fails deep inside GNU Radio with "band edges must be
    # nondecreasing", which says nothing useful about the cause. Catch it here.
    _, _demod_rate, _, _ = choose_rates(args.samp_rate, args.channel_bw, AUDIO_RATE)
    if _demod_rate <= 2 * AUDIO_STOP_HZ:
        print("error: --channel-bw %.1f kHz is too narrow. It leaves a channel "
              "rate of %.1f kHz, and the audio filter needs more than %.1f kHz "
              "to fit its %.0f kHz passband. Use at least %.0f kHz."
              % (args.channel_bw / 1e3, _demod_rate / 1e3,
                 2 * AUDIO_STOP_HZ / 1e3, AUDIO_PASS_HZ / 1e3,
                 2 * AUDIO_STOP_HZ / 1e3),
              file=sys.stderr)
        return 2

    if args.channel_bw >= args.samp_rate:
        print("error: --channel-bw %.1f kHz does not fit inside a %.1f kHz "
              "capture. Lower it or raise --samp-rate."
              % (args.channel_bw / 1e3, args.samp_rate / 1e3), file=sys.stderr)
        return 2

    usable = (args.samp_rate - args.channel_bw) / 2.0

    play_freq = None
    if args.channel is not None:
        play_freq = resolve_channel(args.channel)
        if play_freq is None:
            print("error: cannot make sense of --channel %r. Use a colour "
                  "(blue), a number (5 or ch5), or MHz." % args.channel,
                  file=sys.stderr)
            return 2
        if play_freq not in all_freqs:
            all_freqs = sorted(set(all_freqs + [play_freq]))

    center = args.center
    if center is None:
        if play_freq is not None and not args.record:
            # Playing one channel: put it comfortably off DC and ignore the
            # rest of the plan.
            center = play_freq + args.dc_guard * 5
            # A file has no idea what it was tuned to. Guessing a centre from
            # the requested channel is right for a radio and meaningless for a
            # capture, where 0 Hz is wherever the recording was made. Without
            # this warning the receiver translates to an empty part of the
            # spectrum and plays noise, with nothing on screen to say why.
            if str(args.source).startswith("file:"):
                print("warning: --channel with a file source assumes the "
                      "capture is centred on %.4f MHz. If it is not, you will "
                      "hear noise. Give --center with the capture's true "
                      "centre frequency in Hz."
                      % (center / 1e6), file=sys.stderr)
        else:
            center = choose_center(all_freqs, usable, args.dc_guard)

    freqs = [f for f in all_freqs if abs(f - center) <= usable + 1.0]
    dropped = [f for f in all_freqs if f not in freqs]
    if not freqs:
        print("error: no channel falls inside a %.3f MHz capture centred on "
              "%.4f MHz" % (args.samp_rate / 1e6, center / 1e6),
              file=sys.stderr)
        return 2

    args.play_index = None
    if play_freq is not None:
        if play_freq not in freqs:
            print("error: the channel you asked to play is outside the "
                  "capture", file=sys.stderr)
            return 2
        args.play_index = freqs.index(play_freq)

    if args.record:
        os.makedirs(args.record_dir, exist_ok=True)

    tb = silent_disco_rx(args, freqs, center)

    print("centre        %.4f MHz" % (center / 1e6))
    print("sample rate   %.3f Msps" % (args.samp_rate / 1e6))
    print("demod rate    %.1f kHz  (decimate %d)"
          % (tb.demod_rate / 1e3, tb.chan_decim))
    print("audio rate    %d Hz  (decimate %d%s)"
          % (AUDIO_RATE, tb.audio_decim,
             "" if tb.resamp is None else ", resample %d/%d"
             % (tb.resamp.numerator, tb.resamp.denominator)))
    print("channel taps  %d, %.0f kHz wide"
          % (len(tb.chan_taps), args.channel_bw / 1e3))
    print("demodulator   %s, de-emphasis %.0f us, %s"
          % ("stereo multiplex" if args.stereo else "mono (L+R)",
             args.deemph,
             "audio AGC" if args.agc else
             "deviation %.0f kHz" % (args.deviation / 1e3)))
    print("channels      %d demodulated concurrently" % len(tb.branches))
    for idx, freq in enumerate(freqs):
        marks = []
        if args.record:
            marks.append("record")
        if idx == args.play_index:
            marks.append("PLAY")
        print("  %2d  %9.4f MHz  %+8.1f kHz  %-12s %s"
              % (idx, freq / 1e6, (freq - center) / 1e3,
                 channel_label(freq), " ".join(marks) or "idle"))
    for freq in dropped:
        print("      %9.4f MHz  %-12s outside the capture"
              % (freq / 1e6, channel_label(freq)))

    try:
        tb.start()
        tb.wait()
    except KeyboardInterrupt:
        tb.stop()
        tb.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
