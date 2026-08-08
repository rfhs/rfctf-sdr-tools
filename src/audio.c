#include "audio.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <portaudio.h>

#define FRAMES_PER_BUFFER 1024

/* Cushion the ring must hold before playback starts draining it.  In live
 * mode the producer is paced by the RF clock and can never catch up once it
 * falls behind, so without priming the ring level hovers near empty and
 * every burst-phase jitter becomes an audible underrun click.  ~0.34 s at
 * 48 kHz absorbs tens of ms of scheduling jitter; latency is irrelevant
 * for a listening tool. */
#define PRIME_FRAMES 16384
#define MAX_CH 2

typedef struct {
    audio_t *a;
    float    mono[FRAMES_PER_BUFFER];
    PaStream *stream;
} audio_cb_ctx_t;

static audio_cb_ctx_t g_ctx;

static int pa_callback(const void *input, void *output,
                       unsigned long frameCount,
                       const PaStreamCallbackTimeInfo *timeInfo,
                       PaStreamCallbackFlags statusFlags,
                       void *userData)
{
    (void)input; (void)timeInfo; (void)statusFlags;
    audio_cb_ctx_t *ctx = (audio_cb_ctx_t *)userData;
    audio_t *a = ctx->a;
    float *out = (float *)output;
    unsigned long n = (unsigned long)frameCount;

    /* priming: output silence until the ring holds a real cushion, and
     * re-prime if it ever drains completely (a stall would otherwise
     * restart the chronic near-empty underrun cycle) */
    if (!a->primed) {
        if (ring_avail(a->ring) >= PRIME_FRAMES) {
            a->primed = 1;
        } else {
            memset(out, 0, n * 2 * sizeof(float));
            return paContinue;
        }
    }

    /* read mono samples from the ring (may be fewer on underrun) */
    unsigned long got = (unsigned long)ring_read(a->ring, ctx->mono, n);
    if (got < n) a->underruns += (n - got);
    if (got == 0) a->primed = 0;
    float vol = a->volume;

    for (unsigned long i = 0; i < n; i++) {
        float s = (i < got) ? ctx->mono[i] : 0.0f;
        s *= vol;
        out[2 * i]     = s;   /* L */
        out[2 * i + 1] = s;   /* R */
    }
    return paContinue;
}

int audio_init(audio_t *a, ring_t *ring, float fs)
{
    (void)fs;
    memset(a, 0, sizeof(*a));
    a->ring = ring;
    a->volume = 1.0f;

    PaError err = Pa_Initialize();
    if (err != paNoError) {
        fprintf(stderr, "Pa_Initialize: %s\n", Pa_GetErrorText(err));
        return -1;
    }
    return 0;
}

int audio_start(audio_t *a)
{
    g_ctx.a = a;
    g_ctx.stream = NULL;

    PaStreamParameters out;
    memset(&out, 0, sizeof(out));
    out.device = Pa_GetDefaultOutputDevice();
    if (out.device == paNoDevice) {
        fprintf(stderr, "No default audio output device\n");
        return -1;
    }
    const PaDeviceInfo *info = Pa_GetDeviceInfo(out.device);
    if (!info) { fprintf(stderr, "Pa_GetDeviceInfo failed\n"); return -1; }

    out.channelCount = 2;
    out.sampleFormat = paFloat32;
    /* this is a monitoring tool: favour glitch-free over low latency */
    out.suggestedLatency = info->defaultHighOutputLatency;
    out.hostApiSpecificStreamInfo = NULL;

    PaError err = Pa_OpenDefaultStream(&g_ctx.stream, 0, 2, paFloat32, 48000,
                                       FRAMES_PER_BUFFER, pa_callback, &g_ctx);
    if (err != paNoError) {
        fprintf(stderr, "Pa_OpenDefaultStream: %s\n", Pa_GetErrorText(err));
        return -1;
    }
    err = Pa_StartStream(g_ctx.stream);
    if (err != paNoError) {
        fprintf(stderr, "Pa_StartStream: %s\n", Pa_GetErrorText(err));
        return -1;
    }
    a->running = 1;
    return 0;
}

void audio_stop(audio_t *a)
{
    a->running = 0;
    if (g_ctx.stream) {
        Pa_StopStream(g_ctx.stream);
        Pa_CloseStream(g_ctx.stream);
        g_ctx.stream = NULL;
    }
}

void audio_destroy(audio_t *a)
{
    if (g_ctx.stream) {
        Pa_StopStream(g_ctx.stream);
        Pa_CloseStream(g_ctx.stream);
        g_ctx.stream = NULL;
    }
    Pa_Terminate();
    a->running = 0;
}