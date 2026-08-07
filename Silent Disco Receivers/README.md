# Silent Disco Receivers

Receive the DEF CON silent disco with an SDR and listen to any channel.

The silent disco is a [Quiet Events](https://quietevents.com/) system. DEF CON
publishes this in the Hacker Tracker information documents: "We're using two
different solutions: Quiet Events and Listen WIFI. Quiet Events may also be
known as 'Silent Disco'... Headsets have LEDs on them; the color indicates the
audio channel that the headset is tuned to."

Physically it is the simplest thing it could be: **analogue FM, one carrier per
audio channel, in the US 902-928 MHz ISM band, stereo**. A headset is an FM
receiver, and the channel button retunes it. Nothing stops an SDR from doing
the same thing, or from doing it to several channels at once.

## Files

| File | What it does |
| --- | --- |
| `gr_3.10_silent_disco_rx.grc` | GNU Radio Companion flowgraph. Pick a channel from the drop-down, hear it. |
| `make_silent_disco_iq.py` | Writes a synthetic silent disco capture so you can test the receiver without a rig. |

## Quick start

You need GNU Radio 3.10 and an RTL-SDR. `gr-soapy` ships with GNU Radio 3.10,
so nothing else has to be installed. An antenna cut for 900 MHz helps; the
stock whip works if you are in the room.

```
gnuradio-companion gr_3.10_silent_disco_rx.grc
```

Press run. Pick a channel from **Silent Disco Channel** at the top of the
window. The lower left plot is the whole 2.4 MHz capture, so you can see which
carriers are actually on the air; the lower right plot is the channel you
selected after filtering.

## The channel plan

The drop-down carries the ten Quiet Events Ultra 900 channels used on the main
and creator stages. The colour is the colour of the LED on the matching
headset, so a headset lying on a chair tells you the frequency.

| Channel | Colour | MHz | | Channel | Colour | MHz |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | red | 920.1 | | 6 | turquoise | 922.8 |
| 2 | yellow | 920.7 | | 7 | white | 923.4 |
| 3 | green | 921.2 | | 8 | orange | 924.2 |
| 4 | purple | 921.9 | | 9 | pink | 924.7 |
| 5 | blue | 922.3 | | 10 | mint | 925.9 |

Village and community locations use the Quiet Events 45 Max transmitter
instead, which has 45 channels from 908.1 to 927.5 MHz.

Two things worth noticing about this plan, because they drive the design:

* **The spacing is not uniform.** It runs from 400 kHz to 1.2 MHz. Anything
  that assumes a regular channel grid, such as a polyphase channelizer, does
  not fit it.
* **The whole plan is wider than one cheap SDR can see.** 908 to 927.5 MHz is
  19.4 MHz; an RTL-SDR gets about 2.4 MHz. You will always be choosing a slice.

## How it works

```
Soapy RTLSDR Source  ->  Frequency Xlating FIR Filter  ->  FM Demod  ->  AGC  ->  Volume  ->  Audio Sink
```

* The radio tunes 300 kHz **below** the channel you picked, and the translating
  filter shifts by +300 kHz to bring it back to zero. That keeps the carrier off
  the RTL-SDR's DC spike, which otherwise sits right in the middle of your
  audio.
* The channel filter is 200 kHz wide with a Blackman-Harris window. Hamming's
  53 dB stopband is not enough when a loud transmitter is 400 kHz from a quiet
  one, which the plan above allows.
* `FM Demod` is stock `analog.fm_demod_cf` with a 15 kHz audio passband. That
  15 kHz limit matters: these transmitters send a broadcast-style stereo
  multiplex, and the 19 kHz pilot tone and the 38 kHz L-R subcarrier sit just
  above the audio. Filter at 15 kHz and you get clean mono, which is the L+R
  sum, and it works whether the transmitter is stereo or mono. Filter wider and
  you get a 19 kHz whine and the AGC chasing it.
* The AGC is there because Quiet Events do not publish their deviation, and no
  FCC grant exists under the brand name (the hardware is certified under the
  factory's name). Rather than guess the number and get the level wrong, the
  audio is levelled after demodulation.

## Testing it without a silent disco

`make_silent_disco_iq.py` writes a complex float32 capture containing as many
FM carriers as you ask for, each with a full stereo multiplex: pre-emphasis,
L+R, a 19 kHz pilot and the L-R subcarrier at 38 kHz. Each carrier gets a
different tone, so a correct decode is provable rather than a matter of
opinion.

```
./make_silent_disco_iq.py --out silent_disco.cf32 --offsets=-200e3,300e3,800e3
```

Then in the flowgraph, right-click **File Source** and **Throttle** and enable
them, right-click **Soapy RTLSDR Source** and disable it, and run. The default
`if_offset` of 300 kHz lands on the middle carrier, which carries a 700 Hz tone
on the left and 1400 Hz on the right.

This writes a file. It does not transmit, and neither does the flowgraph.

## Only 3.10

The other receivers in this repository ship for 3.7 through 3.10. This one is
3.10 only: it uses `gr-soapy`, which does not exist before 3.9, and 3.10 is the
version it was written and tested against. A 3.9 port would be the same
flowgraph; earlier versions would need `gr-osmosdr` instead.
