#!/usr/bin/env python3
"""
silent_disco_multi.py - Multi-channel Silent Disco monitor for RFCTF

Monitors 5 or more Silent Disco channels SIMULTANEOUSLY from a single
HackRF One capture, shows a live signal-strength meter for every monitored
channel, and plays the audio of whichever channel you select -- switchable
at runtime without restarting or retuning the radio.

How it works
------------
The whole channel plan (920.1 - 926.7 MHz) spans only 6.6 MHz, so a single
9.6 MSPS capture centred at 923.4 MHz contains every channel at once.
Each monitored channel is then split out in parallel by its own
frequency-translating FIR filter, which shifts that channel to baseband and
decimates in one step:

    HackRF @ 9.6 MSPS  (centre 923.4 MHz, all channels present)
        |
        +-- xlate(offset_1) --/40--> 240 kHz --> WBFM --/5--> 48 kHz --+
        +-- xlate(offset_2) --/40--> 240 kHz --> WBFM --/5--> 48 kHz --+
        +-- ...                                                        +--> mixer --> sound card
        +-- xlate(offset_N) --/40--> 240 kHz --> WBFM --/5--> 48 kHz --+
        |
        +-- per-channel power probe --> live dB meter

Every branch runs continuously, so the meters show all channels at the same
time. Audio selection is just a per-branch gain: the selected branch is set
to its audio gain and the rest to zero, then all branches are summed into
one sound card sink. Switching channels therefore has no audio dropout and
never touches the tuner.

Usage
-----
    # monitor the first five channels, listen to CH1
    python3 silent_disco_multi.py

    # monitor a specific set, listen to CH3
    python3 silent_disco_multi.py --channels CH1,CH2,CH3,A1,A2 --listen CH3

    # monitor every known channel
    python3 silent_disco_multi.py --channels all

While running, type a channel name (e.g. "CH4") and press Enter to switch
the audio to it. Type "m" to mute, "q" or Ctrl+C to quit.
"""

import argparse
import math
import os
import signal
import sys
import threading
import time

from gnuradio import gr
from gnuradio import analog
from gnuradio import audio
from gnuradio import blocks
from gnuradio import filter as gr_filter

# ---------------------------------------------------------------------------
# osmosdr source lookup: the module moved around between GNU Radio releases,
# so try every known location before giving up.
# ---------------------------------------------------------------------------
_src_cls = None
for _attempt in (
    lambda: __import__("osmosdr").source,
    lambda: __import__("gnuradio.osmosdr", fromlist=["source"]).source,
    lambda: __import__("osmocom.source", fromlist=["osmocom_src"]).osmocom_src,
):
    try:
        _src_cls = _attempt()
        break
    except Exception:
        continue

if _src_cls is None:
    raise ImportError(
        "No osmosdr source module found.\n"
        "Tried: osmosdr, gnuradio.osmosdr, osmocom.source\n"
        "Install with: sudo apt install gr-osmosdr"
    )


# ---------------------------------------------------------------------------
# Channel plan (MHz -> Hz)
# ---------------------------------------------------------------------------
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

DEFAULT_CHANNELS = ["CH1", "CH2", "CH3", "CH4", "CH5"]

# Rate plan: integer decimation the whole way to the sound card.
#   9.6 MSPS --/20--> 480 kHz (WBFM quad rate) --/10--> 48 kHz (audio)
#
# The 480 kHz quad rate is deliberate. WBFM stereo carries the L-R
# subcarrier out to 53 kHz on top of ~75 kHz deviation, so by Carson's rule
# the signal occupies ~256 kHz. A 240 kHz quad rate (Nyquist +/-120 kHz)
# cannot represent that and silently truncates the stereo multiplex. At
# 480 kHz (Nyquist +/-240 kHz) a 280 kHz channel filter fits with margin,
# and 480/10 lands on 48 kHz exactly so no resampler is needed.
SAMP_RATE = 9.6e6
RF_DECIM = 20
QUAD_RATE = SAMP_RATE / RF_DECIM           # 480 kHz
AUDIO_DECIM = 10
AUDIO_RATE = int(QUAD_RATE / AUDIO_DECIM)  # 48 kHz


def make_lowpass(samp_rate, cutoff, transition):
    """firdes.low_pass() lost its `taps` kwarg and gained window/param in
    GNU Radio 3.10; try the new signature first, fall back to the old one."""
    try:
        return gr_filter.firdes.low_pass(
            gain=1.0,
            sampling_freq=samp_rate,
            cutoff_freq=cutoff,
            transition_width=transition,
            window=0,          # WIN_HAMMING
            param=6.76,
        )
    except TypeError:
        return gr_filter.firdes.low_pass(
            gain=1.0,
            sampling_freq=samp_rate,
            cutoff_freq=cutoff,
            transition_width=transition,
        )


def make_wbfm(quad_rate, audio_decim):
    """wfm_rcv() dropped the `deemph` kwarg in some builds."""
    try:
        return analog.wfm_rcv(
            quad_rate=quad_rate,
            audio_decimation=audio_decim,
            deemph=75e-6,      # 75 us de-emphasis
        )
    except TypeError:
        return analog.wfm_rcv(
            quad_rate=quad_rate,
            audio_decimation=audio_decim,
        )


class ChannelBranch(object):
    """One monitored channel: xlate to baseband, demodulate, meter, gate."""

    def __init__(self, tb, source, name, freq, center_freq, chan_taps, audio_gain):
        self.name = name
        self.freq = freq
        self.offset = freq - center_freq

        # Shift this channel down to baseband and decimate in one block.
        self.xlate = gr_filter.freq_xlating_fir_filter_ccf(
            RF_DECIM, chan_taps, self.offset, SAMP_RATE
        )

        self.demod = make_wbfm(QUAD_RATE, AUDIO_DECIM)

        # Audio gate: 0 = silent, audio_gain = audible. Every branch keeps
        # running regardless, so switching is instant and dropout-free.
        self.gate = blocks.multiply_const_ff(0.0)
        self.audio_gain = audio_gain

        # Power meter tapped off the channelised stream (pre-demod), so the
        # reading reflects RF presence even when the branch is muted.
        self.mag = blocks.complex_to_mag_squared()
        self.avg = blocks.moving_average_ff(4000, 1.0 / 4000, 4000)
        self.probe = blocks.probe_signal_f()

        tb.connect(source, self.xlate, self.demod, self.gate)
        tb.connect(self.xlate, self.mag, self.avg, self.probe)

    def set_audible(self, audible):
        self.gate.set_k(self.audio_gain if audible else 0.0)

    def power_db(self):
        p = self.probe.level()
        if p <= 0.0:
            return -99.0
        return 10.0 * math.log10(p)


class MultiChannelRX(gr.top_block):
    def __init__(self, names, lna_gain, vga_gain, audio_gain):
        gr.top_block.__init__(self, "Silent Disco Multi-Channel Monitor")

        freqs = [CHANNELS[n] for n in names]
        # Centre the capture on the middle of the requested channels so the
        # offsets stay as small as possible.
        self.center_freq = (min(freqs) + max(freqs)) / 2.0

        span_needed = max(freqs) - min(freqs)
        if span_needed > SAMP_RATE * 0.8:
            raise ValueError(
                "Requested channels span %.2f MHz, too wide for a %.1f MSPS "
                "capture. Monitor fewer channels or ones closer together."
                % (span_needed / 1e6, SAMP_RATE / 1e6)
            )

        self.src = self._make_source(lna_gain, vga_gain)

        # One shared taps set for every branch: +/-140 kHz passband (280 kHz
        # total) covers the full WBFM stereo multiplex per Carson's rule
        # (~256 kHz). An 80 kHz transition keeps the tap count (and so the
        # CPU cost per branch) reasonable; the stopband starts at 220 kHz,
        # comfortably inside the 400 kHz minimum channel spacing.
        chan_taps = make_lowpass(SAMP_RATE, 140e3, 80e3)
        self.ntaps = len(chan_taps)

        self.branches = []
        for name in names:
            self.branches.append(
                ChannelBranch(
                    self, self.src, name, CHANNELS[name],
                    self.center_freq, chan_taps, audio_gain,
                )
            )

        # Sum every branch into a single sound card sink. Muted branches
        # contribute silence, so exactly one channel is heard at a time.
        self.mixer = blocks.add_ff()
        for i, branch in enumerate(self.branches):
            self.connect(branch.gate, (self.mixer, i))

        # Soft limiter on the summed audio: tanh squashes peaks smoothly
        # instead of clipping harshly, so a high --audio-gain distorts
        # gracefully rather than crackling.
        self.limiter = blocks.transcendental("tanh", "float")

        self.audio_sink = audio.sink(AUDIO_RATE, "", True)
        self.connect(self.mixer, self.limiter, self.audio_sink)

    def _make_source(self, lna_gain, vga_gain):
        args_str = (
            "hackrf,nchan=1"
            ",samp_rate=" + str(SAMP_RATE) +
            ",center_freq=" + str(self.center_freq) +
            ",gain=" + str(lna_gain) +
            ",if_gain=" + str(vga_gain) +
            ",bb_gain=0"
        )
        try:
            src = _src_cls(args=args_str)
        except TypeError:
            src = _src_cls(args="hackrf")

        src.set_sample_rate(SAMP_RATE)
        src.set_center_freq(self.center_freq)
        for setter, value in (
            ("set_gain", lna_gain),
            ("set_if_gain", vga_gain),
            ("set_bb_gain", 0),
        ):
            try:
                getattr(src, setter)(value)
            except Exception:
                pass
        try:
            getattr(src, "set_bandwidth")(0)
        except Exception:
            pass
        return src

    def listen_to(self, name):
        """Route exactly one monitored channel to the sound card."""
        for branch in self.branches:
            branch.set_audible(branch.name == name)

    def mute(self):
        for branch in self.branches:
            branch.set_audible(False)


def render_meters(tb, listening):
    """Draw one line per monitored channel with a live dB bar."""
    lines = []
    for branch in tb.branches:
        db = branch.power_db()
        # Map roughly -80..0 dB onto a 40-cell bar.
        filled = int(max(0.0, min(1.0, (db + 80.0) / 80.0)) * 40)
        marker = ">" if branch.name == listening else " "
        lines.append(
            "%s %-5s %9.3f MHz  %6.1f dB  |%s%s|"
            % (marker, branch.name, branch.freq / 1e6, db,
               "#" * filled, "." * (40 - filled))
        )
    return lines


def input_thread(tb, state):
    """Read channel switches from stdin without blocking the flowgraph."""
    for raw in sys.stdin:
        cmd = raw.strip()
        if not cmd:
            continue
        low = cmd.lower()
        if low in ("q", "quit", "exit"):
            state["running"] = False
            return
        if low in ("m", "mute"):
            tb.mute()
            state["listening"] = None
            continue
        name = cmd.upper()
        if any(b.name == name for b in tb.branches):
            tb.listen_to(name)
            state["listening"] = name
        else:
            state["message"] = "not monitored: %s" % cmd


def parse_channel_list(value):
    if value.strip().lower() == "all":
        return list(CHANNELS.keys())
    names = []
    for part in value.split(","):
        name = part.strip().upper()
        if not name:
            continue
        if name not in CHANNELS:
            raise SystemExit(
                "[!] Unknown channel %r. Known: %s"
                % (part.strip(), ", ".join(CHANNELS))
            )
        if name not in names:
            names.append(name)
    return names


def main():
    ap = argparse.ArgumentParser(
        description="Monitor 5+ Silent Disco channels at once on a HackRF One"
    )
    ap.add_argument(
        "--channels", default=",".join(DEFAULT_CHANNELS),
        help="comma-separated channels to monitor, or 'all' "
             "(default: %s)" % ",".join(DEFAULT_CHANNELS),
    )
    ap.add_argument(
        "--listen",
        help="channel to route to the sound card at startup "
             "(default: first monitored channel)",
    )
    ap.add_argument("--list", action="store_true",
                    help="list known channels and exit")
    ap.add_argument("--gain", type=int, default=16,
                    help="HackRF LNA gain in dB (default: 16, max 40)")
    ap.add_argument("--vga", type=int, default=16,
                    help="HackRF VGA/IF gain in dB (default: 16, max 62)")
    ap.add_argument("--audio-gain", type=float, default=5.0,
                    help="audio gain for the selected channel (default: 5.0)")
    ap.add_argument("--refresh", type=float, default=0.5,
                    help="meter refresh interval in seconds (default: 0.5)")
    args = ap.parse_args()

    if args.list:
        print("Known Silent Disco channels:")
        for name, freq in CHANNELS.items():
            print("  %-5s %9.3f MHz" % (name, freq / 1e6))
        return

    names = parse_channel_list(args.channels)
    if len(names) < 2:
        raise SystemExit("[!] Monitor at least two channels.")

    listening = (args.listen or names[0]).upper()
    if listening not in names:
        raise SystemExit(
            "[!] --listen %s is not in the monitored set (%s)"
            % (listening, ",".join(names))
        )

    tb = MultiChannelRX(names, args.gain, args.vga, args.audio_gain)

    print("[*] Silent Disco multi-channel monitor")
    print("[*] Monitoring : %d channels -> %s" % (len(names), ", ".join(names)))
    print("[*] Capture    : %.3f MHz centre, %.1f MSPS"
          % (tb.center_freq / 1e6, SAMP_RATE / 1e6))
    print("[*] Channel BW : 100 kHz passband, %d taps/branch" % tb.ntaps)
    print("[*] Rates      : %.1f MSPS -> /%d -> %.0f kHz -> /%d -> %d Hz audio"
          % (SAMP_RATE / 1e6, RF_DECIM, QUAD_RATE / 1e3, AUDIO_DECIM, AUDIO_RATE))
    print("[*] HackRF     : LNA=%d dB, VGA=%d dB" % (args.gain, args.vga))
    print("[*] Controls   : type a channel name + Enter to listen, "
          "'m' to mute, 'q' to quit")
    print()

    state = {"running": True, "listening": listening, "message": ""}

    tb.start()
    tb.listen_to(listening)

    reader = threading.Thread(target=input_thread, args=(tb, state))
    reader.daemon = True
    reader.start()

    def on_sigint(sig, frame):
        state["running"] = False

    signal.signal(signal.SIGINT, on_sigint)

    drawn = 0
    try:
        while state["running"]:
            lines = render_meters(tb, state["listening"])
            status = "listening: %s" % (state["listening"] or "muted")
            if state["message"]:
                status += "   (%s)" % state["message"]
            lines.append(status)

            if drawn:
                sys.stdout.write("\033[%dA" % drawn)
            for line in lines:
                sys.stdout.write("\033[2K" + line + "\n")
            sys.stdout.flush()
            drawn = len(lines)

            time.sleep(args.refresh)
    finally:
        print("\n[*] Stopping...")
        tb.stop()
        tb.wait()


if __name__ == "__main__":
    main()
