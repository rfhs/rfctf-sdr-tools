#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <complex.h>
#include <unistd.h>
#include <liquid/liquid.h>

#include "ring.h"
#include "dsp.h"
#include "channel.h"

#define TRACE_SEC 2.0f

static float sim_msg(double t, int idx)
{
    double f = 400.0 * (double)(idx + 1);
    return 0.55f * (float)sin(2.0 * M_PI * f * t);
}

int main(int argc, char **argv)
{
    uint64_t lo = 0;
    float kf = 0.5f;
    int nchan = 1;
    if (argc > 1) nchan = atoi(argv[1]);
    if (nchan < 1) nchan = 1;
    if (nchan > MAX_DEMOD) nchan = MAX_DEMOD;
    if (argc > 2) kf = atof(argv[2]);

    channel_table_t table;
    channel_init(&table);

    uint64_t freq[MAX_DEMOD];
    for (int i = 0; i < nchan; i++) freq[i] = table.list[i].freq_hz; /* A1.. */

    uint64_t lo_min = ~0ULL, lo_max = 0;
    for (int i = 0; i < nchan; i++) {
        if (freq[i] < lo_min) lo_min = freq[i];
        if (freq[i] > lo_max) lo_max = freq[i];
    }
    lo = (lo_min + lo_max) / 2;

    ring_t iq, au;
    ring_init(&iq, sizeof(liquid_float_complex), 1 << 19);
    ring_init(&au, sizeof(float), 1 << 16);

    dsp_t dsp;
    dsp_init(&dsp, &iq, &au, DSP_FS_IN, DSP_FS_AUD, lo, kf, 75e-6f, nchan,
             220000.0f, 15000.0f, 1, 0.3f);
    for (int i = 0; i < nchan; i++) {
        dsp_set_chain(&dsp, i, freq[i]);
        dsp_set_gain(&dsp, i, 1.0f);
    }
    dsp_start(&dsp);

    /* generate all nchan FM carriers on one baseband */
    float dev = 50000.0f;
    double t = 0.0, dt = 1.0 / DSP_FS_IN;
    size_t total_in = (size_t)(TRACE_SEC * DSP_FS_IN);
    size_t n = 0;
    float phase[MAX_DEMOD];
    memset(phase, 0, sizeof(phase));
    while (n < total_in) {
        static liquid_float_complex buf[4096];
        for (int i = 0; i < 4096; i++) {
            liquid_float_complex acc = 0.0f;
            for (int c = 0; c < nchan; c++) {
                float m = sim_msg(t, c);
                phase[c] += 2.0f * (float)M_PI * m * (dev * dt);
                phase[c] -= 2.0f * (float)M_PI * floorf(phase[c] / (2.0f * (float)M_PI));
                float off = 2.0f * (float)M_PI * (float)((double)freq[c] - (double)lo) * dt;
                phase[c] += off;
                acc += cexpf(_Complex_I * phase[c]);
            }
            buf[i] = acc * (0.8f / (float)nchan);
            t += dt;
        }
        ring_write(&iq, buf, 4096);
        n += 4096;
        usleep(100);
    }
    dsp_stop(&dsp);
    dsp_join(&dsp);

    /* read the mixed audio out  (up to ~2s worth) */
    size_t cap = (size_t)(TRACE_SEC * DSP_FS_AUD);
    float *a = malloc(sizeof(float) * cap);
    size_t got = 0;
    while (got < cap) {
        size_t r = ring_read(&au, a + got, cap - got);
        if (r == 0) break;
        got += r;
    }

    /* peak search per expected tone.  The simulated message is a pure tone
     * at 400*(i+1) Hz, but the resampler queue tail leaves the audio a few
     * samples shy of the nominal length, so an exact-bin DFT over a fixed
     * window leaks.  Instead scan a small band around each expected tone and
     * report the strongest component, and pass if that peak is within 1% and
     * strong enough to be real signal. */
    size_t N = got < (size_t)(0.5 * DSP_FS_AUD) ? got : (size_t)(0.5 * DSP_FS_AUD);
    printf("got %zu audio samples (using %zu)\n", got, N);
    int fail = 0;
    for (int c = 0; c < nchan; c++) {
        float f0 = 400.0f * (float)(c + 1);
        double best = 0, bestf = f0;
        for (double f = f0 - 12.0; f <= f0 + 12.0; f += 0.2) {
            double re = 0, im = 0;
            for (size_t i = 0; i < N; i++) {
                double ph = 2.0 * M_PI * f * (double)i / DSP_FS_AUD;
                re += a[i] * cos(ph);
                im += a[i] * sin(ph);
            }
            re /= (double)N; im /= (double)N;
            double mag = 2.0 * sqrt(re * re + im * im);
            if (mag > best) { best = mag; bestf = f; }
        }
        double rel = fabs(bestf - f0) / f0;
        int ok = (rel < 0.01) && (best > 0.02);
        if (!ok) fail = 1;
        printf("chain %d (%.1f MHz, tone %.0f Hz): peak %.1f Hz |DFT| = %.3f  %s\n",
               c + 1, (double)freq[c] / 1e6, f0, bestf, best, ok ? "OK" : "FAIL");
    }
    printf(fail ? "RESULT: FAIL\n" : "RESULT: PASS\n");

    free(a);
    dsp_destroy(&dsp);
    ring_destroy(&iq);
    ring_destroy(&au);
    return 0;
}