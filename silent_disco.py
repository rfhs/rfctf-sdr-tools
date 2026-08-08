#!/usr/bin/env python3
"""
silent_disco.py - Silent Disco Receiver for RFCTF
Monitors and plays audio from silent disco channels on a HackRF One.

Usage:
    python3 silent_disco.py <channel>
    python3 silent_disco.py --list
    python3 silent_disco.py A1

Channels:
    CH1=920.1, CH2=920.7, CH3=921.2, CH4=921.9, CH5=922.3,
    CH6=922.8, CH7=923.4, CH8=924.2, CH9=924.7, CH10=925.9,
    A1=920.5, A2=922.4, A3=926.7  (all MHz)
"""

import argparse
import signal
import sys
import os
import atexit

# Suppress HackRF firmware debug spam BEFORE importing GR/osmosdr
_devnull = None
_old_stderr = None
try:
    _devnull = open(os.devnull, 'w')
    _old_stderr = os.dup(sys.stderr.fileno())
    os.dup2(_devnull.fileno(), sys.stderr.fileno())
except Exception:
    pass

def _restore_stderr():
    try:
        if _old_stderr is not None and _devnull is not None:
            os.dup2(_old_stderr, sys.stderr.fileno())
            os.close(_old_stderr)
            _devnull.close()
    except Exception:
        pass

atexit.register(_restore_stderr)

from gnuradio import gr
try:
    gr.log.filter('', gr.log.LOG_ERROR)
except Exception:
    pass
from gnuradio import audio
from gnuradio import analog
from gnuradio import filter
from gnuradio import blocks

# Exhaustive osmosdr source detection across GR versions
_src_cls = None

# 1) Old-style osmocom package
try:
    from osmocom.source import osmocom_src
    _src_cls = osmocom_src
except Exception:
    pass

# 2) gnuradio.osmocom namespace
if _src_cls is None:
    try:
        from gnuradio import osmocom
        _src_cls = osmocom.source.osmocom_src
    except Exception:
        pass

# 3) gnuradio.osmosdr block
if _src_cls is None:
    try:
        from gnuradio import osmosdr
        _src_cls = osmosdr.source
    except Exception:
        pass

# 4) gnuradio.osmosdr submodule import
if _src_cls is None:
    try:
        from gnuradio.osmosdr import source as _s
        _src_cls = _s
    except Exception:
        pass

# 5) top-level osmosdr module (some distros install it here)
if _src_cls is None:
    try:
        import osmosdr
        _src_cls = osmosdr.source
    except Exception:
        pass

if _src_cls is None:
    import os as _os
    gr_mods = sorted([m for m in _os.listdir(_os.path.dirname(gr.__file__)) if m.startswith("osmo") or "osmosdr" in m])
    raise ImportError(
        "No osmosdr source module found.\n"
        "Tried: osmocom.source, gnuradio.osmocom, gnuradio.osmosdr\n"
        "Found gnuradio modules: {}\n"
        "Install: sudo apt install gr-osmosdr\n"
        "Or build from: https://github.com/osmocom/gr-osmosdr".format(gr_mods)
    )

# Channel map
CHANNELS = {
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
    "A1": 920.5e6,
    "A2": 922.4e6,
    "A3": 926.7e6,
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Silent Disco Receiver - HackRF One"
    )
    parser.add_argument(
        "channel",
        nargs="?",
        help="Channel name (e.g. CH1, A1) or frequency in MHz",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available channels and exit",
    )
    parser.add_argument(
        "--freq",
        type=float,
        help="Frequency in MHz (alternative to channel name)",
    )
    parser.add_argument(
        "--gain",
        type=int,
        default=16,
        help="LNA gain in dB (default: 16, max 40)",
    )
    parser.add_argument(
        "--vga",
        type=int,
        default=14,
        help="VGA gain in dB (default: 14, max 62)",
    )
    parser.add_argument(
        "--audio-gain",
        type=float,
        default=5.0,
        help="Post-demod audio gain multiplier (default: 5.0)",
    )
    return parser.parse_args()


def resolve_frequency(channel_name=None, freq_mhz=None):
    if channel_name:
        if channel_name.upper() in CHANNELS:
            return CHANNELS[channel_name.upper()]
        try:
            return float(channel_name) * 1e6
        except ValueError:
            print(f"[!] Unknown channel: {channel_name}")
            print(f"    Available: {', '.join(CHANNELS.keys())}")
            sys.exit(1)
    if freq_mhz:
        return freq_mhz * 1e6
    print("[!] Must specify a channel or --freq")
    sys.exit(1)


class SilentDiscoRX(gr.top_block):
    def __init__(self, freq, lna_gain=30, vga_gain=30, audio_gain=5.0):
        gr.top_block.__init__(self, "Silent Disco RX")

        ##################################################
        # Parameters
        ##################################################
        # Rate plan (integer decimation all the way to the soundcard):
        #   2.4 MSPS  --/10-->  240 kHz (quad rate into wfm_rcv)
        #   240 kHz   --/5 -->   48 kHz (audio out)
        self.samp_rate = 2.4e6
        self.rf_decim = 10
        self.quad_rate = self.samp_rate / self.rf_decim   # 240 kHz
        self.audio_decim = 5
        self.audio_rate = int(self.quad_rate / self.audio_decim)  # 48 kHz
        self.center_freq = freq
        self.lna_gain = lna_gain
        self.vga_gain = vga_gain

        ##################################################
        # Blocks
        ##################################################
        # HackRF One source
        if getattr(_src_cls, '__name__', '') == 'osmocom_src':
            self.src = _src_cls(
                args="numchan=" + str(1) + " " +
                     "samp_rate=" + str(self.samp_rate) + " " +
                     "center_freq=" + str(self.center_freq) + " " +
                     "gain=" + str(self.lna_gain) + " " +
                     "if_gain=" + str(self.vga_gain) + " " +
                     "bb_gain=" + str(0),
                freq=self.center_freq,
                rate=self.samp_rate,
                gain=self.lna_gain,
                if_gain=self.vga_gain,
                bb_gain=0,
                antenna="",
                band=0,
            )
        else:
            # osmosdr.source: args string must include nchan=1
            args_str = (
                "hackrf,nchan=" + str(1) +
                ",samp_rate=" + str(self.samp_rate) +
                ",center_freq=" + str(self.center_freq) +
                ",gain=" + str(self.lna_gain) +
                ",if_gain=" + str(self.vga_gain) +
                ",bb_gain=" + str(0)
            )
            try:
                self.src = _src_cls(args=args_str)
            except TypeError:
                self.src = _src_cls(
                    args="hackrf",
                    center_freq=self.center_freq,
                    sample_rate=self.samp_rate,
                )
            self.src.set_center_freq(self.center_freq)
            self.src.set_sample_rate(self.samp_rate)
            try:
                self.src.set_gain(self.lna_gain, "LNA")
            except Exception:
                pass
            try:
                self.src.set_if_gain(self.vga_gain)
            except Exception:
                pass
            try:
                self.src.set_bb_gain(0)
            except Exception:
                pass

        # Channel filter + decimation: 2.4 MSPS -> 240 kHz
        # Passband ~100 kHz keeps the full WBFM stereo multiplex
        # (L+R, 19 kHz pilot, L-R subcarrier) intact.
        try:
            taps = filter.firdes.low_pass(
                gain=1.0,
                sampling_freq=self.samp_rate,
                cutoff_freq=100e3,
                transition_width=20e3,
                window=0,
                param=6.76,
            )
        except TypeError:
            taps = filter.firdes.low_pass(
                gain=1.0,
                sampling_freq=self.samp_rate,
                cutoff_freq=100e3,
                transition_width=20e3,
            )
        self.lpf = filter.fir_filter_ccf(self.rf_decim, taps)

        # Wide-band FM demodulator at the decimated quad rate.
        # 240 kHz in, /5 -> 48 kHz audio out.
        try:
            self.wbfm = analog.wfm_rcv(
                quad_rate=self.quad_rate,
                audio_decimation=self.audio_decim,
                deemph=75e-6,       # 75 µs de-emphasis
            )
        except TypeError:
            self.wbfm = analog.wfm_rcv(
                quad_rate=self.quad_rate,
                audio_decimation=self.audio_decim,
            )

        # Audio gain: boost quiet signals
        self.audio_amp = blocks.multiply_const_ff(audio_gain)

        # Audio sink at the rate the chain actually produces (48 kHz)
        self.audio_sink = audio.sink(self.audio_rate, "", True)

        ##################################################
        # Connections
        ##################################################
        self.connect(
            (self.src, 0),
            self.lpf,
            self.wbfm,
            self.audio_amp,
            self.audio_sink,
        )


def main():
    args = parse_args()

    if args.list:
        print("Available Silent Disco channels:")
        print(f"  {'Name':<6}  {'Freq (MHz)':>12}")
        print(f"  {'-----':<6}  {'----------':>12}")
        for name, freq in CHANNELS.items():
            print(f"  {name:<6}  {freq/1e6:>12.1f}")
        sys.exit(0)

    freq = resolve_frequency(args.channel, args.freq)
    freq_mhz = freq / 1e6

    print(f"[*] Silent Disco Receiver")
    print(f"[*] Channel  : {args.channel if args.channel else f'{freq_mhz:.1f} MHz'}")
    print(f"[*] Frequency: {freq_mhz:.3f} MHz")
    print(f"[*] HackRF   : LNA={args.gain} dB, VGA={args.vga} dB")
    print(f"[*] Rates    : 2.4 MSPS -> /10 -> 240 kHz -> /5 -> 48 kHz audio")
    print(f"[*] Ctrl+C   : stop\n")

    tb = SilentDiscoRX(
        freq=freq,
        lna_gain=args.gain,
        vga_gain=args.vga,
        audio_gain=args.audio_gain,
    )

    # Graceful Ctrl+C
    def sig_handler(sig, frame):
        print("\n[*] Stopping...")
        tb.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)

    try:
        tb.start()
        signal.pause()
    except Exception as e:
        print("[!] Runtime error:", repr(e))
        raise


if __name__ == "__main__":
    main()
