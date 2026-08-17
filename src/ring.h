#ifndef RING_H
#define RING_H

#include <stddef.h>
#include <stdint.h>
#include <pthread.h>

typedef struct {
    uint8_t  *buf;
    size_t    elem_size;
    size_t    cap;      /* capacity in elements (power of two) */
    size_t    mask;
    size_t    head;     /* producer write index */
    size_t    tail;     /* consumer read index */
    pthread_mutex_t lock;
    pthread_cond_t  cv;
} ring_t;

void ring_init(ring_t *r, size_t elem_size, size_t cap_pow2);
void ring_destroy(ring_t *r);

/* non-blocking, returns number of elements copied (may be < n) */
size_t ring_avail(ring_t *r);
size_t ring_write(ring_t *r, const void *data, size_t n);
size_t ring_read(ring_t *r, void *out, size_t n);

/* blocking variants (blocks while insufficient space/data) */
void ring_write_block(ring_t *r, const void *data, size_t n);
size_t ring_read_block(ring_t *r, void *out, size_t n);

#endif