#!/usr/bin/env python3
"""
silent_disco_multi.py - Simultaneous multi-channel Silent Disco Receiver

Usage:
    python3 silent_disco_multi.py
    (records CH1-CH5 simultaneously to CH1.wav ... CH5.wav until Ctrl+C)
"""

import sys
import signal

from gnuradio import gr, analog, filter, blocks

try:
    import osmosdr
except ImportError:
    from gnuradio import osmosdr

# Channels to monitor simultaneously (must all fit within samp_rate
# of the chosen center_freq below).
CHANNELS = {
    "CH1": 925.601e6, #925601.000
    "CH2": 926.719e6, #926719.000
    "CH3": 924.500e6,  #924500.000
    "CH4": 922.417e6, #922417.000
    "CH5": 925.306e6 #925306.000
}

CENTER_FREQ = 921.2e6   # tune HackRF here; covers all channels above
SAMP_RATE = 4e6         # wide capture, decimated per-branch below


class ChannelBranch(gr.hier_block2):
    """One channel: shift to baseband, filter, WBFM demod, record to WAV."""

    def __init__(self, name, offset_freq, samp_rate):
        gr.hier_block2.__init__(
            self, f"branch_{name}",
            gr.io_signature(1, 1, gr.sizeof_gr_complex),
            gr.io_signature(0, 0, 0),
        )

        decim = 20
        quad_rate = samp_rate / decim  # 200 kHz into the demod

        taps = filter.firdes.low_pass(1.0, samp_rate, 100e3, 20e3)
        xlate = filter.freq_xlating_fir_filter_ccc(
            decim, taps, offset_freq, samp_rate
        )

        wbfm = analog.wfm_rcv(quad_rate=quad_rate, audio_decimation=5)
        audio_rate = int(quad_rate / 5)  # 40 kHz

        amp = blocks.multiply_const_ff(5.0)
        wav = blocks.wavfile_sink(f"{name}.wav", 1, audio_rate, 16)

        self.connect(self, xlate, wbfm, amp, wav)


class SilentDiscoMultiRX(gr.top_block):
    def __init__(self, channels, center_freq, samp_rate):
        gr.top_block.__init__(self, "Silent Disco Multi-Channel RX")

        self.src = osmosdr.source(args="hackrf,nchan=1")
        self.src.set_sample_rate(samp_rate)
        self.src.set_center_freq(center_freq)
        self.src.set_gain(16, "LNA")
        self.src.set_if_gain(14)
        self.src.set_bb_gain(0)

        for name, freq in channels.items():
            offset = freq - center_freq
            branch = ChannelBranch(name, offset, samp_rate)
            self.connect(self.src, branch)


def main():
    print(f"[*] Center freq: {CENTER_FREQ/1e6:.1f} MHz, capture {SAMP_RATE/1e6:.1f} MSPS")
    print(f"[*] Recording {len(CHANNELS)} channels simultaneously: {', '.join(CHANNELS)}")
    print("[*] Ctrl+C to stop\n")

    tb = SilentDiscoMultiRX(CHANNELS, CENTER_FREQ, SAMP_RATE)

    def stop(sig, frame):
        print("\n[*] Stopping, flushing WAV files...")
        tb.stop()
        tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    tb.start()
    signal.pause()


if __name__ == "__main__":
    main()
