#!/usr/bin/env python3
"""
silent_disco.py - Silent Disco Receiver for RFCTF

Tunes a HackRF One to a Silent Disco channel and plays the demodulated
WBFM audio live. Channel selection through argument choice when running
silent_disco.py.

Usage:
    python3 silent_disco.py CH1
    python3 silent_disco.py CH2
"""

import sys
import signal

from gnuradio import gr, analog, filter, blocks, audio

try:
    import osmosdr
except ImportError:
    from gnuradio import osmosdr

# At least two known Silent Disco channels (center freq in Hz)
# Confirmed live on-site via gqrx.
CHANNELS = {
    "CH1": 925.601e6, #925601.000
    "CH2": 926.719e6, #926719.000
    "CH3": 924.500e6  #924500.000
}


class SilentDiscoRX(gr.top_block):
    """HackRF -> channel filter -> WBFM demod -> soundcard."""

    def __init__(self, freq, lna_gain=16, vga_gain=14, audio_gain=5.0):
        gr.top_block.__init__(self, "Silent Disco RX")

        samp_rate = 2.4e6
        rf_decim = 10
        quad_rate = samp_rate / rf_decim         # 240 kHz into the demod
        audio_decim = 5

        self.src = osmosdr.source(args="hackrf,nchan=1")
        self.src.set_sample_rate(samp_rate)
        self.src.set_center_freq(freq)
        self.src.set_gain(lna_gain, "LNA")
        self.src.set_if_gain(vga_gain)
        self.src.set_bb_gain(0)

        taps = filter.firdes.low_pass(1.0, samp_rate, 100e3, 20e3)
        chan_filter = filter.fir_filter_ccf(rf_decim, taps)

        wbfm = analog.wfm_rcv(quad_rate=quad_rate, audio_decimation=audio_decim)

        amp = blocks.multiply_const_ff(audio_gain)
        sink = audio.sink(int(quad_rate / audio_decim), "", True)

        self.connect(self.src, chan_filter, wbfm, amp, sink)


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in CHANNELS:
        sys.exit(f"Usage: {sys.argv[0]} <{'|'.join(CHANNELS)}>")

    channel = sys.argv[1]
    freq = CHANNELS[channel]
    print(f"[*] Tuning to {channel} ({freq/1e6:.3f} MHz) - Ctrl+C to stop")

    tb = SilentDiscoRX(freq)

    def stop(sig, frame):
        tb.stop()
        tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    tb.start()
    signal.pause()


if __name__ == "__main__":
    main()