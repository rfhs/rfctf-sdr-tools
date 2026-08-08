#ifndef DSP_H
#define DSP_H

#include <stdint.h>
#include <pthread.h>
#include <liquid/liquid.h>

#include "ring.h"
#include "channel.h"

/* Input sample rate ceiling (hackrf): 20 Msps covers the whole 908-927.5 MHz
 * band.  The runtime rate may be lower (see main.c): the decimation factor
 * is chosen so the post-decimation band rate is always DSP_FS_BAND, which
 * keeps every downstream stage (channel filter, de-emphasis, kf, audio
 * resampler) independent of the capture rate. */
#define DSP_FS_IN   20000000.0f
#define DSP_FS_BAND 500000.0f

/* smallest decimation factor supported (bounds scratch buffer sizing) */
#define DSP_MIN_DECIM 8

/* DSP thread reads at most this many complex samples at a time */
#define DSP_CHUNK   4000

/* scratch sizing: nband is largest at the smallest decimation factor */
#define SCRATCH_BAND   (DSP_CHUNK / DSP_MIN_DECIM + 64)
#define SCRATCH_RESAMP (SCRATCH_BAND * 4 + 64)

#define DSP_FS_AUD  48000.0f

/* one chain: full demod pipeline for a single selected channel */
typedef struct {
    uint64_t chan_freq;   /* tuned channel (RF) */

    /* DSP stages */
    nco_crcf      mix;      /* shift channel to DC */
    firdecim_crcf decim;    /* channel-select FIR, decimate to fs_band */
    freqdem       dem;      /* FM demodulator */
    iirfilt_rrrf  deemph;   /* de-emphasis one-pole */
    msresamp_rrrf resamp;   /* fs_band -> fs_audio */
    firfilt_rrrf  lpf;      /* audio low-pass at fs_audio (~15 kHz) */

    float fs_band;

    /* audio AGC state (dsp_thread only, no lock needed) */
    int   agc_on;
    float agc_target;      /* target peak envelope */
    float agc_env;         /* |x| envelope estimate */
    float agc_gain;        /* smoothed gain actually applied */

    /* per-chain controls, guarded by dsp_t.ch_lock */
    float gain;            /* 0 (muted) .. 2 */
    int   on;
    float lvl_est;         /* running demod RMS level (post-gain) */
} dsp_chan_t;

typedef struct dsp_s dsp_t;
typedef struct { dsp_t *d; int idx; } dsp_worker_t;

struct dsp_s {
    dsp_chan_t ch[MAX_DEMOD];
    int        nchan;         /* number of chains built */

    ring_t *iq;              /* shared input ring @ fs_in */
    ring_t *audio;           /* summed output ring @ fs_audio        */

    float    fs_in;
    float    fs_audio;
    uint64_t lo_hz;
    float    kf;
    float    deemp_tau;
    float    chan_bw_hz;   /* channel-selection filter width */
    float    lpf_hz;       /* audio low-pass cutoff (0 = off) */
    int      decim_m;      /* fs_in / fs_band, computed in dsp_init */

    /* per-chain gain lock */
    pthread_mutex_t ch_lock;
    int             chan_dirty;

    /* input diagnostics, written by dsp_thread, read under ch_lock */
    float    iq_lvl;         /* running mean |IQ| of the last chunk */

    /* audio ring overflow: mixed samples the ring could not take */
    volatile size_t aud_drops;
    /* total mixed samples written to the audio ring (rate diagnostics) */
    volatile size_t aud_produced;

    /* Threading: one reader/mixer thread plus one worker thread per
     * channel.  The reader fills chunk, bumps the generation, and every
     * worker demodulates its own chain from the same chunk in parallel
     * (the FIR decimator + NCO per channel are ~one core each at high
     * rates x many channels; a single thread could not sustain 20 Msps
     * with several channels selected). */
    volatile int running;
    pthread_t  thread;                  /* reader/mixer */
    pthread_t  worker_th[MAX_DEMOD];    /* per-chain workers */
    dsp_worker_t workers[MAX_DEMOD];

    liquid_float_complex *chunk;        /* DSP_CHUNK samples, reader-owned */
    size_t   chunk_len;
    size_t   chunk_ny[MAX_DEMOD];       /* per-chain audio count this chunk */
    float   *res_all;                   /* nchan x SCRATCH_RESAMP audio */
    float   *mix_buf;                   /* SCRATCH_RESAMP mix scratch */
    pthread_mutex_t sync_lock;
    pthread_cond_t  cv_go;
    pthread_cond_t  cv_done;
    uint64_t generation;
    int      done_count;
};

int  dsp_init(dsp_t *d, ring_t *iq, ring_t *audio,
              float fs_in, float fs_audio, uint64_t lo_hz,
              float kf, float deemp_tau, int nchan,
              float chan_bw_hz, float lpf_hz,
              int agc_on, float agc_target);
void dsp_destroy(dsp_t *d);
int  dsp_set_chain(dsp_t *d, int idx, uint64_t chan_freq);
void dsp_set_gain(dsp_t *d, int idx, float gain);
float dsp_get_gain(dsp_t *d, int idx);
float dsp_get_level(dsp_t *d, int idx);   /* running demod RMS level */
float dsp_get_iq_level(dsp_t *d);         /* running mean |IQ| */
int  dsp_start(dsp_t *d);
void dsp_stop(dsp_t *d);
void dsp_join(dsp_t *d);

#endif