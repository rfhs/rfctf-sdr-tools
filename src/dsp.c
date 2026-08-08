#include "dsp.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <complex.h>
#include <unistd.h>

static void build_chain(dsp_chan_t *c, uint64_t chan_freq,
                        float fs_in, float fs_audio, float kf, float deemp_tau,
                        int decim_m, float chan_bw_hz, float lpf_hz,
                        int agc_on, float agc_target)
{
    memset(c, 0, sizeof(*c));
    c->chan_freq = chan_freq;
    c->fs_band   = fs_in / (float)decim_m;   /* ~500 kHz by construction */
    c->gain      = 1.0f;
    c->on        = 1;
    c->agc_on     = agc_on;
    c->agc_target = agc_target;
    c->agc_env    = 0.0f;
    c->agc_gain   = 1.0f;

/* NCO: created at DC; the (chan_freq - lo_hz) offset is applied later by
 * dsp_set_chain() before the mix thread starts.  Setting a large offset
 * here is fine, but chan_freq=0 during build would push nco_crcf_constrain
 * out of range and hang.  LIQUID_VCO: table-driven oscillator, much
 * cheaper than per-sample sincos at 20 Msps. */
    c->mix = nco_crcf_create(LIQUID_VCO);
    nco_crcf_reset(c->mix);

    /* channel-selection FIR decimator: Kaiser windowed-sinc low-pass,
     * cutoff at +/-chan_bw/2, ~60 dB stopband starting half a cutoff
     * above the pass edge (before the 200/400 kHz-spaced neighbours).
     * The old order-6 Cheby2 IIR only had a 40 dB stopband, which let
     * loud adjacent carriers leak into the demod as static. */
    {
        const float As = 60.0f;
        float fc = 0.5f * chan_bw_hz / fs_in;     /* e.g. 110e3/20e6 */
        float ft = 1.5f * fc;                     /* stop edge */
        if (ft > 0.45f) ft = 0.45f;
        float df = ft - fc;                       /* transition width */
        unsigned n = (unsigned)ceilf((As - 7.95f) / (14.36f * df)) + 1;
        /* round up to a multiple of the decimation factor */
        n = ((n + (unsigned)decim_m - 1) / (unsigned)decim_m) * (unsigned)decim_m;
        float *h = malloc(n * sizeof(float));
        if (h) {
            liquid_firdes_kaiser(n, fc, As, 0.0f, h);
            c->decim = firdecim_crcf_create((unsigned)decim_m, h, n);
            free(h);
        }
        if (!c->decim)  /* fall back to liquid's own Kaiser design */
            c->decim = firdecim_crcf_create_kaiser((unsigned)decim_m, 6, As);
    }

    /* FM demodulator */
    c->dem = freqdem_create(kf);

    /* de-emphasis: one-pole low-pass, alpha = exp(-1/(RC*fs)); skipped when
     * deemp_tau <= 0 */
    if (deemp_tau > 0.0f) {
        float rc    = deemp_tau;                 /* e.g. 75e-6 */
        float alpha = expf(-1.0f / (rc * c->fs_band));
        float b[2]  = { 1.0f - alpha, 0.0f };
        float a[2]  = { 1.0f, -alpha };
        c->deemph = iirfilt_rrrf_create(b, 2, a, 2);
    } else {
        c->deemph = NULL;
    }

    /* audio resampler fs_band -> fs_audio */
    c->resamp = msresamp_rrrf_create(fs_audio / c->fs_band, 60.0f);

    /* audio low-pass at fs_audio: keeps the voice band, removes the FM
     * discriminator's high-frequency noise (its noise power rises with
     * f^2), the 19 kHz stereo pilot and the 38 kHz subcarrier remnants.
     * Kaiser, ~3 kHz transition, 60 dB stopband.  Without this filter the
     * 15-24 kHz hiss band lands on top of the voice. */
    c->lpf = NULL;
    if (lpf_hz > 0.0f && 2.0f * lpf_hz < fs_audio) {
        const float As = 60.0f;
        float fc = lpf_hz / fs_audio;             /* e.g. 15e3/48e3 */
        float df = 3000.0f / fs_audio;            /* transition width */
        unsigned n = (unsigned)ceilf((As - 7.95f) / (14.36f * df)) + 1;
        n |= 1;                                   /* odd length */
        float *h = malloc(n * sizeof(float));
        if (h) {
            liquid_firdes_kaiser(n, fc, As, 0.0f, h);
            c->lpf = firfilt_rrrf_create(h, n);
            free(h);
        }
    }
}

int dsp_init(dsp_t *d, ring_t *iq, ring_t *audio,
             float fs_in, float fs_audio, uint64_t lo_hz,
             float kf, float deemp_tau, int nchan,
             float chan_bw_hz, float lpf_hz,
             int agc_on, float agc_target)
{
    memset(d, 0, sizeof(*d));
    d->iq        = iq;
    d->audio     = audio;
    d->fs_in     = fs_in;
    d->fs_audio  = fs_audio;
    d->lo_hz     = lo_hz;
    d->kf        = kf;
    d->deemp_tau = deemp_tau;
    d->chan_bw_hz = chan_bw_hz;
    d->lpf_hz    = lpf_hz;
    d->nchan     = nchan;

    /* decimation that lands the band rate at ~DSP_FS_BAND regardless of
     * the capture rate, so every downstream stage is rate-independent */
    d->decim_m = (int)lroundf(fs_in / DSP_FS_BAND);
    if (d->decim_m < DSP_MIN_DECIM) d->decim_m = DSP_MIN_DECIM;

    pthread_mutex_init(&d->ch_lock, NULL);
    pthread_mutex_init(&d->sync_lock, NULL);
    pthread_cond_init(&d->cv_go, NULL);
    pthread_cond_init(&d->cv_done, NULL);
    d->generation = 0;
    d->done_count = 0;
    d->running = 0;

    d->chunk   = malloc(DSP_CHUNK * sizeof(liquid_float_complex));
    d->res_all = malloc((size_t)nchan * SCRATCH_RESAMP * sizeof(float));
    d->mix_buf = malloc(SCRATCH_RESAMP * sizeof(float));

    /* build all nchan chains; frequencies assigned via dsp_set_chain()
       before dsp_start() */
    for (int i = 0; i < nchan; i++) {
        build_chain(&d->ch[i], 0, fs_in, fs_audio, kf, deemp_tau,
                    d->decim_m, chan_bw_hz, lpf_hz, agc_on, agc_target);
    }
    return 0;
}

void dsp_destroy(dsp_t *d)
{
    for (int i = 0; i < d->nchan; i++) {
        dsp_chan_t *c = &d->ch[i];
        if (c->mix)    nco_crcf_destroy(c->mix);
        if (c->decim)  firdecim_crcf_destroy(c->decim);
        if (c->dem)    freqdem_destroy(c->dem);
        if (c->deemph) iirfilt_rrrf_destroy(c->deemph);
        if (c->resamp) msresamp_rrrf_destroy(c->resamp);
        if (c->lpf)    firfilt_rrrf_destroy(c->lpf);
    }
    free(d->chunk);
    free(d->res_all);
    free(d->mix_buf);
    pthread_cond_destroy(&d->cv_go);
    pthread_cond_destroy(&d->cv_done);
    pthread_mutex_destroy(&d->sync_lock);
    pthread_mutex_destroy(&d->ch_lock);
}

/* assign channel frequency to chain index; only call before dsp_start */
int dsp_set_chain(dsp_t *d, int idx, uint64_t chan_freq)
{
    if (idx < 0 || idx >= d->nchan) return -1;
    dsp_chan_t *c = &d->ch[idx];
    c->chan_freq = chan_freq;

    double offset = (double)chan_freq - (double)d->lo_hz;  /* Hz, +/-10 MHz */
    nco_crcf_reset(c->mix);
    nco_crcf_set_frequency(c->mix, (float)(2.0 * M_PI * offset / (double)d->fs_in));
    firdecim_crcf_reset(c->decim);
    freqdem_reset(c->dem);
    if (c->deemph) iirfilt_rrrf_reset(c->deemph);
    if (c->resamp) msresamp_rrrf_reset(c->resamp);
    if (c->lpf)    firfilt_rrrf_reset(c->lpf);
    c->agc_env  = 0.0f;
    c->agc_gain = 1.0f;
    return 0;
}

void dsp_set_gain(dsp_t *d, int idx, float gain)
{
    if (idx < 0 || idx >= d->nchan) return;
    pthread_mutex_lock(&d->ch_lock);
    d->ch[idx].gain = (gain < 0.0f) ? 0.0f : gain;
    d->ch[idx].on   = d->ch[idx].gain > 0.0f;
    pthread_mutex_unlock(&d->ch_lock);
}

float dsp_get_gain(dsp_t *d, int idx)
{
    if (idx < 0 || idx >= d->nchan) return 0.0f;
    return d->ch[idx].gain;
}

float dsp_get_level(dsp_t *d, int idx)
{
    if (idx < 0 || idx >= d->nchan) return 0.0f;
    pthread_mutex_lock(&d->ch_lock);
    float v = d->ch[idx].lvl_est;
    pthread_mutex_unlock(&d->ch_lock);
    return v;
}

float dsp_get_iq_level(dsp_t *d)
{
    pthread_mutex_lock(&d->ch_lock);
    float v = d->iq_lvl;
    pthread_mutex_unlock(&d->ch_lock);
    return v;
}

/* Per-chain worker: waits for each new input chunk, then runs its whole
 * demod pipeline on it.  Chains only read the shared chunk; all mutable
 * state is per-chain, so no locking is needed on the DSP path itself. */
static void *chain_thread(void *arg)
{
    dsp_worker_t *w = (dsp_worker_t *)arg;
    dsp_t *d = w->d;
    const int c = w->idx;
    dsp_chan_t *ch = &d->ch[c];
    float *res = d->res_all + (size_t)c * SCRATCH_RESAMP;

    static _Thread_local liquid_float_complex band[DSP_CHUNK];
    static _Thread_local float det[SCRATCH_BAND];

    uint64_t seen = 0;
    for (;;) {
        pthread_mutex_lock(&d->sync_lock);
        while (d->generation == seen && d->running)
            pthread_cond_wait(&d->cv_go, &d->sync_lock);
        if (!d->running) {
            pthread_mutex_unlock(&d->sync_lock);
            break;
        }
        seen = d->generation;
        size_t got = d->chunk_len;
        pthread_mutex_unlock(&d->sync_lock);

        float gain;
        pthread_mutex_lock(&d->ch_lock);
        gain = ch->gain;
        pthread_mutex_unlock(&d->ch_lock);

        size_t nband = got / (size_t)d->decim_m;
        unsigned ny = 0;

        if (gain > 0.0f && nband > 0) {
            /* shift channel to DC */
            nco_crcf_mix_block_down(ch->mix, d->chunk, band, (unsigned)got);

            /* decimate to fs_band (n*M in -> n out) */
            firdecim_crcf_execute_block(ch->decim, band, (unsigned)nband, band);

            /* FM demodulate */
            freqdem_demodulate_block(ch->dem, band, (unsigned)nband, det);

            /* per-chain demod level (pre gain, for diagnostics) */
            float acc = 0.0f;
            for (unsigned i = 0; i < nband; i++) acc += det[i] * det[i];
            acc /= (float)nband;
            pthread_mutex_lock(&d->ch_lock);
            ch->lvl_est = 0.9f * ch->lvl_est + 0.1f * sqrtf(acc);
            pthread_mutex_unlock(&d->ch_lock);

            /* de-emphasis (in place) */
            if (ch->deemph)
                iirfilt_rrrf_execute_block(ch->deemph, det, (unsigned)nband,
                                           det);

            /* resample to fs_audio */
            msresamp_rrrf_execute(ch->resamp, det, nband, res, &ny);

            if (ny > 0) {
                /* audio low-pass: strip discriminator hiss above the voice
                 * band, the 19 kHz pilot and 38 kHz subcarrier remnants */
                if (ch->lpf)
                    firfilt_rrrf_execute_block(ch->lpf, res, ny, res);

                /* audio AGC: level the voice independent of RF gain and of
                 * the (unknown) transmitter deviation.  Peak-envelope
                 * follower with fast-ish attack and slow release; gain is
                 * smoothed to avoid zipper noise and clamped so static is
                 * not boosted forever. */
                if (ch->agc_on) {
                    const float atk = 1.0f - expf(-1.0f / (0.010f * d->fs_audio));
                    const float rel = 1.0f - expf(-1.0f / (0.150f * d->fs_audio));
                    const float gmax = 100.0f;   /* +40 dB */
                    const float gmin = 0.25f;    /* -12 dB */
                    const float gsmooth = 0.002f;
                    for (unsigned i = 0; i < ny; i++) {
                        float m = fabsf(res[i]);
                        float a = (m > ch->agc_env) ? atk : rel;
                        ch->agc_env += a * (m - ch->agc_env);
                        float g = ch->agc_target / (ch->agc_env + 1e-6f);
                        if (g > gmax) g = gmax;
                        if (g < gmin) g = gmin;
                        ch->agc_gain += gsmooth * (g - ch->agc_gain);
                        res[i] *= ch->agc_gain;
                    }
                }

                /* per-chain volume (after the AGC so muting is instant and
                 * does not disturb the AGC's envelope) */
                for (unsigned i = 0; i < ny; i++) res[i] *= gain;
            }
        }
        d->chunk_ny[c] = ny;

        pthread_mutex_lock(&d->sync_lock);
        if (++d->done_count >= d->nchan)
            pthread_cond_signal(&d->cv_done);
        pthread_mutex_unlock(&d->sync_lock);
    }
    return NULL;
}

/* Reader/mixer: drains the IQ ring in whole multiples of the decimation
 * factor, publishes each chunk to the workers, then mixes their audio. */
static void *dsp_thread(void *arg)
{
    dsp_t *d = (dsp_t *)arg;

    while (d->running) {
        /* pull one chunk of complex input; poll so we can exit promptly.
         * Consume only whole multiples of the decimation factor.  Reading
         * "whatever is there" and flooring to a multiple of M silently
         * discards the remainder (up to M-1 samples per read): with the
         * HackRF's 4096-sample pushes this threw away ~0.4% of the stream
         * in little jumps, audible as crackle/static and measurable as a
         * ~0.35% time compression of the demodulated audio.  Leaving the
         * remainder in the ring costs nothing -- it is consumed next pass. */
        size_t navail = ring_avail(d->iq);
        size_t want = navail - (navail % (size_t)d->decim_m);
        size_t maxchunk = DSP_CHUNK - (DSP_CHUNK % (size_t)d->decim_m);
        if (want > maxchunk) want = maxchunk;
        if (want == 0) {
            usleep(200);
            continue;
        }
        size_t got = ring_read(d->iq, d->chunk, want);
        if (got == 0) {
            usleep(200);
            continue;
        }

        /* remove any DC bias in the chunk (HackRF DC spur / ADC offset).
         * The NCO mix cannot fix this because the spur sits at baseband 0,
         * so subtract the complex mean up front. */
        liquid_float_complex dc = 0.0f;
        for (size_t i = 0; i < got; i++) dc += d->chunk[i];
        dc /= (float)got;
        for (size_t i = 0; i < got; i++) d->chunk[i] -= dc;

        /* input level: mean |IQ| over the (DC-removed) chunk */
        float iq_lvl = 0.0f;
        for (size_t i = 0; i < got; i++)
            iq_lvl += cabsf(d->chunk[i]);
        iq_lvl /= (float)got;

        /* publish the chunk and wait for every chain worker */
        pthread_mutex_lock(&d->sync_lock);
        d->chunk_len = got;
        d->done_count = 0;
        d->generation++;
        pthread_cond_broadcast(&d->cv_go);
        while (d->done_count < d->nchan && d->running)
            pthread_cond_wait(&d->cv_done, &d->sync_lock);
        int completed = (d->done_count >= d->nchan);
        pthread_mutex_unlock(&d->sync_lock);
        if (!completed) continue;   /* shutdown raced the chunk; abandon it */

        /* mix the chains that produced audio this chunk */
        size_t nout = 0;
        int    nactive = 0;
        int    active[MAX_DEMOD];
        for (int c = 0; c < d->nchan; c++) {
            active[c] = (d->chunk_ny[c] > 0);
            if (active[c]) {
                if (nactive == 0 || d->chunk_ny[c] < nout)
                    nout = d->chunk_ny[c];
                nactive++;
            }
        }
        if (nactive == 0 || nout == 0) continue;
        for (unsigned j = 0; j < nout; j++) {
            float acc = 0.0f;
            for (int c = 0; c < d->nchan; c++)
                if (active[c])
                    acc += d->res_all[(size_t)c * SCRATCH_RESAMP + j];
            d->mix_buf[j] = acc;
        }
        size_t aw = ring_write(d->audio, d->mix_buf, nout);
        if (aw < nout) d->aud_drops += nout - aw;
        d->aud_produced += aw;

        /* commit input level for diagnostics */
        pthread_mutex_lock(&d->ch_lock);
        d->iq_lvl = iq_lvl;
        pthread_mutex_unlock(&d->ch_lock);
    }
    return NULL;
}

int dsp_start(dsp_t *d)
{
    d->running = 1;
    for (int i = 0; i < d->nchan; i++) {
        d->workers[i].d = d;
        d->workers[i].idx = i;
        if (pthread_create(&d->worker_th[i], NULL, chain_thread,
                           &d->workers[i]) != 0) {
            d->running = 0;
            pthread_mutex_lock(&d->sync_lock);
            pthread_cond_broadcast(&d->cv_go);
            pthread_mutex_unlock(&d->sync_lock);
            for (int k = 0; k < i; k++)
                pthread_join(d->worker_th[k], NULL);
            return -1;
        }
    }
    if (pthread_create(&d->thread, NULL, dsp_thread, d) != 0) {
        d->running = 0;
        pthread_mutex_lock(&d->sync_lock);
        pthread_cond_broadcast(&d->cv_go);
        pthread_mutex_unlock(&d->sync_lock);
        for (int i = 0; i < d->nchan; i++)
            pthread_join(d->worker_th[i], NULL);
        return -1;
    }
    return 0;
}

void dsp_stop(dsp_t *d)
{
    d->running = 0;
    /* wake any thread parked on either cond so everyone can exit */
    pthread_mutex_lock(&d->sync_lock);
    pthread_cond_broadcast(&d->cv_go);
    pthread_cond_broadcast(&d->cv_done);
    pthread_mutex_unlock(&d->sync_lock);
}

void dsp_join(dsp_t *d)
{
    pthread_join(d->thread, NULL);
    for (int i = 0; i < d->nchan; i++)
        pthread_join(d->worker_th[i], NULL);
}