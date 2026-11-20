/*
 * CamDeck: firmware do XIAO ESP32-S3 Sense ligado ao uConsole só por USB.
 *
 * Sem Wi-Fi nem Bluetooth. A câmera manda JPEG, o microfone manda PCM e o uConsole
 * (host/camd.py) manda comandos JSON, um por linha. Protocolo em link.h.
 *
 * Comandos (todos aceitam "id", devolvido na resposta):
 *   {"cmd":"hello"}                      -> {"ev":"hello", fw, sensor, sizes, settings, ...}
 *   {"cmd":"info"}                       -> {"ev":"info", ...}  (o mesmo sem a versão)
 *   {"cmd":"stream","on":true}           -> liga/desliga os quadros PKT_JPEG
 *   {"cmd":"set","key":"brightness","val":1}  (também "framesize":"VGA" e "fps_max":15)
 *   {"cmd":"snap","size":"UXGA"}         -> PKT_SNAP e depois {"ev":"snap","r":0}
 *   {"cmd":"mic","on":true,"gain":4}     -> liga/desliga os blocos PKT_AUDIO
 *   {"cmd":"bench","ms":3000}            -> envia lixo o mais depressa possível (débito da USB)
 *   {"cmd":"reboot"}
 * Eventos: {"ev":"stat",...} a cada segundo.
 */
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_chip_info.h"
#include "driver/temperature_sensor.h"
#include "cJSON.h"
#include "link.h"
#include "cam.h"
#include "mic.h"

#define CAMDECK_FW_VERSION "0.1.0"
#define PKT_BENCH 6

static const char *TAG = "camdeck";
static temperature_sensor_handle_t s_tsens;
static bool s_cam_ok, s_mic_ok;

static void send_obj(cJSON *o)
{
    char *txt = cJSON_PrintUnformatted(o);
    if (txt) {
        link_send_json(txt);
        cJSON_free(txt);
    }
    cJSON_Delete(o);
}

static cJSON *reply(const char *ev, const cJSON *req)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "ev", ev);
    const cJSON *id = cJSON_GetObjectItem(req, "id");
    if (id) cJSON_AddItemToObject(o, "id", cJSON_Duplicate(id, true));
    return o;
}

static void add_info(cJSON *o)
{
    cJSON_AddBoolToObject(o, "cam_ok", s_cam_ok);
    cJSON_AddBoolToObject(o, "mic_ok", s_mic_ok);
    cJSON_AddBoolToObject(o, "streaming", camx_streaming());
    cJSON_AddBoolToObject(o, "mic", mic_enabled());
    cJSON_AddNumberToObject(o, "mic_gain", mic_gain());
    if (s_cam_ok) camx_info_json(o);
}

static void bench(int ms)
{
    static uint8_t junk[16384];
    int64_t end = esp_timer_get_time() + (int64_t)ms * 1000;
    while (esp_timer_get_time() < end && link_send(PKT_BENCH, junk, sizeof junk)) {}
}

static void handle(const char *line)
{
    cJSON *req = cJSON_Parse(line);
    if (!req) {
        ESP_LOGW(TAG, "comando inválido: %.60s", line);
        return;
    }
    const cJSON *jc = cJSON_GetObjectItem(req, "cmd");
    const char *cmd = cJSON_IsString(jc) ? jc->valuestring : "";

    if (!strcmp(cmd, "hello") || !strcmp(cmd, "info")) {
        cJSON *o = reply(cmd, req);
        if (cmd[0] == 'h') {
            esp_chip_info_t ci;
            esp_chip_info(&ci);
            cJSON_AddStringToObject(o, "fw", CAMDECK_FW_VERSION);
            cJSON_AddNumberToObject(o, "rev", ci.revision);
            cJSON_AddNumberToObject(o, "psram_total", heap_caps_get_total_size(MALLOC_CAP_SPIRAM));
        }
        add_info(o);
        send_obj(o);
    } else if (!strcmp(cmd, "stream")) {
        camx_stream(s_cam_ok && cJSON_IsTrue(cJSON_GetObjectItem(req, "on")));
        cJSON *o = reply("stream", req);
        cJSON_AddBoolToObject(o, "on", camx_streaming());
        send_obj(o);
    } else if (!strcmp(cmd, "set")) {
        const cJSON *k = cJSON_GetObjectItem(req, "key");
        const cJSON *v = cJSON_GetObjectItem(req, "val");
        int r = s_cam_ok ? camx_set(cJSON_IsString(k) ? k->valuestring : NULL, v) : -2;
        cJSON *o = reply("set", req);
        if (cJSON_IsString(k)) cJSON_AddStringToObject(o, "key", k->valuestring);
        cJSON_AddNumberToObject(o, "r", r);
        send_obj(o);
    } else if (!strcmp(cmd, "snap")) {
        const cJSON *sz = cJSON_GetObjectItem(req, "size");
        int r = s_cam_ok ? camx_snap(cJSON_IsString(sz) ? sz->valuestring : NULL) : -2;
        cJSON *o = reply("snap", req);
        cJSON_AddNumberToObject(o, "r", r);
        send_obj(o);
    } else if (!strcmp(cmd, "mic")) {
        const cJSON *on = cJSON_GetObjectItem(req, "on");
        const cJSON *g = cJSON_GetObjectItem(req, "gain");
        if (cJSON_IsBool(on)) mic_enable(s_mic_ok && cJSON_IsTrue(on));
        if (cJSON_IsNumber(g)) mic_set_gain(g->valueint);
        cJSON *o = reply("mic", req);
        cJSON_AddBoolToObject(o, "on", mic_enabled());
        cJSON_AddNumberToObject(o, "gain", mic_gain());
        send_obj(o);
    } else if (!strcmp(cmd, "bench")) {
        const cJSON *ms = cJSON_GetObjectItem(req, "ms");
        bool was = camx_streaming();
        camx_stream(false);
        vTaskDelay(pdMS_TO_TICKS(100));
        int64_t t0 = esp_timer_get_time();
        uint64_t b0 = link_tx_bytes();
        bench(cJSON_IsNumber(ms) ? ms->valueint : 3000);
        float dt = (esp_timer_get_time() - t0) / 1e6f;
        cJSON *o = reply("bench", req);
        cJSON_AddNumberToObject(o, "kbps", (link_tx_bytes() - b0) / dt / 1024.0f);
        send_obj(o);
        camx_stream(was);
    } else if (!strcmp(cmd, "reboot")) {
        send_obj(reply("reboot", req));
        vTaskDelay(pdMS_TO_TICKS(100));
        esp_restart();
    } else {
        cJSON *o = reply("error", req);
        cJSON_AddStringToObject(o, "msg", "unknown cmd");
        send_obj(o);
    }
    cJSON_Delete(req);
}

static void cmd_task(void *arg)
{
    static char line[1024];
    size_t n = 0;
    uint8_t buf[128];
    for (;;) {
        int got = link_read(buf, sizeof buf, 100);
        for (int i = 0; i < got; i++) {
            char c = buf[i];
            if (c == '\n' || c == '\r') {
                if (n) {
                    line[n] = 0;
                    handle(line);
                    n = 0;
                }
            } else if (n < sizeof line - 1) {
                line[n++] = c;
            } else {
                n = 0;   /* linha grande demais: descarta */
            }
        }
    }
}

static void stat_task(void *arg)
{
    int64_t last = esp_timer_get_time();
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        int64_t now = esp_timer_get_time();
        float dt = (now - last) / 1e6f;
        last = now;
        if (!link_connected()) continue;
        cJSON *o = cJSON_CreateObject();
        cJSON_AddStringToObject(o, "ev", "stat");
        cJSON_AddNumberToObject(o, "up", (double)(now / 1000000));
        if (s_cam_ok) camx_stats(o, dt);
        float t = 0;
        if (s_tsens && temperature_sensor_get_celsius(s_tsens, &t) == ESP_OK)
            cJSON_AddNumberToObject(o, "temp", (int)(t * 10) / 10.0);
        cJSON_AddNumberToObject(o, "heap", heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
        cJSON_AddNumberToObject(o, "psram", heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
        cJSON_AddNumberToObject(o, "tx_err", link_tx_errors());
        cJSON_AddBoolToObject(o, "streaming", camx_streaming());
        cJSON_AddBoolToObject(o, "mic", mic_enabled());
        cJSON_AddNumberToObject(o, "mic_peak", mic_peak());
        send_obj(o);
    }
}

void app_main(void)
{
    link_init();

    temperature_sensor_config_t tc = TEMPERATURE_SENSOR_CONFIG_DEFAULT(10, 80);
    if (temperature_sensor_install(&tc, &s_tsens) == ESP_OK) temperature_sensor_enable(s_tsens);
    else s_tsens = NULL;

    s_cam_ok = camx_init() == ESP_OK;
    s_mic_ok = mic_init() == ESP_OK;
    ESP_LOGI(TAG, "CamDeck %s: câmera %s, microfone %s", CAMDECK_FW_VERSION,
             s_cam_ok ? "ok" : "FALHOU", s_mic_ok ? "ok" : "FALHOU");

    xTaskCreatePinnedToCore(cmd_task, "cmd", 6144, NULL, 6, NULL, 0);
    xTaskCreatePinnedToCore(stat_task, "stat", 4096, NULL, 3, NULL, 0);
    if (s_mic_ok) xTaskCreatePinnedToCore(mic_task, "mic", 4096, NULL, 7, NULL, 0);
    if (s_cam_ok) xTaskCreatePinnedToCore(camx_task, "cam", 6144, NULL, 5, NULL, 1);

    /* Avisa o host de que o ESP (re)arrancou: ele volta a mandar a configuração. */
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "ev", "boot");
    cJSON_AddStringToObject(o, "fw", CAMDECK_FW_VERSION);
    send_obj(o);
}
