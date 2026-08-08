#include "hackrx.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <liquid/liquid.h>

/* RX buffer holds float-complex samples; transfer buffers are converted in
 * chunks of this many samples */
#define RX_PUSH_CHUNK 4096

/* Default libhackrf transfer layout: interleaved int8 I/Q pairs */
static int rx_callback(hackrf_transfer *transfer)
{
    hackrx_t *h = (hackrx_t *)transfer->rx_ctx;

    if (!h->running) return -1;

    /* optionally save raw int8 I/Q for later off-line analysis */
    if (h->dump && h->dump_bytes < h->dump_cap) {
        size_t room = h->dump_cap - h->dump_bytes;
        size_t n = (size_t)transfer->valid_length < room
                       ? (size_t)transfer->valid_length : room;
        if (n > 0) {
            size_t w = fwrite(transfer->buffer, 1, n, h->dump);
            h->dump_bytes += w;
        }
        /* leave the file open; close it on hackrx_stop */
    }

    int pairs = transfer->valid_length / 2;
    if (pairs <= 0) return 0;

    int off = 0;
    while (off < pairs) {
        int n = RX_PUSH_CHUNK;
        if (n > pairs - off) n = pairs - off;

        /* convert int8 pairs to float complex in a temp buffer */
        static liquid_float_complex tmp[RX_PUSH_CHUNK];
        const int8_t *b = (const int8_t *)transfer->buffer;
        for (int i = 0; i < n; i++) {
            tmp[i] = ((float)b[2 * (off + i)]     / 128.0f) +
                     ((float)b[2 * (off + i) + 1] / 128.0f) * _Complex_I;
        }
        /* non-blocking write: if the DSP thread falls behind and the ring
         * is full, the tail of this block is dropped.  Count it so the
         * diag line can show it -- dropped RF is audible as crackle. */
        size_t w = ring_write(&h->iq, tmp, (size_t)n);
        if (w < (size_t)n) h->drops += (size_t)n - w;
        off += n;
    }
    return 0;
}

int hackrx_open(hackrx_t *h)
{
    memset(h, 0, sizeof(*h));   /* dump/error/drops must not be garbage */
    int r = hackrf_init();
    if (r != HACKRF_SUCCESS) {
        fprintf(stderr, "hackrf_init: %s\n", hackrf_error_name(r));
        return -1;
    }
    r = hackrf_open(&h->dev);
    if (r != HACKRF_SUCCESS) {
        fprintf(stderr, "hackrf_open: %s\n", hackrf_error_name(r));
        return -1;
    }
    /* 2^19 complex samples ~= 26 ms at 20 Msps: absorbs DSP thread
     * scheduling jitter.  The old 2^16 (3.3 ms) overflowed on any hiccup
     * and the dropped RF was audible as static. */
    ring_init(&h->iq, sizeof(liquid_float_complex), 1 << 19);
    return 0;
}

int hackrx_configure(hackrx_t *h, uint64_t lo_hz, double sample_rate,
                     uint32_t lna_gain, uint32_t vga_gain, int amp_enable)
{
    int r;
    r = hackrf_set_sample_rate(h->dev, sample_rate);
    if (r != HACKRF_SUCCESS) { fprintf(stderr, "set_sample_rate: %s\n", hackrf_error_name(r)); return -1; }
    r = hackrf_set_freq(h->dev, lo_hz);
    if (r != HACKRF_SUCCESS) { fprintf(stderr, "set_freq: %s\n", hackrf_error_name(r)); return -1; }
    r = hackrf_set_lna_gain(h->dev, lna_gain);
    if (r != HACKRF_SUCCESS) { fprintf(stderr, "lna_gain: %s\n", hackrf_error_name(r)); return -1; }
    r = hackrf_set_vga_gain(h->dev, vga_gain);
    if (r != HACKRF_SUCCESS) { fprintf(stderr, "vga_gain: %s\n", hackrf_error_name(r)); return -1; }
    r = hackrf_set_amp_enable(h->dev, amp_enable);
    if (r != HACKRF_SUCCESS) { fprintf(stderr, "amp: %s\n", hackrf_error_name(r)); return -1; }
    return 0;
}

int hackrx_set_freq(hackrx_t *h, uint64_t lo_hz)
{
    return hackrf_set_freq(h->dev, lo_hz);
}

int hackrx_start(hackrx_t *h)
{
    h->running = 1;
    int r = hackrf_start_rx(h->dev, rx_callback, h);
    if (r != HACKRF_SUCCESS) {
        fprintf(stderr, "start_rx: %s\n", hackrf_error_name(r));
        h->running = 0;
        return -1;
    }
    return 0;
}

int hackrx_stop(hackrx_t *h)
{
    h->running = 0;
    int r = hackrf_stop_rx(h->dev);
    if (r != HACKRF_SUCCESS) {
        fprintf(stderr, "stop_rx: %s\n", hackrf_error_name(r));
        return -1;
    }
    return 0;
}

void hackrx_close(hackrx_t *h)
{
    if (h->dump) { fclose(h->dump); h->dump = NULL; }
    if (h->dev) hackrf_close(h->dev);
    hackrf_exit();
    h->dev = NULL;
}

int hackrx_dump_open(hackrx_t *h, const char *path, size_t cap_bytes)
{
    h->dump = fopen(path, "wb");
    if (!h->dump) {
        fprintf(stderr, "dump: cannot open %s\n", path);
        return -1;
    }
    h->dump_bytes = 0;
    h->dump_cap = cap_bytes;
    return 0;
}

void hackrx_dump_close(hackrx_t *h)
{
    if (h->dump) {
        fflush(h->dump);
        fclose(h->dump);
        h->dump = NULL;
        printf("iq dump saved: %zu bytes\n", h->dump_bytes);
    }
}