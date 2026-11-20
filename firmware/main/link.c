#include "link.h"
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "driver/usb_serial_jtag.h"
#include "esp_rom_crc.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "esp_rom_sys.h"

#define TX_TIMEOUT_MS 300   /* sem o host a ler, desiste do pacote em vez de bloquear tudo */

static SemaphoreHandle_t s_tx_lock;
static uint16_t s_seq;
static uint64_t s_tx_bytes;
static uint32_t s_tx_errors;

static bool write_all(const uint8_t *p, size_t n)
{
    while (n) {
        /* O driver recusa (não parte) escritas maiores do que o espaço do buffer: blocos de 4 KB */
        size_t chunk = n > 4096 ? 4096 : n;
        int w = usb_serial_jtag_write_bytes(p, chunk, pdMS_TO_TICKS(TX_TIMEOUT_MS));
        if (w <= 0) return false;
        p += w;
        n -= w;
        s_tx_bytes += w;
    }
    return true;
}

bool link_connected(void)
{
    return usb_serial_jtag_is_connected();
}

bool link_send(uint8_t type, const void *data, size_t len)
{
    if (!link_connected()) return false;
    uint8_t h[LINK_HDR_LEN];
    uint32_t ts = (uint32_t)(esp_timer_get_time() / 1000);
    uint32_t crc = esp_rom_crc32_le(0, data, len);
    h[0] = 'C'; h[1] = 'D'; h[2] = 'K'; h[3] = 0xA5;
    h[4] = type; h[5] = 0;
    xSemaphoreTake(s_tx_lock, portMAX_DELAY);
    uint16_t seq = s_seq++;
    memcpy(h + 6, &seq, 2);
    uint32_t l32 = len;
    memcpy(h + 8, &l32, 4);
    memcpy(h + 12, &ts, 4);
    memcpy(h + 16, &crc, 4);
    bool ok = write_all(h, sizeof h) && write_all(data, len);
    if (!ok) s_tx_errors++;
    xSemaphoreGive(s_tx_lock);
    return ok;
}

bool link_send_json(const char *json)
{
    return link_send(PKT_JSON, json, strlen(json));
}

int link_read(uint8_t *buf, size_t max, uint32_t timeout_ms)
{
    return usb_serial_jtag_read_bytes(buf, max, pdMS_TO_TICKS(timeout_ms));
}

uint64_t link_tx_bytes(void) { return s_tx_bytes; }
uint32_t link_tx_errors(void) { return s_tx_errors; }

/* Os logs do ESP-IDF vão como pacotes PKT_LOG, para não sujarem o canal binário. */
static int log_vprintf(const char *fmt, va_list ap)
{
    char line[256];
    int n = vsnprintf(line, sizeof line, fmt, ap);
    if (n <= 0) return n;
    if (n >= (int)sizeof line) n = sizeof line - 1;
    while (n && (line[n - 1] == '\n' || line[n - 1] == '\r')) n--;
    if (n && s_tx_lock && !xPortInIsrContext()) link_send(PKT_LOG, line, n);
    return n;
}

/* O esp_rom_printf/ets_printf (avisos do driver da câmera, como "NO-EOI" ou "FB-OVF")
 * escreve carácter a carácter no canal do ROM; junta-os em linhas e manda-as como PKT_LOG. */
static char s_rom_line[160];
static size_t s_rom_n;

static void rom_putc(char c)
{
    if (c == '\r') return;
    if (c != '\n' && s_rom_n < sizeof s_rom_line) {
        s_rom_line[s_rom_n++] = c;
        return;
    }
    if (s_rom_n && !xPortInIsrContext() && xTaskGetSchedulerState() == taskSCHEDULER_RUNNING)
        link_send(PKT_LOG, s_rom_line, s_rom_n);
    s_rom_n = 0;
}

void link_init(void)
{
    s_tx_lock = xSemaphoreCreateMutex();
    usb_serial_jtag_driver_config_t cfg = {
        .tx_buffer_size = 32 * 1024,
        .rx_buffer_size = 2048,
    };
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&cfg));
    esp_log_set_vprintf(log_vprintf);
    esp_rom_install_channel_putc(1, rom_putc);
}
