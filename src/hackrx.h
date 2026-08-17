#ifndef HACKRX_H
#define HACKRX_H

#include <stdint.h>
#include <stdio.h>
#include <hackrf.h>

#include "ring.h"

typedef struct {
    ring_t         iq;          /* ring of liquid_float_complex */
    hackrf_device *dev;
    volatile int   running;
    volatile int   error;
    volatile size_t drops;      /* IQ samples dropped on ring overflow */
    FILE          *dump;        /* raw int8 I/Q dump (optional) */
    size_t         dump_bytes;
    size_t         dump_cap;
} hackrx_t;

int hackrx_open(hackrx_t *h);
int hackrx_configure(hackrx_t *h, uint64_t lo_hz, double sample_rate,
                     uint32_t lna_gain, uint32_t vga_gain, int amp_enable);
int hackrx_set_freq(hackrx_t *h, uint64_t lo_hz);
int hackrx_start(hackrx_t *h);
int hackrx_stop(hackrx_t *h);
void hackrx_close(hackrx_t *h);
int hackrx_dump_open(hackrx_t *h, const char *path, size_t cap_bytes);
void hackrx_dump_close(hackrx_t *h);

#endif