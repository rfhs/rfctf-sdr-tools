#!/usr/bin/env python3
#
# selftest.py - prove silent_disco_rx.py really demodulates, with no radio.
#
# Generates a synthetic multi-carrier silent disco signal, runs the receiver
# over it, and checks that each WAV file holds the tones that were put on that
# carrier and nothing else.
#
# It also runs two negative controls, because a test that only ever passes is
# not a test:
#
#   noise only     no carriers at all, just AWGN. Must FAIL.
#   mistuned       real carriers, receiver pointed 500 kHz off. Must FAIL.
#
# If the negative controls pass, the pass threshold is sitting in the noise
# and the positive results mean nothing.
#
# SPDX-License-Identifier: BSD-3-Clause

import glob
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
from scipy.io import wavfile

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, "make_silent_disco_iq.py")
RX = os.path.join(HERE, "silent_disco_rx.py")

# Must match make_silent_disco_iq.py.
TONES = [400.0, 700.0, 1100.0, 1700.0, 2300.0, 2900.0, 3700.0, 4300.0]

# A real carrier lands 60 dB or more over the in-band noise floor. The two
# negative controls land under 18 dB. 30 dB sits clear of both.
MIN_SNR_DB = 30.0
MIN_ISOLATION_DB = 30.0
# Left and right must actually differ. Real hardware manages far more than
# this; the threshold only has to catch a collapse to mono.
MIN_SEPARATION_DB = 20.0

# The generator gates the audio between 1.0 and 0.1, so the source has
# 20 dB of dynamic range. A 2:1 compressor must halve that and the
# expander must put it back. Tolerances are loose because the envelope
# follower has lag and the estimate is a percentile, not an oracle.
COMPAND_SOURCE_DB = 20.0
COMPAND_TOL_DB = 3.0


def run(cmd):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        print(proc.stdout)
        raise SystemExit("command failed: %s" % " ".join(cmd))
    return proc.stdout


def tone_snr(path):
    """Return (sample_rate, [(track_name, {tone_hz: snr_db})]) for a WAV.

    Every carrier's left tone and its right tone (twice the left) is measured,
    so a channel that ends up in the wrong file, or a left/right swap, shows
    up as a level in a slot that should be empty.
    """
    fs, data = wavfile.read(path)
    if data.ndim == 1:
        tracks = [("mono", data.astype(np.float64) / 32768.0)]
    else:
        tracks = [("left", data[:, 0].astype(np.float64) / 32768.0),
                  ("right", data[:, 1].astype(np.float64) / 32768.0)]

    probes = sorted(set(TONES) | set(2 * t for t in TONES))
    out = []
    for name, x in tracks:
        # Skip the first half: filter transients and AGC settling live there.
        x = x[len(x) // 2:]
        n = 1 << int(np.floor(np.log2(max(len(x), 2))))
        if n < 4096:
            out.append((name, {}))
            continue
        spec = np.abs(np.fft.rfft(x[:n] * np.hanning(n)))
        faxis = np.fft.rfftfreq(n, 1.0 / fs)
        band = (faxis > 100) & (faxis < 15000)
        floor = np.median(spec[band])
        levels = {}
        for t in probes:
            m = (faxis > t - 30) & (faxis < t + 30)
            if m.any():
                levels[t] = 20 * np.log10(spec[m].max() / max(floor, 1e-12))
        out.append((name, levels))
    return fs, out


def check(directory, label, expect_pass, nchan=None):
    paths = sorted(glob.glob(os.path.join(directory, "*.wav")))
    if not paths:
        print("  %s: no WAV files produced" % label)
        return not expect_pass

    count = nchan if nchan is not None else len(paths)
    print("  %s: %d files" % (label, len(paths)))
    all_good = True
    for idx, path in enumerate(paths):
        own = TONES[idx]
        pair = 2 * own
        fs, tracks = tone_snr(path)
        for name, levels in tracks:
            if not levels:
                all_good = False
                continue
            # In stereo, left should hold the tone and right should hold
            # twice it. In mono both arrive together as L+R.
            if name == "left":
                wanted, unwanted_own = [own], [pair]
            elif name == "right":
                wanted, unwanted_own = [pair], [own]
            else:
                wanted, unwanted_own = [own, pair], []

            mine = max(levels.get(t, -300.0) for t in wanted)
            foreign = []
            for j in range(count):
                if j == idx:
                    continue
                foreign.append(levels.get(TONES[j], -300.0))
                foreign.append(levels.get(2 * TONES[j], -300.0))
            worst_other = max(foreign) if foreign else -300.0
            good = (mine >= MIN_SNR_DB
                    and mine - worst_other >= MIN_ISOLATION_DB)
            # The opposite track was previously printed but never checked, so a
            # regression collapsing L and R into the same audio would still
            # have passed. Require real separation when decoding stereo.
            if unwanted_own:
                sep = mine - levels.get(unwanted_own[0], -300.0)
                good &= sep >= MIN_SEPARATION_DB
            all_good &= good
            extra = ""
            if unwanted_own:
                extra = "  opposite track %6.1f dB" % levels.get(
                    unwanted_own[0], -300.0)
            print("    ch%d %-5s own %6.1f dB   next best other %6.1f dB%s   %s"
                  % (idx, name, mine, worst_other, extra,
                     "ok" if good else "no"))

    verdict = (all_good == expect_pass)
    print("  %s: %s (expected %s) -> %s"
          % (label, "PASS" if all_good else "FAIL",
             "PASS" if expect_pass else "FAIL",
             "correct" if verdict else "WRONG"))
    return verdict


def dynamic_range_db(path):
    """Loud-to-quiet ratio in dB, from short-window RMS percentiles."""
    fs, data = wavfile.read(path)
    a = (data if data.ndim == 1 else data[:, 0]).astype(np.float64) / 32768.0
    a = a[len(a) // 6:]                       # drop the envelope settling
    win = max(1, int(0.05 * fs))
    nw = len(a) // win
    if nw < 8:
        return 0.0
    r = np.sqrt(np.mean(a[:nw * win].reshape(nw, win) ** 2, axis=1)) + 1e-12
    return 20.0 * np.log10(np.percentile(r, 90) / np.percentile(r, 10))


def check_compander(tmp):
    """The expander must invert the compressor, and matter when absent."""
    plain = os.path.join(tmp, "compand_plain.cf32")
    comp = os.path.join(tmp, "compand_comp.cf32")
    gen = [sys.executable, GEN, "--samp-rate", "2e6", "--offsets", "-400000",
           "--tones", "400", "--seconds", "8.0", "--mono", "--noise", "0.001",
           "--dynamics", "2", "--quiet-level", "0.1"]
    run(gen + ["--out", plain])
    run(gen + ["--compand", "--out", comp])

    rx = [sys.executable, RX, "--samp-rate", "2e6", "--center", "922.4e6",
          "--freqs", "922.0", "--record", "--no-agc", "--record-dir"]
    out = {}
    for label, src, extra in (("uncompanded, no expander", plain, []),
                              ("companded, no expander", comp, []),
                              ("companded, with expander", comp, ["--expander"])):
        d = os.path.join(tmp, "cp_" + label.split(",")[0].replace(" ", "_")
                         + ("_exp" if extra else ""))
        run(rx + [d, "--source", "file:" + src] + extra)
        wavs = sorted(glob.glob(os.path.join(d, "*.wav")))
        out[label] = dynamic_range_db(wavs[0]) if wavs else 0.0

    good = True
    for label, want in (("uncompanded, no expander", COMPAND_SOURCE_DB),
                        ("companded, no expander", COMPAND_SOURCE_DB / 2.0),
                        ("companded, with expander", COMPAND_SOURCE_DB)):
        got = out[label]
        hit = abs(got - want) <= COMPAND_TOL_DB
        good &= hit
        print("    %-26s %6.1f dB   want %4.1f   %s"
              % (label, got, want, "yes" if hit else "NO"))
    print("  compander: %s" % ("PASS" if good else "FAIL"))
    return good


def main():
    tmp = tempfile.mkdtemp(prefix="silent_disco_selftest_")
    seconds = "3.0"
    ok = True
    try:
        iq = os.path.join(tmp, "five.cf32")
        noise_iq = os.path.join(tmp, "noise.cf32")
        sparse_iq = os.path.join(tmp, "sparse.cf32")

        print("generating test signals")
        run([sys.executable, GEN, "--out", iq, "--seconds", seconds,
             "--noise", "0.05"])
        # Noise only: one carrier at zero amplitude leaves pure AWGN.
        run([sys.executable, GEN, "--out", noise_iq, "--seconds", seconds,
             "--offsets", "0", "--levels", "0.0", "--noise", "0.3"])
        # Three carriers 1 MHz apart, so that a receiver asked for the gaps
        # between them is a genuine 500 kHz mistune with nothing to find.
        run([sys.executable, GEN, "--out", sparse_iq, "--seconds", seconds,
             "--offsets=-1000e3,0,1000e3", "--noise", "0.05"])

        print("positive control: mono decode of five channels")
        d = os.path.join(tmp, "mono")
        run([sys.executable, RX, "--source", "file:" + iq, "--plan",
             "ultra900", "--record", "--record-dir", d])
        ok &= check(d, "mono five channels", True)

        print("positive control: stereo decode of five channels")
        d = os.path.join(tmp, "stereo")
        run([sys.executable, RX, "--source", "file:" + iq, "--plan",
             "ultra900", "--record", "--record-dir", d, "--stereo"])
        ok &= check(d, "stereo five channels", True)

        print("negative control: noise only, no carriers")
        d = os.path.join(tmp, "noise")
        run([sys.executable, RX, "--source", "file:" + noise_iq, "--plan",
             "ultra900", "--record", "--record-dir", d])
        ok &= check(d, "noise only", False)

        # Carriers sit at -1000, 0 and +1000 kHz; ask for the two gaps at
        # -500 and +500 kHz. That is a 500 kHz mistune, wider than the
        # tightest real channel spacing in the Ultra 900 plan.
        print("negative control: receiver mistuned 500 kHz")
        d = os.path.join(tmp, "mistuned")
        run([sys.executable, RX, "--source", "file:" + sparse_iq, "--freqs",
             "921.25,922.25", "--center", "921.75e6",
             "--record", "--record-dir", d])
        ok &= check(d, "mistuned 500 kHz", False, nchan=3)

        print("compander: expander must invert the 2:1 compressor")
        ok &= check_compander(tmp)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("selftest: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
