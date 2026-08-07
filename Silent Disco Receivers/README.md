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
## Listening to every channel at once

`silent_disco_rx.py` does what the flowgraph does, from the command line, and
does it to every channel in the capture simultaneously.

```
./silent_disco_rx.py --plan ultra900 --record --record-dir wav
```

That tunes one radio, works out which of the ten Ultra 900 channels fit inside
a 2.4 MHz capture, and demodulates all of them concurrently into one WAV file
each:

```
centre        921.7500 MHz
sample rate   2.400 Msps
demod rate    480.0 kHz  (decimate 5)
audio rate    48000 Hz  (decimate 10)
channel taps  201, 200 kHz wide
demodulator   mono (L+R), de-emphasis 75 us, audio AGC
channels      5 demodulated concurrently
   0   920.7000 MHz   -1050.0 kHz  2 yellow     record
   1   921.2000 MHz    -550.0 kHz  3 green      record
   2   921.9000 MHz    +150.0 kHz  4 purple     record
   3   922.3000 MHz    +550.0 kHz  5 blue       record
   4   922.8000 MHz   +1050.0 kHz  6 turquoise  record
       920.1000 MHz  1 red        outside the capture
       923.4000 MHz  7 white      outside the capture
```

It also plays a single channel, by colour, by number, or by frequency:

```
./silent_disco_rx.py --channel blue
./silent_disco_rx.py --channel ch5
./silent_disco_rx.py --channel 922.3
```

and will do both at once, so you can listen to one channel while recording all
of them:

```
./silent_disco_rx.py --plan ultra900 --record --record-dir wav --channel blue
```

Useful options:

| Option | Why |
| --- | --- |
| `--source hackrf` | any SoapySDR driver: `rtlsdr`, `hackrf`, `airspy`, `bladerf`, `lime`, `uhd`, `plutosdr` |
| `--source file:capture.cf32` | work offline against a recorded capture |
| `--samp-rate 8e6` | a wider radio reaches more channels at once |
| `--stereo` | decode the stereo multiplex into left and right instead of mono L+R |
| `--plan max45` | the 45-channel village plan instead of the main-stage one |
| `--freqs 920.7,921.2,921.9` | ignore the plans, use these frequencies |
| `--seconds 60` | stop after a minute |
| `--list-plans` | print every built-in plan and exit |

### Why N filters and not a channelizer

The obvious alternative is a polyphase channelizer: capture wide, split into N
uniform bins, demodulate each bin. It is much cheaper per channel, and for many
channels on a regular grid it is the right answer. It is the wrong answer here,
for one reason and one reason only: **the channel plan is not on a regular
grid.**

The Ultra 900 channels are at 920.1, 920.7, 921.2, 921.9, 922.3, 922.8, 923.4,
924.2, 924.7 and 925.9 MHz. The gaps are 600, 500, 700, 400, 500, 600, 800, 500
and 1200 kHz. The finest uniform grid that contains all of them has 100 kHz
bins, so a channelizer would have to produce 24 bins to cover the range and
then stitch two or three adjacent bins back together for every channel, because
one channel is 200 kHz wide. That is more work than the thing it was supposed to
save, and it constrains the tuner's centre frequency to the bin grid as well.

`freq_xlating_fir_filter_ccf` has no such constraint. Each channel gets its own
translation, so irregular spacing costs nothing, and each channel can have its
own filter width, gain and output file.

The efficiency argument that favours a channelizer does not bite at this scale.
Five channels at 200 kHz with a 201-tap decimating filter is about 0.5 GMAC/s,
which is nothing. On an eight-core desktop this runs at roughly **12 times real
time in mono and 6 times real time in stereo**, so the constraint is USB
bandwidth, not arithmetic.

[RTLSDR-Airband](https://github.com/rtl-airband/RTLSDR-Airband) does use an FFT
channelizer and is right to: it demodulates tens of narrow AM and NFM voice
channels on a Raspberry Pi, where constant-cost-per-channel is the whole game,
and it decimates to 8 or 16 kHz audio. Neither of those applies to five 200 kHz
stereo music channels.

## Proving it works

`selftest.py` needs no radio. It generates a synthetic five-carrier silent
disco signal, runs the receiver over it, and checks that each WAV file holds
the tones that were put on that carrier and nothing else.

```
./selftest.py
```

It runs two **negative controls** as well, because a test that only ever
passes is not a test:

* **noise only** - no carriers at all. Must fail.
* **mistuned 500 kHz** - real carriers, receiver pointed into the gaps between
  them. Must fail.

Measured on GNU Radio 3.10.9.2:

| Case | Wanted tone over noise floor | Best other channel | Verdict |
| --- | --- | --- | --- |
| mono, five channels | 103 dB | 9 dB | pass |
| stereo, five channels | 92 dB | 23 dB | pass |
| noise only | 17 dB | 17 dB | correctly fails |
| mistuned 500 kHz | 16 dB | 16 dB | correctly fails |

The pass threshold is 30 dB. It sits 13 dB above everything the negative
controls produce and 60 dB below everything the positive controls produce.
Stereo separation measures 47 to 53 dB, which matches what this class of
hardware is specified at.
