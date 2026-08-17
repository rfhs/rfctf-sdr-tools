#include "channel.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

/* Silent Disco system: 5 transmitters (A-E) x 9 channels each */
static const char * tx_names[5] = { "A", "B", "C", "D", "E" };

/* channel frequencies in MHz, row = transmitter (A..E), col = slot (1..9)
 * from the TX1345-MA..ME datasheet channel map */
static const double chan_tbl[5][9] = {
    { 908.1, 909.9, 911.8, 913.6, 915.1, 920.7, 922.8, 924.7, 926.2 }, /* A */
    { 908.4, 910.3, 912.2, 913.9, 915.5, 921.2, 923.1, 925.0, 926.5 }, /* B */
    { 908.7, 910.8, 912.7, 914.2, 915.8, 921.5, 923.4, 925.3, 926.8 }, /* C */
    { 909.2, 911.1, 913.0, 914.5, 920.1, 921.9, 923.8, 925.6, 927.1 }, /* D */
    { 909.5, 911.4, 913.3, 914.8, 920.4, 922.3, 924.2, 925.9, 927.5 }, /* E */
};

void channel_init(channel_table_t *t)
{
    int n = 0;
    for (int tx = 0; tx < 5 && n < MAX_CHANNELS; tx++) {
        for (int slot = 1; slot <= 9 && n < MAX_CHANNELS; slot++) {
            channel_t *c = &t->list[n];
            c->freq_hz    = (uint64_t)llround(chan_tbl[tx][slot-1] * 1e6);
            snprintf(c->name, sizeof(c->name), "%c%1d",
                     tx_names[tx][0], slot);
            n++;
        }
    }
    t->count = n;
}

/* Ultra-900 "U9" unit, US model (HP2310-U). 10 selectable channels + 3
 * alternate ("A") channels. Frequencies per the U9 US datasheet. */
typedef struct {
    const char *name;
    double      freq_mhz;
} u9_chan_t;

static const u9_chan_t u9_tbl[] = {
    { "CH1", 920.1 }, { "CH2", 920.7 }, { "CH3", 921.2 }, { "CH4", 921.9 },
    { "CH5", 922.3 }, { "CH6", 922.8 }, { "CH7", 923.4 }, { "CH8", 924.2 },
    { "CH9", 924.7 }, { "CH10", 925.9 },
    { "A1", 920.5 },  { "A2", 922.4 },  { "A3", 926.7 },
};

int channel_init_u9(channel_table_t *t)
{
    const int n = (int)(sizeof(u9_tbl) / sizeof(u9_tbl[0]));
    for (int i = 0; i < n && i < MAX_CHANNELS; i++) {
        channel_t *c       = &t->list[i];
        c->freq_hz          = (uint64_t)llround(u9_tbl[i].freq_mhz * 1e6);
        snprintf(c->name, sizeof(c->name), "%s", u9_tbl[i].name);
    }
    t->count = n;
    return t->count;
}

int channel_find(const channel_table_t *t, const char *name)
{
    for (int i = 0; i < t->count; i++) {
        if (strcasecmp(t->list[i].name, name) == 0) return i;
    }
    return -1;
}

int channel_lookup(const channel_table_t *t, uint64_t freq_hz)
{
    /* could binary search since table is sorted by slot rows;
     * linear scan is fine for 45 entries */
    for (int i = 0; i < t->count; i++) {
        if (t->list[i].freq_hz == freq_hz) return i;
    }
    return -1;
}