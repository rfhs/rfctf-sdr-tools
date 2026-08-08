#ifndef CHANNEL_H
#define CHANNEL_H

#include <stdint.h>

#define MAX_CHANNELS 64
#define MAX_DEMOD    10

typedef struct {
    uint64_t freq_hz;   /* center frequency of the FM channel */
    char     name[48];
} channel_t;

typedef struct {
    channel_t list[MAX_CHANNELS];
    int       count;
} channel_table_t;

void channel_init(channel_table_t *t);               /* TX1345 A-E map (default) */
int  channel_init_u9(channel_table_t *t);            /* Ultra-900 U9 map; returns count */
int  channel_find(const channel_table_t *t, const char *name); /* index, or -1 */
int  channel_lookup(const channel_table_t *t, uint64_t freq_hz); /* index, or -1 */

#endif