#include "ring.h"
#include <stdlib.h>
#include <string.h>

void ring_init(ring_t *r, size_t elem_size, size_t cap_pow2)
{
    size_t cap = 1;
    while (cap < cap_pow2) cap <<= 1;
    r->buf = malloc(cap * elem_size);
    r->elem_size = elem_size;
    r->cap = cap;
    r->mask = cap - 1;
    r->head = 0;
    r->tail = 0;
    pthread_mutex_init(&r->lock, NULL);
    pthread_cond_init(&r->cv, NULL);
}

void ring_destroy(ring_t *r)
{
    free(r->buf);
    pthread_mutex_destroy(&r->lock);
    pthread_cond_destroy(&r->cv);
    r->buf = NULL;
    r->cap = 0;
}

size_t ring_avail(ring_t *r)
{
    size_t n;
    pthread_mutex_lock(&r->lock);
    n = (r->head - r->tail);
    pthread_mutex_unlock(&r->lock);
    return n;
}

size_t ring_space(ring_t *r)
{
    size_t n;
    pthread_mutex_lock(&r->lock);
    n = (r->tail + r->cap - r->head);
    pthread_mutex_unlock(&r->lock);
    return n;
}

size_t ring_write(ring_t *r, const void *data, size_t n)
{
    pthread_mutex_lock(&r->lock);
    size_t space = (r->tail + r->cap - r->head);
    if (n > space) n = space;
    size_t written = n;
    /* copy in at most two contiguous runs (wrap): per-element memcpy at
     * 20 Msamples/s was a measurable CPU load on both endpoints */
    while (n) {
        size_t idx = r->head & r->mask;
        size_t run = r->cap - idx;
        if (run > n) run = n;
        memcpy(r->buf + idx * r->elem_size, data, run * r->elem_size);
        data = (const uint8_t *)data + run * r->elem_size;
        r->head += run;
        n -= run;
    }
    pthread_cond_broadcast(&r->cv);
    pthread_mutex_unlock(&r->lock);
    return written;
}

size_t ring_read(ring_t *r, void *out, size_t n)
{
    pthread_mutex_lock(&r->lock);
    size_t avail = (r->head - r->tail);
    if (n > avail) n = avail;
    size_t got = n;
    while (n) {
        size_t idx = r->tail & r->mask;
        size_t run = r->cap - idx;
        if (run > n) run = n;
        memcpy(out, r->buf + idx * r->elem_size, run * r->elem_size);
        out = (uint8_t *)out + run * r->elem_size;
        r->tail += run;
        n -= run;
    }
    pthread_cond_broadcast(&r->cv);
    pthread_mutex_unlock(&r->lock);
    return got;
}

void ring_write_block(ring_t *r, const void *data, size_t n)
{
    size_t done = 0;
    pthread_mutex_lock(&r->lock);
    while (done < n) {
        size_t space = (r->tail + r->cap - r->head);
        if (space == 0) {
            pthread_cond_wait(&r->cv, &r->lock);
            continue;
        }
        size_t step = (n - done < space) ? (n - done) : space;
        for (size_t i = 0; i < step; i++) {
            memcpy(r->buf + (r->head & r->mask) * r->elem_size,
                   (const uint8_t *)data + (done + i) * r->elem_size,
                   r->elem_size);
            r->head++;
        }
        done += step;
    }
    pthread_cond_broadcast(&r->cv);
    pthread_mutex_unlock(&r->lock);
}

size_t ring_read_block(ring_t *r, void *out, size_t n)
{
    size_t got = 0;
    pthread_mutex_lock(&r->lock);
    while (got < n) {
        size_t avail = (r->head - r->tail);
        if (avail == 0) {
            pthread_cond_wait(&r->cv, &r->lock);
            continue;
        }
        size_t step = (n - got < avail) ? (n - got) : avail;
        for (size_t i = 0; i < step; i++) {
            memcpy((uint8_t *)out + (got + i) * r->elem_size,
                   r->buf + (r->tail & r->mask) * r->elem_size,
                   r->elem_size);
            r->tail++;
        }
        got += step;
    }
    pthread_cond_broadcast(&r->cv);
    pthread_mutex_unlock(&r->lock);
    return got;
}