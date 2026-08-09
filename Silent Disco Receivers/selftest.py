#!/usr/bin/env python3
"""Lightweight self-test for the multi-channel Silent Disco receiver.

This does not attempt exhaustive RF validation.  Instead it verifies that

  * the flowgraph can be constructed with a small synthetic capture,
  * all configured channels run to completion, and
  * the expected WAV files are produced.

The synthetic capture is just complex Gaussian noise; the goal is to keep
this script fast and dependency-free beyond GNU Radio and NumPy.  For
serious validation against real silent-disco transmitters, point
silent_disco_rx.py at an IQ recording or live SDR.
"""

import os
import tempfile
from pathlib import Path

import numpy as np

from gnuradio import blocks, gr

import silent_disco_rx


class _NoiseCapture(gr.top_block):
    """Generate a short complex64 noise capture on disk."""

    def __init__(self, *, samp_rate: float, num_samples: int, path: str) -> None:
        super().__init__("silent_disco_selftest_noise")

        rng = np.random.default_rng()
        iq = (rng.standard_normal(num_samples) + 1j * rng.standard_normal(num_samples)).astype(
            np.complex64,
        )

        # Write once using a vector source feeding a file sink; this keeps
        # formats identical to what silent_disco_rx.py expects.
        src = blocks.vector_source_c(iq.tolist(), False)
        sink = blocks.file_sink(gr.sizeof_gr_complex, path)
        self.connect(src, sink)



def run_smoke_test() -> None:
    """End-to-end smoke test that builds the receiver and runs it briefly."""

    samp_rate = 2.4e6
    num_samples = int(0.25 * samp_rate)  # 250 ms of noise

    with tempfile.TemporaryDirectory(prefix="silent_disco_selftest_") as tmpdir:
        tmp = Path(tmpdir)
        iq_path = str(tmp / "noise.c64")

        # Generate synthetic capture.
        ng = _NoiseCapture(samp_rate=samp_rate, num_samples=num_samples, path=iq_path)
        ng.start()
        ng.wait()

        # Use a tiny subset of the Ultra 900 plan so the test is fast.
        plan = silent_disco_rx.ULTRA900
        ch_names = ["CH1", "CH3", "CH5"]
        channels = [(name, plan.channels[name]) for name in ch_names]
        center_hz = sum(f for _, f in channels) / len(channels)

        tb = silent_disco_rx.MultiChannelSilentDisco(
            center_freq_hz=center_hz,
            samp_rate=samp_rate,
            channel_map=channels,
            iq_file=iq_path,
            out_prefix=str(tmp / "test"),
        )

        tb.start()
        tb.wait()

        # Confirm that a WAV file was produced per requested channel.
        missing = []
        for name in ch_names:
            wav = tmp / f"test_{name}.wav"
            if not wav.exists() or wav.stat().st_size == 0:
                missing.append(str(wav))

        if missing:
            raise SystemExit(
                "self-test failed: expected WAV files not created: " + ", ".join(missing),
            )

        print("self-test passed: ", ", ".join(f"test_{n}.wav" for n in ch_names))


if __name__ == "__main__":  # pragma: no cover
    run_smoke_test()
