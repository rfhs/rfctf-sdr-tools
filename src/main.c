#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <getopt.h>
#include <termios.h>
#include <math.h>
#include <complex.h>
#include <pthread.h>
#include <liquid/liquid.h>

#include "channel.h"
#include "hackrx.h"
#include "dsp.h"
#include "audio.h"
#include "ring.h"

static volatile int g_quit = 0;

static void on_sigint(int s) { (void)s; g_quit = 1; }

/* --- synthetic FM transmitter for offline validation -------------------- */
typedef struct {
    ring_t  *iq;
    uint64_t lo;          /* receiver LO */
    float    fs_in;
    int      nchan;
    uint64_t ch_freqs[MAX_DEMOD];
    volatile int running;
    pthread_t thread;
} sim_xmit_t;

/* generate one block of the multi-channel sim baseband with rotating
 * phasors (no transcendentals per sample).  Fallback path for the rare
 * non-periodic geometry; see sim_thread. */
static void sim_generate(const sim_xmit_t *s, liquid_float_complex *buf, int n,
                         liquid_float_complex *mtone, liquid_float_complex *carr,
                         liquid_float_complex *dstate,
                         const liquid_float_complex *mrot,
                         const liquid_float_complex *crot, float kfm)
{
    for (int i = 0; i < n; i++) {
        liquid_float_complex acc = 0.0f;
        for (int c = 0; c < s->nchan; c++) {
            mtone[c] *= mrot[c];
            float msg = 0.55f * cimagf(mtone[c]);
            /* FM deviation phasor: the per-sample angle is tiny
             * (<= ~0.01 rad), so a 2nd-order Taylor step is exact to
             * ~1e-7 rad and needs no transcendental call */
            float d = kfm * msg;
            dstate[c] *= 1.0f + _Complex_I * d - 0.5f * d * d;
            carr[c] *= crot[c];
            acc += carr[c] * dstate[c];
        }
        buf[i] = acc * (0.8f / (float)s->nchan);
    }
}

static void *sim_thread(void *arg)
{
    sim_xmit_t *s = (sim_xmit_t *)arg;
    const float dev = 50000.0f;      /* 50 kHz deviation */
    const double dt = 1.0 / (double)s->fs_in;
    const float kfm = 2.0f * (float)M_PI * dev * (float)dt; /* rad/sample per unit msg */

    /* The sim signal is exactly periodic: every message tone is a multiple
     * of 400 Hz and every carrier offset is a multiple of 100 kHz, so the
     * whole multi-channel baseband repeats every 2.5 ms.  Render one period
     * once (full sin/cexpf math is affordable one-off) and then just loop
     * it: streaming generation cannot sustain high rates x many channels
     * in real time, and once the sim falls behind wall time the whole
     * downstream pipeline -- and the audio RATE -- sags with it. */
    size_t period = (size_t)llround((double)s->fs_in / 400.0);
    liquid_float_complex *loop = malloc(period * sizeof(*loop));
    int periodic = (loop != NULL);
    if (periodic) {
        for (int c = 0; c < s->nchan; c++) {
            double off = (double)s->ch_freqs[c] - (double)s->lo;
            double cyc = off * ((double)period * dt);   /* cycles per period */
            if (fabs(cyc - round(cyc)) > 1e-6) { periodic = 0; break; }
        }
    }
    if (periodic) {
        double t = 0.0;
        double phase[MAX_DEMOD] = {0};
        for (size_t i = 0; i < period; i++) {
            liquid_float_complex acc = 0.0f;
            for (int c = 0; c < s->nchan; c++) {
                double mf = 400.0 * (double)(c + 1);
                double msg = 0.55 * sin(2.0 * M_PI * mf * t);
                phase[c] += 2.0 * M_PI * msg * (dev * dt);
                phase[c] -= 2.0 * M_PI * floor(phase[c] / (2.0 * M_PI));
                double off = 2.0 * M_PI *
                             ((double)s->ch_freqs[c] - (double)s->lo) * dt;
                phase[c] += off;
                acc += cexp(_Complex_I * phase[c]);
            }
            loop[i] = acc * (0.8f / (float)s->nchan);
            t += dt;
        }
    }

    /* phasor state for the non-periodic fallback path */
    static liquid_float_complex mrot[MAX_DEMOD], mtone[MAX_DEMOD];
    static liquid_float_complex crot[MAX_DEMOD], carr[MAX_DEMOD];
    static liquid_float_complex dstate[MAX_DEMOD];
    if (!periodic) {
        for (int c = 0; c < s->nchan; c++) {
            double mf = 400.0 * (double)(c + 1);
            mrot[c]   = cexpf(_Complex_I * (float)(2.0 * M_PI * mf * dt));
            mtone[c]  = 1.0f;
            double off = 2.0 * M_PI *
                         ((double)s->ch_freqs[c] - (double)s->lo) * dt;
            crot[c]   = cexpf(_Complex_I * (float)off);
            carr[c]   = 1.0f;
            dstate[c] = 1.0f;
        }
    }

    /* pace production to wall-clock real time, like a live radio: without
     * this the sim runs at CPU speed, the audio ring overflows (AUDDROPS)
     * and every dropped burst is an audible click */
    struct timespec t0;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    uint64_t produced = 0;
    size_t pos = 0;

    while (s->running) {
        static liquid_float_complex buf[4096];

        if (periodic) {
            for (int i = 0; i < 4096; i++) {
                buf[i] = loop[pos];
                if (++pos == period) pos = 0;
            }
        } else {
            sim_generate(s, buf, 4096, mtone, carr, dstate, mrot, crot, kfm);
            /* keep the phasors on the unit circle */
            for (int c = 0; c < s->nchan; c++) {
                mtone[c]  /= cabsf(mtone[c]);
                carr[c]   /= cabsf(carr[c]);
                dstate[c] /= cabsf(dstate[c]);
            }
        }
        ring_write(s->iq, buf, 4096);
        produced += 4096;

        /* sleep off however far ahead of real time we are */
        double due = (double)produced / (double)s->fs_in;
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        double elapsed = (double)(now.tv_sec - t0.tv_sec) +
                         1e-9 * (double)(now.tv_nsec - t0.tv_nsec);
        double ahead = due - elapsed;
        if (ahead > 0.0005) usleep((useconds_t)(ahead * 1e6 * 0.9));
    }
    free(loop);
    return NULL;
}

static void sim_start(sim_xmit_t *s)
{
    s->running = 1;
    pthread_create(&s->thread, NULL, sim_thread, s);
}

static void usage(const char *prog)
{
    printf("usage: %s [options]\n"
           "  -c, --channels <list> demod channels, e.g. A1,B3,E5 (max %d)\n"
           "  -l, --lo <Hz>        hackrf LO center (default = auto, kept off\n"
           "                       the DC spur)\n"
           "  -r, --rate <Hz>      capture rate (default = auto: smallest of\n"
           "                       8/10/12.5/16/20 Msps covering the selection)\n"
           "  -k, --kf <float>     FM demod factor (default: derived from --dev)\n"
           "  -v, --volume <f>     output volume (default 1.0)\n"
           "      --deemph <us>    de-emphasis time const 0,50,75 (default 75)\n"
           "      --dev <kHz>      assumed FM deviation, sets default kf (default 200)\n"
           "      --bw <kHz>       channel filter width (default 220)\n"
           "      --lpf <kHz>      audio low-pass cutoff (default 15)\n"
           "      --level <f>      audio AGC target level (default 0.3)\n"
           "      --no-agc         disable the audio AGC\n"
           "      --dump <path>    save N MiB of raw int8 I/Q for analysis\n"
           "      --lna <db>       hackrf LNA gain 0-40 (default 20)\n"
           "      --vga <db>       hackrf VGA gain 0-62 (default 40)\n"
           "      --amp            enable hackrf amp (default off)\n"
           "  -u, --u9             use Ultra-900 channel map (CH1-CH10, A1-A3)\n"
           "  -s, --sim            run simulated FM source (no hackrf)\n"
           "  -h, --help\n"
           "\n"
           "Channel map (TX1345 A-E):\n", prog, MAX_DEMOD);
}

/* parse a comma/space separated channel list into freq[]; returns count */
static int parse_channels(const char *list, const channel_table_t *t,
                          uint64_t *freq, int max)
{
    int n = 0;
    char buf[256];
    snprintf(buf, sizeof(buf), "%s", list);
    buf[sizeof(buf) - 1] = '\0';

    char *saveptr = NULL;
    for (char *tok = strtok_r(buf, ", ", &saveptr); tok;
         tok = strtok_r(NULL, ", ", &saveptr)) {
        int idx = channel_find(t, tok);
        if (idx < 0) {
            fprintf(stderr, "unknown channel '%s'\n", tok);
            continue;
        }
        freq[n++] = t->list[idx].freq_hz;
        if (n >= max) break;
    }
    return n;
}

/* LO = midpoint of min/max selected frequency */
static uint64_t pick_lo(const uint64_t *freq, int nchan)
{
    uint64_t lo_min = ~0ULL, lo_max = 0;
    for (int i = 0; i < nchan; i++) {
        if (freq[i] < lo_min) lo_min = freq[i];
        if (freq[i] > lo_max) lo_max = freq[i];
    }
    return (lo_min + lo_max) / 2;
}

/* Distance from the nearest selected channel to DC in baseband, in Hz, for
 * a given LO.  Returns -1.0 if any channel falls outside the capture
 * window. */
static double lo_clearance(const uint64_t *freq, int nchan, uint64_t lo,
                           double fs)
{
    const double window_half = fs / 2.0;
    double min_off = 1e300;
    for (int i = 0; i < nchan; i++) {
        double off = (double)freq[i] - (double)lo;
        if (fabs(off) > window_half) return -1.0;
        if (fabs(off) < min_off) min_off = fabs(off);
    }
    return min_off;
}

/* Choose an LO that keeps every selected channel at least ~1 MHz away from
 * baseband DC.  The HackRF has a strong DC spur at 0 Hz baseband that would
 * otherwise ride on any channel tuned there and turn the audio to static.
 * All candidates must keep every channel inside the capture window;
 * otherwise that geometry is impossible. */
static uint64_t pick_lo_clear(const uint64_t *freq, int nchan, double fs)
{
    uint64_t lo_mid = pick_lo(freq, nchan);
    uint64_t best   = lo_mid;
    double   best_cl = 0.0;

    /* try nudging the LO by these offsets and keep the one with the largest
     * channel-to-DC clearance */
    static const int64_t offs[] = {
        1500000LL, -1500000LL, 2500000LL, -2500000LL,
        4000000LL, -4000000LL, 5000000LL, -5000000LL,
    };
    for (size_t k = 0; k < sizeof(offs)/sizeof(offs[0]); k++) {
        int64_t cand = (int64_t)lo_mid + offs[k];
        if (cand < 0) continue;
        double cl = lo_clearance(freq, nchan, (uint64_t)cand, fs);
        if (cl < 0) continue;                  /* outside capture window */
        if (cl > best_cl) { best_cl = cl; best = (uint64_t)cand; }
        if (best_cl >= 1100000.0) break;
    }
    return best;
}

/* Smallest standard capture rate that covers the selection, with margin
 * for the LO nudge and the channel filter skirt.  A single channel does not
 * need 20 Msps: lower rates halve (or better) the USB throughput, and a
 * link that cannot sustain the rate drops samples on-device, which shows
 * up as the audio production RATE falling below 48 kHz. */
static double pick_sample_rate(uint64_t span_hz, float chan_bw_hz,
                               double forced)
{
    if (forced > 0.0) return forced;
    static const double rates[] = {8e6, 10e6, 12.5e6, 16e6, 20e6};
    double need = (double)span_hz + (double)chan_bw_hz + 0.5e6;
    for (size_t i = 0; i < sizeof(rates)/sizeof(rates[0]); i++)
        if (rates[i] >= need) return rates[i];
    return DSP_FS_IN;
}

int main(int argc, char **argv)
{
    uint64_t lo = 0;
    double rate  = 0.0;     /* 0: auto */
    float kf   = -1.0f;     /* <0: derive from dev */
    float vol  = 1.0f;
    float deemph = 75.0f;   /* us; 0 disables */
    float dev_khz  = 200.0f;/* assumed FM deviation, for default kf */
    float bw_khz   = 220.0f;/* channel filter width */
    float lpf_khz  = 15.0f; /* audio low-pass cutoff */
    int   agc_on   = 1;
    float agc_lvl  = 0.3f;
    uint32_t lna = 20, vga = 40;
    int amp = 0;
    int sim = 0;
    int use_u9 = 0;
    char chan_arg[256] = "";

    enum { OPT_LNA = 1000, OPT_VGA, OPT_AMP, OPT_DEEMPH, OPT_DUMP,
           OPT_LPF, OPT_BW, OPT_DEV, OPT_LEVEL, OPT_NOAGC };
    static const struct option longopts[] = {
        {"channels", required_argument, NULL, 'c'},
        {"lo",       required_argument, NULL, 'l'},
        {"rate",     required_argument, NULL, 'r'},
        {"kf",       required_argument, NULL, 'k'},
        {"volume",   required_argument, NULL, 'v'},
        {"deemph",   required_argument, NULL, OPT_DEEMPH},
        {"dump",     required_argument, NULL, OPT_DUMP},
        {"lpf",      required_argument, NULL, OPT_LPF},
        {"bw",       required_argument, NULL, OPT_BW},
        {"dev",      required_argument, NULL, OPT_DEV},
        {"level",    required_argument, NULL, OPT_LEVEL},
        {"no-agc",   no_argument,       NULL, OPT_NOAGC},
        {"sim",      no_argument,       NULL, 's'},
        {"u9",       no_argument,       NULL, 'u'},
        {"lna-gain", required_argument, NULL, OPT_LNA},
        {"vga-gain", required_argument, NULL, OPT_VGA},
        {"amp",      no_argument,       NULL, OPT_AMP},
        {"help",     no_argument,       NULL, 'h'},
        {NULL, 0, NULL, 0}
    };
    int c;
    char dump_path[512] = "";
    while ((c = getopt_long(argc, argv, "c:l:r:k:v:shu", longopts, NULL)) != -1) {
        switch (c) {
        case 'c': snprintf(chan_arg, sizeof(chan_arg), "%s", optarg); break;
        case 'l': lo = strtoull(optarg, NULL, 0); break;
        case 'r': rate = strtod(optarg, NULL); break;
        case 'k': kf = atof(optarg); break;
        case 'v': vol = atof(optarg); break;
        case OPT_DEEMPH: deemph = atof(optarg); break;
        case OPT_LPF: lpf_khz = atof(optarg); break;
        case OPT_BW: bw_khz = atof(optarg); break;
        case OPT_DEV: dev_khz = atof(optarg); break;
        case OPT_LEVEL: agc_lvl = atof(optarg); break;
        case OPT_NOAGC: agc_on = 0; break;
        case 's': sim = 1; break;
        case 'u': use_u9 = 1; break;
        case OPT_LNA: lna = (uint32_t)atoi(optarg); break;
        case OPT_VGA: vga = (uint32_t)atoi(optarg); break;
        case OPT_AMP: amp = 1; break;
        case OPT_DUMP: snprintf(dump_path, sizeof(dump_path), "%s", optarg); break;
        case 'h': default: usage(argv[0]); return 0;
        }
    }

    /* kf not given: derive from the assumed deviation.  liquid's freqmod
     * increments phase by 2*pi*kf*m per sample and freqdem divides it back
     * out, so the kf that normalizes a full-deviation signal to ~unit
     * amplitude is kf = dev/fs_band (verified empirically with a matched
     * freqmod/freqdem pair).  The audio AGC absorbs any residual mismatch. */
    if (kf < 0.0f)
        kf = (float)((dev_khz * 1e3) / (double)DSP_FS_BAND);

    signal(SIGINT, on_sigint);
    signal(SIGTERM, on_sigint);

    /* ---- channel table ---- */
    channel_table_t table;
    if (use_u9) {
        channel_init_u9(&table);
        printf("Ultra-900 channel map\n");
    } else {
        channel_init(&table);
    }

    /* ---- select channels ---- */
    uint64_t freq[MAX_DEMOD];
    int nchan = 0;
    if (chan_arg[0])
        nchan = parse_channels(chan_arg, &table, freq, MAX_DEMOD);

    while (nchan == 0) {
        /* interactive mode: print menu and read channel names */
        printf("\nChannel selection (up to %d, within 20 MHz):\n", MAX_DEMOD);
        for (int i = 0; i < table.count; i++) {
            if (i % 5 == 0) printf("  ");
            printf("%s=%.1f  ", table.list[i].name,
                   table.list[i].freq_hz / 1e6);
            if (i % 5 == 4) printf("\n");
        }
        printf("\nChannels (comma separated, e.g. %s): ",
               use_u9 ? "CH1,CH3,A2" : "A1,B3,C5");
        fflush(stdout);
        char buf[256];
        if (!fgets(buf, sizeof(buf), stdin)) break;
        buf[strcspn(buf, "\n")] = '\0';
        nchan = parse_channels(buf, &table, freq, MAX_DEMOD);
        if (nchan == 0)
            printf("No valid channels. Try again.\n");
    }

    /* ---- span check, sample rate and LO ---- */
    uint64_t lo_min = ~0ULL, lo_max = 0;
    for (int i = 0; i < nchan; i++) {
        if (freq[i] < lo_min) lo_min = freq[i];
        if (freq[i] > lo_max) lo_max = freq[i];
    }
    if (lo_max - lo_min > 20000000ULL) {
        fprintf(stderr, "channels span %.1f MHz (>20 MHz)\n",
                (double)(lo_max - lo_min) / 1e6);
        return 1;
    }
    /* pick the smallest rate covering the selection: a USB link that cannot
     * sustain the capture rate drops samples on-device, which then shows up
     * as the audio RATE sagging below 48 kHz (choppy "fast" audio) */
    double fs = pick_sample_rate(lo_max - lo_min, bw_khz * 1e3f, rate);
    if (lo == 0) lo = pick_lo_clear(freq, nchan, fs);

    printf("selected %d channel%s, LO = %.1f MHz, span %.1f MHz, rate %.1f Msps\n",
           nchan, nchan == 1 ? "" : "s", lo / 1e6,
           (double)(lo_max - lo_min) / 1e6, fs / 1e6);

    /* ---- ring buffers ---- */
    ring_t iq, audio;
    ring_init(&iq, sizeof(liquid_float_complex), 1 << 19);
    ring_init(&audio, sizeof(float), 1 << 16);

    /* ---- hackrf or simulated source ---- */
    hackrx_t hx;
    int have_hx = 0, hx_started = 0;
    sim_xmit_t sx;
    ring_t *iq_in = &iq;    /* DSP input ring */
    if (!sim) {
        if (hackrx_open(&hx) != 0) goto fail_hx;
        have_hx = 1;
        if (dump_path[0] &&
            hackrx_dump_open(&hx, dump_path, (size_t)4 << 20) != 0) goto fail_hx;
        if (hackrx_configure(&hx, lo, fs, lna, vga, amp) != 0) goto fail_hx;
        iq_in = &hx.iq;     /* DSP reads what hackrf writes */
        /* NOTE: hackrx_start() deferred until the DSP and audio consumers
         * are running -- streaming into a ring nobody drains yet just
         * overflows it (the frozen IQDROPS burst seen at startup). */
    } else {
        printf("simulating %d FM channels\n", nchan);
        sx.iq = &iq;      /* output goes to the DSP input ring */
        sx.lo = lo;
        sx.fs_in = (float)fs;
        sx.nchan = nchan;
        for (int i = 0; i < nchan; i++) sx.ch_freqs[i] = freq[i];
        sim_start(&sx);
    }

    /* ---- DSP ---- */
    dsp_t dsp;
    dsp_init(&dsp, iq_in, &audio, (float)fs, DSP_FS_AUD, lo,
             kf, deemph > 0.0f ? deemph * 1e-6f : 0.0f, nchan,
             bw_khz * 1e3f, lpf_khz * 1e3f, agc_on, agc_lvl);
    for (int i = 0; i < nchan; i++) {
        dsp_set_chain(&dsp, i, freq[i]);
        dsp_set_gain(&dsp, i, 1.0f);
    }

    /* ---- audio ---- */
    audio_t aud;
    if (audio_init(&aud, &audio, DSP_FS_AUD) != 0) return 1;
    aud.volume = vol;

    if (dsp_start(&dsp) != 0) goto fail_dsp;
    if (audio_start(&aud) != 0) goto fail_aud;

    /* RF stream starts last, once every consumer is draining */
    if (have_hx) {
        if (hackrx_start(&hx) != 0) goto fail_stream;
        hx_started = 1;
    }

    /* ---- interactive key loop (termios raw) ---- */
    struct termios oldt, newt;
    tcgetattr(STDIN_FILENO, &oldt);
    newt = oldt;
    newt.c_lflag &= ~(ICANON | ECHO);
    newt.c_cc[VMIN] = 0;
    newt.c_cc[VTIME] = 0;
    tcsetattr(STDIN_FILENO, TCSANOW, &newt);

    printf("\nkeys: 1-0 toggle channels, +/- volume, q quit\n");
    long diag = 0;
    while (!g_quit) {
        char ch = 0;
        if (read(STDIN_FILENO, &ch, 1) == 1) {
            if (ch == 'q') break;
            else if (ch >= '1' && ch <= '9' && (ch - '1') < nchan) {
                int idx = ch - '1';
                float g = dsp_get_gain(&dsp, idx);
                dsp_set_gain(&dsp, idx, (g > 0) ? 0.0f : 1.0f);
                printf("channel %d %s: %s\n", idx + 1,
                       table.list[channel_lookup(&table, freq[idx])].name,
                       (g > 0) ? "muted" : "on");
            } else if (ch == '0') {
                /* toggle ALL */
                int alloff = 1;
                for (int i = 0; i < nchan; i++)
                    if (dsp_get_gain(&dsp, i) > 0) { alloff = 0; break; }
                for (int i = 0; i < nchan; i++)
                    dsp_set_gain(&dsp, i, alloff ? 1.0f : 0.0f);
                printf("all channels %s\n", alloff ? "on" : "muted");
            } else if (ch == '=' || ch == '+') {
                aud.volume = aud.volume * 1.2f;
                if (aud.volume > 2.0f) aud.volume = 2.0f;
                printf("volume %4.2f\n", aud.volume);
            } else if (ch == '-' || ch == '_') {
                aud.volume = aud.volume * 0.8f;
                if (aud.volume < 0.05f) aud.volume = 0.05f;
                printf("volume %4.2f\n", aud.volume);
            }
        } else {
            if (have_hx && hx.error) { printf("\nhackrf error seen\n"); break; }
            if ((++diag % 100) == 0) {
                /* audio production rate: should sit at ~48000 in live mode.
                 * Below that = DSP falling behind; the voice sounds fast
                 * because the sound card plays whatever arrives at 48 kHz. */
                static size_t last_prod = 0;
                static struct timespec last_t;
                static int have_last = 0;
                struct timespec now;
                clock_gettime(CLOCK_MONOTONIC, &now);
                printf("\r");
                if (have_last) {
                    double dt = (double)(now.tv_sec - last_t.tv_sec) +
                                1e-9 * (double)(now.tv_nsec - last_t.tv_nsec);
                    if (dt > 0.0) {
                        double arate = (double)(dsp.aud_produced - last_prod) / dt;
                        printf("RATE %.0f  ", arate);
                        /* live production sagging below 48k means the host
                         * is not receiving the full capture rate (USB link
                         * dropping samples on-device).  Say so once. */
                        static int rate_warned = 0;
                        if (have_hx && !rate_warned && arate < 47000.0) {
                            rate_warned = 1;
                            printf("\n*** audio rate %.0f < 48000: the USB "
                                   "link is not sustaining %.1f Msps. "
                                   "Try fewer channels, a lower --rate, or a "
                                   "different USB port/cable. ***\n",
                                   arate, fs / 1e6);
                        }
                    }
                }
                last_prod = dsp.aud_produced;
                last_t = now;
                have_last = 1;
                printf("IQ %.3f  ", dsp_get_iq_level(&dsp));
                if (have_hx && hx.drops)
                    printf("IQDROPS %zu  ", hx.drops);
                if (aud.underruns)
                    printf("AUDUND %lu  ", aud.underruns);
                if (dsp.aud_drops)
                    printf("AUDDROPS %zu  ", dsp.aud_drops);
                for (int i = 0; i < nchan; i++) {
                    int g = dsp_get_gain(&dsp, i) > 0 ? 1 : 0;
                    printf("%s %.3f  ",
                           table.list[channel_lookup(&table, freq[i])].name,
                           g ? dsp_get_level(&dsp, i) : 0.0f);
                }
                fflush(stdout);
            }
            usleep(20000);
        }
    }

    tcsetattr(STDIN_FILENO, TCSANOW, &oldt);

    dsp_stop(&dsp);
    dsp_join(&dsp);
    audio_stop(&aud);
    if (have_hx) {
        if (hx_started) hackrx_stop(&hx);
        if (dump_path[0]) hackrx_dump_close(&hx);
        hackrx_close(&hx);
    } else {
        sx.running = 0;
        pthread_join(sx.thread, NULL);
    }
    audio_destroy(&aud);
    dsp_destroy(&dsp);
    ring_destroy(&iq);
    ring_destroy(&audio);
    return 0;

fail_stream:
    audio_stop(&aud);
fail_aud:
    dsp_stop(&dsp);
fail_dsp:
    if (have_hx) {
        if (hx_started) hackrx_stop(&hx);
        hackrx_close(&hx);
    }
fail_hx:
    return 1;
}