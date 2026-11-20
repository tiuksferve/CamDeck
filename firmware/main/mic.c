/*
 * Microfone PDM do Sense (CLK GPIO42, DATA GPIO41): PCM 16 bits mono a 16 kHz,
 * enviado em blocos de 40 ms (PKT_AUDIO). O DMA guarda ~0,5 s, que cobre o tempo em
 * que a USB está ocupada com um quadro grande ou uma foto.
 */
#include "mic.h"
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/i2s_pdm.h"
#include "esp_log.h"
#include "link.h"

#define BLOCK_SAMPLES (MIC_RATE / 25)   /* 40 ms */


static i2s_chan_handle_t s_rx;
static volatile bool s_on;
static volatile int s_gain = 4;
static volatile int s_peak;

esp_err_t mic_init(void)
{
    i2s_chan_config_t cc = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    cc.dma_desc_num = 8;
    cc.dma_frame_num = 1024;
    esp_err_t err = i2s_new_channel(&cc, NULL, &s_rx);
    if (err) return err;
    i2s_pdm_rx_config_t pc = {
        .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(MIC_RATE),
        .slot_cfg = I2S_PDM_RX_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .clk = GPIO_NUM_42,
            .din = GPIO_NUM_41,
            .invert_flags = { .clk_inv = false },
        },
    };
    err = i2s_channel_init_pdm_rx_mode(s_rx, &pc);
    if (err) return err;
    return i2s_channel_enable(s_rx);
}

void mic_enable(bool on) { s_on = on; }
bool mic_enabled(void) { return s_on; }
void mic_set_gain(int g) { s_gain = g < 1 ? 1 : (g > 16 ? 16 : g); }
int mic_gain(void) { return s_gain; }

int mic_peak(void)
{
    int p = s_peak;
    s_peak = 0;
    return p;
}

void mic_task(void *arg)
{
    static int16_t buf[BLOCK_SAMPLES];
    for (;;) {
        size_t got = 0;
        if (!s_rx || i2s_channel_read(s_rx, buf, sizeof buf, &got, pdMS_TO_TICKS(200)) != ESP_OK || !got) {
            vTaskDelay(pdMS_TO_TICKS(50));
            continue;
        }
        int n = got / 2, g = s_gain, peak = s_peak;
        for (int i = 0; i < n; i++) {
            int v = buf[i] * g;
            if (v > 32767) v = 32767;
            else if (v < -32768) v = -32768;
            buf[i] = v;
            int a = abs(v);
            if (a > peak) peak = a;
        }
        s_peak = peak;
        if (s_on && link_connected()) link_send(PKT_AUDIO, buf, got);
    }
}
