#!/usr/bin/env python3
"""Find the silent disco carriers on the air, before trying to demodulate them.

The receiver takes a channel plan on faith. Our plans are reconstructed from
vendor documentation, not measured off the DEF CON system, so if the real
transmitters sit anywhere else `--plan ultra900` produces silence and says
nothing about why. This sweeps the band, reports every carrier it actually
finds, and prints the `--freqs` string that matches them.

Run this FIRST at the table. It answers "is the system even on, and where".

    ./survey.py --source hackrf --samp-rate 8e6
    ./survey.py --source hackrf --start 920e6 --stop 925e6     # colours only
    ./survey.py --source file:capture.cf32 --center 922.4e6    # a recording

Receive only. This program does not transmit.
"""

import argparse
import sys

import numpy as np

# --- the plans the receiver ships, duplicated deliberately -------------------
# Importing silent_disco_rx would drag in the whole GNU Radio demodulator chain
# and couple this tool to it. The point of a survey is to work when the
# receiver does not.
COLOURS = {
    920.1e6: "1 red", 920.7e6: "2 yellow", 921.2e6: "3 green",
    921.9e6: "4 purple", 922.3e6: "5 blue", 922.8e6: "6 turquoise",
    923.4e6: "7 white", 924.2e6: "8 orange", 924.7e6: "9 pink",
}


def _soapy_source(soapy, driver, device_args):
    """Open a SoapySDR source, tolerating drivers that reject bufflen.

    bufflen is an RTL-SDR stream argument. UHD rejects it outright with
    "Unsupported stream argument bufflen for channel 0", so a USRP could not
    be opened at all. Ask for the larger buffer, and fall back to the driver's
    own default when it is not understood.
    """
    try:
        return soapy.source("driver=%s" % driver, "fc32", 1,
                            device_args, "bufflen=16384", [""], [""])
    except Exception:
        return soapy.source("driver=%s" % driver, "fc32", 1,
                            device_args, "", [""], [""])


def capture(source, device_args, samp_rate, center, gain, hw_agc, ppm, nsamp):
    """Grab nsamp complex samples at one tuning. Returns a complex64 array."""
    from gnuradio import gr, blocks

    tb = gr.top_block()
    if str(source).startswith("file:"):
        src = blocks.file_source(gr.sizeof_gr_complex, source[5:], False)
    else:
        from gnuradio import soapy
        src = _soapy_source(soapy, source, device_args)
        src.set_sample_rate(0, samp_rate)
        src.set_frequency(0, center)
        src.set_frequency_correction(0, ppm)
        src.set_gain_mode(0, hw_agc)
        if not hw_agc:
            try:
                src.set_gain(0, "TUNER", gain)
            except Exception:
                src.set_gain(0, gain)

    head = blocks.head(gr.sizeof_gr_complex, int(nsamp))
    sink = blocks.vector_sink_c()
    tb.connect(src, head, sink)
    tb.run()
    return np.array(sink.data(), dtype=np.complex64)


def spectrum(x, samp_rate, nfft):
    """Welch PSD in dB, DC-centred, with the matching frequency offsets."""
    if len(x) < nfft:
        return None, None
    win = np.hanning(nfft)
    nseg = len(x) // nfft
    acc = np.zeros(nfft)
    for i in range(nseg):
        seg = x[i * nfft:(i + 1) * nfft] * win
        acc += np.abs(np.fft.fft(seg)) ** 2
    acc /= nseg
    psd = 10.0 * np.log10(np.fft.fftshift(acc) + 1e-20)
    off = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / samp_rate))
    return off, psd


def find_carriers(off, psd, floor, threshold_db, min_sep_hz):
    """Local maxima standing threshold_db above the noise floor.

    An FM carrier is a broad hump, not a spike, so this takes the strongest
    bin in each contiguous run above the threshold rather than every bin.
    """
    hot = psd > (floor + threshold_db)
    out = []
    i = 0
    while i < len(hot):
        if not hot[i]:
            i += 1
            continue
        j = i
        while j < len(hot) and hot[j]:
            j += 1
        run = slice(i, j)
        k = i + int(np.argmax(psd[run]))
        out.append((off[k], psd[k] - floor))
        i = j
    # Merge anything closer together than a channel can be.
    out.sort(key=lambda t: t[0])
    merged = []
    for f, snr in out:
        if merged and abs(f - merged[-1][0]) < min_sep_hz:
            if snr > merged[-1][1]:
                merged[-1] = (f, snr)
        else:
            merged.append((f, snr))
    return merged


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Sweep for silent disco carriers and report what is "
                    "actually transmitting.",
        epilog="Receive only. This program does not transmit.")
    p.add_argument("--source", default="hackrf",
                   help="SoapySDR driver, or file:PATH for a complex float32 "
                        "capture (default hackrf)")
    p.add_argument("--device-args", default="",
                   help="extra SoapySDR device arguments")
    p.add_argument("--samp-rate", type=float, default=8e6,
                   help="capture sample rate in Hz (default 8e6)")
    p.add_argument("--start", type=float, default=902e6,
                   help="low edge of the sweep in Hz (default 902e6)")
    p.add_argument("--stop", type=float, default=928e6,
                   help="high edge of the sweep in Hz (default 928e6)")
    p.add_argument("--center", type=float, default=None,
                   help="single tuning in Hz; required for a file source, "
                        "and skips the sweep for a radio")
    p.add_argument("--gain", type=float, default=40.0, help="RF gain in dB")
    p.add_argument("--hw-agc", action="store_true",
                   help="enable the tuner's own AGC")
    p.add_argument("--ppm", type=float, default=0.0,
                   help="frequency correction in PPM")
    p.add_argument("--dwell", type=float, default=0.25,
                   help="seconds of samples per tuning (default 0.25)")
    p.add_argument("--nfft", type=int, default=4096, help="FFT size")
    p.add_argument("--threshold", type=float, default=8.0,
                   help="dB above the noise floor to call a carrier "
                        "(default 8)")
    p.add_argument("--dc-guard", type=float, default=60e3,
                   help="ignore this much either side of the tuner centre, "
                        "where the DC spike lives (default 60e3)")
    p.add_argument("--usable", type=float, default=0.75,
                   help="fraction of the sample rate to trust, the rest is "
                        "filter roll-off (default 0.75)")
    args = p.parse_args(argv)

    is_file = str(args.source).startswith("file:")
    if is_file and args.center is None:
        print("error: a file has no idea what it was tuned to. Give --center "
              "with the capture's true centre frequency in Hz.",
              file=sys.stderr)
        return 2

    usable = args.samp_rate * args.usable
    if args.center is not None:
        tunings = [args.center]
    else:
        # Overlap the tiles so a carrier landing on a seam is not missed.
        step = usable * 0.8
        n = max(1, int(np.ceil((args.stop - args.start) / step)))
        tunings = [args.start + usable / 2 + i * step for i in range(n)]

    print("sweeping %.1f - %.1f MHz at %.1f Msps, %d tuning(s), %.2fs each"
          % (args.start / 1e6, args.stop / 1e6, args.samp_rate / 1e6,
             len(tunings), args.dwell))

    found = []
    for center in tunings:
        try:
            x = capture(args.source, args.device_args, args.samp_rate, center,
                        args.gain, args.hw_agc, args.ppm,
                        args.samp_rate * args.dwell)
        except Exception as exc:
            print("  %.3f MHz: capture failed: %s" % (center / 1e6, exc),
                  file=sys.stderr)
            continue
        if len(x) == 0:
            print("  %.3f MHz: no samples. Is the radio connected and free?"
                  % (center / 1e6), file=sys.stderr)
            continue

        off, psd = spectrum(x, args.samp_rate, args.nfft)
        if off is None:
            print("  %.3f MHz: too few samples for a %d point FFT"
                  % (center / 1e6, args.nfft), file=sys.stderr)
            continue

        keep = (np.abs(off) < usable / 2) & (np.abs(off) > args.dc_guard)
        off, psd = off[keep], psd[keep]
        floor = float(np.median(psd))
        for f_off, snr in find_carriers(off, psd, floor, args.threshold, 150e3):
            f = center + f_off
            if args.start - 1e6 <= f <= args.stop + 1e6:
                found.append((f, snr))

    # A carrier seen in two overlapping tiles is one carrier.
    found.sort(key=lambda t: t[0])
    merged = []
    for f, snr in found:
        if merged and abs(f - merged[-1][0]) < 150e3:
            if snr > merged[-1][1]:
                merged[-1] = (f, snr)
        else:
            merged.append((f, snr))

    if not merged:
        print("\nNo carriers found.")
        print("The system may be off, out of range, or outside %.1f - %.1f MHz."
              % (args.start / 1e6, args.stop / 1e6))
        print("Try --gain 60, --hw-agc, a wider --start/--stop, or "
              "--threshold 5.")
        return 1

    print("\n%d carrier(s):\n" % len(merged))
    print("  %-14s %8s   %s" % ("frequency", "dB>floor", "nearest plan channel"))
    for f, snr in merged:
        best = min(COLOURS, key=lambda c: abs(c - f))
        delta = (f - best) / 1e3
        note = ("%-14s %+7.1f kHz off" % (COLOURS[best], delta)
                if abs(delta) < 200 else "no plan channel within 200 kHz")
        print("  %10.4f MHz %8.1f   %s" % (f / 1e6, snr, note))

    freqs = ",".join("%.4f" % (f / 1e6) for f, _ in merged)
    print("\nMeasured channel list, use it instead of --plan:\n")
    print("  --freqs %s\n" % freqs)
    if len(merged) >= 5:
        span = (merged[-1][0] - merged[0][0]) / 1e6
        print("  %d channels spanning %.2f MHz. To take them all at once the "
              "sample rate\n  must exceed that span: --samp-rate %.1fe6 or "
              "higher." % (len(merged), span, np.ceil(span / 0.75)))
    else:
        print("  Only %d carriers. Five are needed for the multichannel "
              "challenge - widen\n  the sweep or lower --threshold before "
              "concluding the rest are absent." % len(merged))
    return 0


if __name__ == "__main__":
    sys.exit(main())
