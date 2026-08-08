#!/usr/bin/env python3
"""iqdump_analyze.py -- spectrum analysis of a raw int8 I/Q dump
(hackrf transfer layout: interleaved int8 I, Q).

Reads a raw capture taken at 20 MSps around LO and prints the noise floor,
the strongest spectral peaks (with RF frequencies), and the signal/noise in
a +/-250 kHz window around an optional target channel.

Usage: iqdump_analyze.py <dump.raw> <lo_hz> [chan_freq_hz]
"""
import sys
import numpy as np

FS_IN = 20e6


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    path = sys.argv[1]
    lo = float(sys.argv[2])
    target = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0

    data = np.fromfile(path, dtype=np.int8)
    if data.size % 2:
        data = data[:-1]
    iq = data[0::2].astype(np.float32) + 1j * data[1::2].astype(np.float32)
    ns = iq.size
    print(f"file: {ns} samples ({ns / FS_IN:.3f}s @20MS/s)  LO={lo/1e6:.3f} MHz")

    nfft = min(ns, 1 << 20)
    start = max(0, (ns - nfft) // 2)
    seg = iq[start:start + nfft] / 128.0
    seg = seg - np.mean(seg)
    win = np.hanning(nfft)
    S = np.fft.fftshift(np.fft.fft(seg * win))
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / FS_IN))
    pwr = np.abs(S) ** 2
    pdb = 10 * np.log10(pwr + 1e-30)

    floor = np.percentile(pdb, 10)
    print(f"noise floor: {floor:.1f} dB   peak: {pdb.max():.1f} dB")

    thresh = floor + 6
    shown = 0
    print(f"\n--- peaks >= {thresh:.1f} dB ---")
    for i in range(1, nfft - 1):
        if pdb[i] >= thresh and pdb[i] >= pdb[i - 1] and pdb[i] >= pdb[i + 1]:
            rf = lo + freqs[i]
            print(f"  {rf/1e6:9.3f} MHz  (baseband {freqs[i]/1e6:+7.2f})  {pdb[i]:6.1f} dB")
            shown += 1
            if shown >= 40:
                break

    if target:
        off = target - lo
        half = 250_000
        near = np.abs(freqs - off) <= half
        sig = np.max(pdb[near]) if np.any(near) else floor
        far = np.abs(np.abs(freqs - off) - 500_000) <= half
        flr = np.percentile(pdb[far], 50)
        print(f"\nchannel {target/1e6:.3f} MHz (offset {off/1e6:+.3f}): "
              f"peak {sig:.1f} dB, local noise {flr:.1f} dB, margin {sig-flr:.1f} dB")


if __name__ == "__main__":
    sys.exit(main())