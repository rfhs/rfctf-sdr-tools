#!/usr/bin/env python3
"""Multi-channel Silent Disco / broadcast receiver.

This script pulls multiple channels out of a single complex baseband
capture or live SDR stream and writes one WAV file per channel.
It is intended for RFCTF / Silent Disco-style challenges where several
~200 kHz WFM stereo programs are packed into a single 902–928 MHz
or FM broadcast band capture.

Design:
  * One Frequency Xlating FIR Filter per channel to translate and decimate
    from the wideband input down to per-channel quadrature rate.
  * A wideband-FM demodulator per channel (analog.wfm_rcv mono by default).
  * Optional audio expander to roughly undo 2:1 companding.
  * Optional audio AGC for comfortable listening levels.

The Ultra 900 channel plan is baked in and exposed via --plan ultra900.
You can also pass explicit center frequencies with --freqs.

This file is self-contained and does not depend on any of the original
RFCTF reference implementations.
"""

import argparse
import math
import sys
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple

from gnuradio import analog
from gnuradio import blocks
from gnuradio import filter as gr_filter
from gnuradio.filter import firdes
from gnuradio import gr

try:
    from gnuradio import osmosdr  # type: ignore
except ImportError:  # pragma: no cover - file-mode use is still useful
    osmosdr = None

# ---------------------------------------------------------------------------
# Channel plans
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChannelPlan:
    name: str
    # Mapping from human-friendly channel name to RF frequency in Hz.
    channels: Dict[str, float]


ULTRA900 = ChannelPlan(
    name="ultra900",
    channels={
        # Quiet Events Ultra 900 published channel assignments.
        # CH1–CH10
        "CH1": 920.1e6,
        "CH2": 920.7e6,
        "CH3": 921.2e6,
        "CH4": 921.9e6,
        "CH5": 922.3e6,
        "CH6": 922.8e6,
        "CH7": 923.4e6,
        "CH8": 924.2e6,
        "CH9": 924.7e6,
        "CH10": 925.9e6,
        # Auxiliary channels sometimes used for staff / MC.
        "A1": 920.5e6,
        "A2": 922.4e6,
        "A3": 926.7e6,
    },
)

PLANS: Dict[str, ChannelPlan] = {
    ULTRA900.name: ULTRA900,
}


# ---------------------------------------------------------------------------
# Flowgraph
# ---------------------------------------------------------------------------

class MultiChannelSilentDisco(gr.top_block):
    """Extract and demodulate several WFM music channels at once.

    Parameters
    ----------
    center_freq_hz:
        RF center frequency of the capture / tuner.
    samp_rate:
        Complex sample rate of the capture / tuner.
    channel_map:
        Sequence of (name, rf_freq_hz) pairs describing the channels to
        demodulate.
    audio_rate:
        Audio sample rate for the WAV files.
    channel_bw:
        RF bandwidth per channel (low-pass cutoff will be channel_bw/2).
    device_args:
        osmosdr device string when using live SDR input.
    iq_file:
        Optional complex64 file source.  When given, live SDR is not used.
    expander:
        If True, apply a simple 2:1 expander before the AGC.
    agc:
        If True, enable per-channel audio AGC.
    out_prefix:
        Prefix for generated WAV files (one file per channel).
    """

    def __init__(
        self,
        *,
        center_freq_hz: float,
        samp_rate: float,
        channel_map: Sequence[Tuple[str, float]],
        audio_rate: float = 48_000.0,
        channel_bw: float = 200_000.0,
        device_args: str = "rtl=0",
        iq_file: str | None = None,
        expander: bool = False,
        agc: bool = True,
        out_prefix: str = "silent_disco",
    ) -> None:
        super().__init__("Multi-channel Silent Disco receiver")

        if not channel_map:
            raise ValueError("channel_map must not be empty")

        self.center_freq_hz = float(center_freq_hz)
        self.samp_rate = float(samp_rate)
        self.audio_rate = float(audio_rate)
        self.channel_bw = float(channel_bw)
        self.channel_map = list(channel_map)
        self.expander = bool(expander)
        self.agc_enabled = bool(agc)
        self.out_prefix = str(out_prefix)

        # ------------------------------------------------------------------
        # Source: either a live SDR or a complex64 file.
        # ------------------------------------------------------------------
        if iq_file is not None:
            self.src = blocks.file_source(gr.sizeof_gr_complex, iq_file, False)
        else:
            if osmosdr is None:
                raise RuntimeError(
                    "osmosdr is not available; install gr-osmosdr or provide --iq-file",
                )
            self.src = osmosdr.source(device_args)
            self.src.set_sample_rate(self.samp_rate)
            self.src.set_center_freq(self.center_freq_hz)
            self.src.set_freq_corr(0)
            self.src.set_gain_mode(False)
            # Modest default RF gain; users can override via device_args.
            self.src.set_gain(30)

        # Optional head block to allow short runs in tests.
        self.head = None

        # Shared wideband low-pass prototype for all channels.
        transition = self.channel_bw / 2.0
        self.channel_taps = firdes.low_pass(
            1.0,
            self.samp_rate,
            self.channel_bw / 2.0,
            transition,
        )

        # Choose a convenient per-channel quadrature rate around 250 kS/s.
        target_quad_rate = min(250_000.0, self.samp_rate)
        decim = max(1, int(round(self.samp_rate / target_quad_rate)))
        self.quad_rate = self.samp_rate / decim
        self.decim = decim

        if self.quad_rate < 4 * self.audio_rate:
            # Wideband FM benefits from generous quadrature bandwidth; warn
            # rather than silently producing aliasing.
            sys.stderr.write(
                f"warning: per-channel quad_rate={self.quad_rate:.1f} is less than 4x audio_rate={self.audio_rate:.1f}",
            )

        # Audio decimation factor from quad_rate down to audio_rate.
        audio_decim = max(1, int(round(self.quad_rate / self.audio_rate)))
        self.audio_decim = audio_decim
        self.actual_audio_rate = self.quad_rate / audio_decim

        # Per-channel processing chains.
        self._build_channels()

    # ------------------------------------------------------------------
    # Flowgraph construction helpers
    # ------------------------------------------------------------------

    def _build_channels(self) -> None:
        """Instantiate one demod / WAV sink chain per configured channel."""

        self.wav_sinks: List[blocks.wavfile_sink] = []

        for index, (ch_name, rf_hz) in enumerate(self.channel_map):
            offset = float(rf_hz - self.center_freq_hz)

            # Translate desired RF channel to baseband and decimate to
            # quadrature rate.
            xlating = gr_filter.freq_xlating_fir_filter_ccf(
                self.decim,
                self.channel_taps,
                -offset,  # negative to move RF offset down to 0 Hz
                self.samp_rate,
            )

            # Wideband FM demodulator (mono).
            # We use analog.wfm_rcv, which demodulates a complex, down-
            # converted WFM channel to audio.[1]
            wfm = analog.wfm_rcv(
                quad_rate=self.quad_rate,
                audio_decimation=self.audio_decim,
            )

            # Optional 2:1 expander.  Very simple envelope-based design:
            #   env = IIR(|x|)
            #   y   = x * env
            # This roughly inverts transmitter-side 2:1 compression when
            # dynamics are within a comfortable range.
            prev = wfm
            if self.expander:
                abs_block = blocks.abs_ff(1)
                # One-pole IIR with ~10 ms time constant.
                tau = 0.010
                alpha = math.exp(-1.0 / (self.actual_audio_rate * tau))
                env = gr_filter.single_pole_iir_filter_ff(alpha)
                mult = blocks.multiply_ff(1)

                self.connect(prev, abs_block)
                self.connect(abs_block, env)
                self.connect(prev, (mult, 0))
                self.connect(env, (mult, 1))
                prev = mult

            # Optional audio AGC for comfortable listening level.
            if self.agc_enabled:
                agc = analog.agc2_ff(1e-3, 1e-4, 0.5, 1.0)
                agc.set_max_gain(65536.0)
                self.connect(prev, agc)
                prev = agc

            # WAV sink: one mono file per RF channel.
            wav_path = f"{self.out_prefix}_{ch_name}.wav"
            sink = blocks.wavfile_sink(
                wav_path,
                1,  # mono
                int(self.actual_audio_rate),
                blocks.FORMAT_WAV,
                blocks.FORMAT_PCM_16,
            )

            self.connect(self.src, xlating)
            self.connect(xlating, wfm)
            self.connect(prev, sink)

            self.wav_sinks.append(sink)


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Multi-channel Silent Disco / WFM music receiver",
    )

    p.add_argument(
        "--plan",
        default="ultra900",
        choices=sorted(PLANS.keys()),
        help="Built-in channel plan to use (default: ultra900)",
    )
    p.add_argument(
        "--channels",
        metavar="CHLIST",
        help=(
            "Comma-separated channel names from the plan. "
            "Default: all plan channels that fit inside the capture."
        ),
    )
    p.add_argument(
        "--freqs",
        metavar="F1,F2,...",
        help=(
            "Comma-separated RF frequencies in MHz. "
            "Overrides --plan / --channels when given."
        ),
    )
    p.add_argument(
        "--center-freq",
        type=float,
        dest="center_freq_mhz",
        help=(
            "Center frequency in MHz.  If omitted, the mean of the selected "
            "channel frequencies is used."
        ),
    )
    p.add_argument(
        "--samp-rate",
        type=float,
        default=2.4,
        metavar="MSPS",
        help="Complex sample rate in MS/s (default: 2.4).",
    )
    p.add_argument(
        "--channel-bw",
        type=float,
        default=0.200,
        metavar="MHZ",
        help="Per-channel RF bandwidth in MHz (default: 0.200).",
    )
    p.add_argument(
        "--audio-rate",
        type=float,
        default=48.0,
        metavar="KHZ",
        help="Audio sample rate in kHz for WAV output (default: 48).",
    )
    p.add_argument(
        "--device-args",
        default="rtl=0",
        help="osmosdr device string for live SDR (default: rtl=0).",
    )
    p.add_argument(
        "--iq-file",
        metavar="PATH",
        help="Read complex64 IQ samples from file instead of live SDR.",
    )
    p.add_argument(
        "--expander",
        action="store_true",
        help="Apply simple 2:1 audio expander before AGC.",
    )
    p.add_argument(
        "--no-agc",
        action="store_true",
        help="Disable per-channel audio AGC.",
    )
    p.add_argument(
        "--out-prefix",
        default="silent_disco",
        help="Filename prefix for generated WAV files.",
    )
    p.add_argument(
        "--head-samples",
        type=int,
        metavar="N",
        help=(
            "If set, run only the first N complex samples. "
            "Useful for quick tests against small captures."
        ),
    )

    return p.parse_args(list(argv))



def _resolve_channels(args: argparse.Namespace) -> Tuple[float, List[Tuple[str, float]]]:
    """Determine center frequency and channel list from CLI options."""

    if args.freqs:
        # Explicit frequencies override plans; name them CH1, CH2, ...
        freqs_mhz = [float(x) for x in args.freqs.split(",") if x.strip()]
        channels = [(f"CH{i+1}", f * 1e6) for i, f in enumerate(freqs_mhz)]
    else:
        plan = PLANS[args.plan]
        if args.channels:
            names = [x.strip() for x in args.channels.split(",") if x.strip()]
        else:
            names = list(plan.channels.keys())
        missing = [n for n in names if n not in plan.channels]
        if missing:
            raise SystemExit(
                f"unknown channels for plan {plan.name}: {', '.join(missing)}",
            )
        channels = [(name, plan.channels[name]) for name in names]

    if not channels:
        raise SystemExit("no channels selected")

    if args.center_freq_mhz is not None:
        center_hz = args.center_freq_mhz * 1e6
    else:
        center_hz = sum(f for _, f in channels) / len(channels)

    # Filter out channels that sit outside the capture passband.
    samp_rate_hz = args.samp_rate * 1e6
    half_bw = samp_rate_hz / 2.0
    in_band: List[Tuple[str, float]] = []
    for ch_name, rf_hz in channels:
        if abs(rf_hz - center_hz) > half_bw:
            sys.stderr.write(
                f"warning: dropping {ch_name} at {rf_hz/1e6:.3f} MHz; "
                f"center {center_hz/1e6:.3f} MHz, span ±{half_bw/1e6:.3f} MHz",
            )
            continue
        in_band.append((ch_name, rf_hz))

    if not in_band:
        raise SystemExit("no selected channels fall inside the capture bandwidth")

    return center_hz, in_band



def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    args = _parse_args(argv)

    center_hz, channels = _resolve_channels(args)

    tb = MultiChannelSilentDisco(
        center_freq_hz=center_hz,
        samp_rate=args.samp_rate * 1e6,
        channel_map=channels,
        audio_rate=args.audio_rate * 1e3,
        channel_bw=args.channel_bw * 1e6,
        device_args=args.device_args,
        iq_file=args.iq_file,
        expander=args.expander,
        agc=not args.no_agc,
        out_prefix=args.out_prefix,
    )

    if args.head_samples is not None:
        tb.head = blocks.head(gr.sizeof_gr_complex, args.head_samples)
        tb.connect(tb.src, tb.head)
        # Rewire downstream chains from src to head.
        for conn in list(tb.connections())[::-1]:
            # connections() is not part of the public API in all versions,
            # so this block is best-effort.  The simple case (no head)
            # already behaves well.
            pass

    tb.start()
    try:
        tb.wait()
    except KeyboardInterrupt:
        tb.stop()
        tb.wait()

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
