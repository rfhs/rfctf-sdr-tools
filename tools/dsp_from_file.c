/* dsp_from_file.c -- run the REAL sdisco DSP chain on a raw int8 I/Q dump and
 * write the demodulated audio to a .f32 file, mirroring src/main.c wiring so
 * bugs are reproduced faithfully.
 *
 * usage: dsp_from_file <dump.raw> <lo_hz> <chan_freq_hz> <n|0> <out.f32>
 */
#define _USE_MATH_DEFINES
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <complex.h>
#include <unistd.h>
#include <liquid/liquid.h>
#include "ring.h"
#include "dsp.h"

int main(int argc, char **argv)
{
    if (argc < 5) {
        fprintf(stderr, "usage: %s <dump.raw> <lo> <chan> <nchan> <out.f32>\n", argv[0]);
        return 2;
    }
    const char *path = argv[1];
    uint64_t lo = strtoull(argv[2], NULL, 0);
    uint64_t chan = strtoull(argv[3], NULL, 0);
    int nchan = atoi(argv[4]);
    /* liquid kf convention: kf = dev/fs_band normalizes full deviation to
     * ~unit amplitude (verified empirically); 75 kHz / 500 kHz = 0.15 */
    float kf = (float)(75000.0 / (double)DSP_FS_BAND);
    float deemph = 75e-6f;

    ring_t iq, au;
    ring_init(&iq, sizeof(liquid_float_complex), 1 << 19);
    ring_init(&au, sizeof(float), 1 << 24);

    dsp_t dsp;
    dsp_init(&dsp, &iq, &au, DSP_FS_IN, DSP_FS_AUD, lo, kf, deemph, nchan,
             220000.0f, 15000.0f, 1, 0.3f);
    for (int i = 0; i < nchan; i++) dsp_set_chain(&dsp, i, chan);
    for (int i = 0; i < nchan; i++) dsp_set_gain(&dsp, i, 1.0f);
    dsp_start(&dsp);

    uint64_t tgt_samp = 20000000ULL * 3;
    size_t total = 0;
    FILE *f = fopen(path, "rb");
    if (!f) { perror("open"); return 1; }
    int8_t buf[4096 * 2];
        static liquid_float_complex cbuf[4096];
    int done = 0;
    while (!done) {
        size_t got = fread(buf, 2, 4096, f);
        if (got == 0) { done = 1; break; }
        for (size_t i = 0; i < got; i++)
            cbuf[i] = (float)buf[2*i]/128.0f +
                      (float)buf[2*i+1]/128.0f * _Complex_I;
        /* write with backpressure so no samples are dropped */
        size_t off = 0;
        while (off < got) {
            size_t w = ring_write(&iq, cbuf + off, got - off);
            if (w == 0) { usleep(200); continue; }
            off += w;
        }
        total += got;
    }
    fclose(f);

    /* let the DSP drain: wait until the input ring is empty, then a little more */
    for (int tries = 0; tries < 600 && ring_avail(&iq) > 0; tries++)
        usleep(20000);
    usleep(500000);
    dsp_stop(&dsp);
    dsp_join(&dsp);

    FILE *o = fopen(argv[5], "wb");
    float *a = malloc(sizeof(float) * (1 << 24));
    size_t got_aud = ring_read(&au, a, 1 << 24);
    fwrite(a, sizeof(float), got_aud, o);
    fclose(o);
    printf("wrote %zu audio samples (%.2f s) to %s\n", got_aud, got_aud/DSP_FS_AUD, argv[5]);
    printf("IQ level %.3f  chan1 level %.3f\n",
           dsp_get_iq_level(&dsp), dsp_get_level(&dsp, 0));
    free(a);
    dsp_destroy(&dsp);
    ring_destroy(&iq);
    ring_destroy(&au);
    return 0;
}