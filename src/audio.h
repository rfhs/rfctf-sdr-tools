#ifndef AUDIO_H
#define AUDIO_H

#include "ring.h"

typedef struct {
    ring_t     *ring;      /* float mono at fs_audio (already summed) */
    float       volume;    /* master volume (0..2) */
    volatile int running;
    volatile int primed;   /* ring cushion built; safe to drain */
    volatile unsigned long underruns;  /* frames PortAudio wanted but the
                                          ring could not supply */
} audio_t;

int     audio_init(audio_t *a, ring_t *ring, float fs);
int     audio_start(audio_t *a);
void    audio_stop(audio_t *a);
void    audio_destroy(audio_t *a);

#endif