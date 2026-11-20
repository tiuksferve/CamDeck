/*
 * Câmera do XIAO ESP32-S3 Sense (OV2640 ou OV3660, conforme o lote) em JPEG.
 *
 * A câmera é iniciada no maior tamanho do sensor: o esp32-camera reserva os buffers
 * pelo tamanho do arranque, e só assim a foto pode usar a resolução máxima enquanto
 * o stream corre num tamanho menor. Todo o acesso ao sensor passa por s_lock.
 */
#include "cam.h"
#include <string.h>
#include <stddef.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "esp_camera.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "link.h"

static const char *TAG = "cam";

/* Pinos da placa de expansão Sense */
#define PIN_XCLK  10
#define PIN_SIOD  40
#define PIN_SIOC  39
#define PIN_D7    48
#define PIN_D6    11
#define PIN_D5    12
#define PIN_D4    14
#define PIN_D3    16
#define PIN_D2    18
#define PIN_D1    17
#define PIN_D0    15
#define PIN_VSYNC 38
#define PIN_HREF  47
#define PIN_PCLK  13

static const char *FS_NAMES[] = {
    "96X96", "QQVGA", "128X128", "QCIF", "HQVGA", "240X240", "QVGA", "320X320", "CIF", "HVGA",
    "VGA", "SVGA", "XGA", "HD", "SXGA", "UXGA", "FHD", "P_HD", "P_3MP", "QXGA", "QHD", "WQXGA",
    "P_FHD", "QSXGA", "5MP",
};
_Static_assert(sizeof FS_NAMES / sizeof FS_NAMES[0] == FRAMESIZE_INVALID, "FS_NAMES");

static SemaphoreHandle_t s_lock;
static volatile bool s_streaming;
static framesize_t s_max_fs = FRAMESIZE_UXGA;
static framesize_t s_stream_fs = FRAMESIZE_VGA;
static int s_fps_max;                 /* 0 = sem limite */
static const char *s_sensor_name = "?";

static uint32_t s_frames, s_bytes, s_drops;

/* ── ajustes genéricos: campo em camera_status_t + função em sensor_t ───────── */
typedef int (*set_int_fn)(sensor_t *, int);
typedef struct {
    const char *key;
    uint16_t st_off;
    uint8_t st_size;
    uint8_t is_signed;
    uint16_t fn_off;
    int16_t min, max;
} knob_t;

#define KNOB(k, field, fn, sgn, lo, hi) \
    { k, offsetof(camera_status_t, field), sizeof(((camera_status_t *)0)->field), sgn, offsetof(sensor_t, fn), lo, hi }

static const knob_t KNOBS[] = {
    KNOB("quality",        quality,        set_quality,        0, 6, 63),
    KNOB("brightness",     brightness,     set_brightness,     1, -2, 2),
    KNOB("contrast",       contrast,       set_contrast,       1, -2, 2),
    KNOB("saturation",     saturation,     set_saturation,     1, -2, 2),
    KNOB("sharpness",      sharpness,      set_sharpness,      1, -2, 2),
    KNOB("denoise",        denoise,        set_denoise,        0, 0, 8),
    KNOB("special_effect", special_effect, set_special_effect, 0, 0, 6),
    KNOB("wb_mode",        wb_mode,        set_wb_mode,        0, 0, 4),
    KNOB("awb",            awb,            set_whitebal,       0, 0, 1),
    KNOB("awb_gain",       awb_gain,       set_awb_gain,       0, 0, 1),
    KNOB("aec",            aec,            set_exposure_ctrl,  0, 0, 1),
    KNOB("aec2",           aec2,           set_aec2,           0, 0, 1),
    KNOB("ae_level",       ae_level,       set_ae_level,       1, -2, 2),
    KNOB("aec_value",      aec_value,      set_aec_value,      0, 0, 1200),
    KNOB("agc",            agc,            set_gain_ctrl,      0, 0, 1),
    KNOB("agc_gain",       agc_gain,       set_agc_gain,       0, 0, 30),
    KNOB("gainceiling",    gainceiling,    set_gainceiling,    0, 0, 6),
    KNOB("bpc",            bpc,            set_bpc,            0, 0, 1),
    KNOB("wpc",            wpc,            set_wpc,            0, 0, 1),
    KNOB("raw_gma",        raw_gma,        set_raw_gma,        0, 0, 1),
    KNOB("lenc",           lenc,           set_lenc,           0, 0, 1),
    KNOB("hmirror",        hmirror,        set_hmirror,        0, 0, 1),
    KNOB("vflip",          vflip,          set_vflip,          0, 0, 1),
    KNOB("dcw",            dcw,            set_dcw,            0, 0, 1),
    KNOB("colorbar",       colorbar,       set_colorbar,       0, 0, 1),
};
#define N_KNOBS (sizeof KNOBS / sizeof KNOBS[0])

static int knob_get(sensor_t *s, const knob_t *k)
{
    const uint8_t *p = (const uint8_t *)&s->status + k->st_off;
    switch (k->st_size) {
    case 1: return k->is_signed ? *(const int8_t *)p : *p;
    case 2: return *(const uint16_t *)p;
    default: return *(const bool *)p;
    }
}

/* Intervalo do ajuste neste sensor: o OV3660 aceita mais passos do que o OV2640 e o
 * limite do ganho é o valor bruto do registo (16 = 1x; o estado guarda só 8 bits). */
static void knob_range(sensor_t *s, const knob_t *k, int *lo, int *hi)
{
    *lo = k->min;
    *hi = k->max;
    if (s->id.PID != OV3660_PID) return;
    static const struct { const char *key; int16_t lo, hi; } OV3660[] = {
        {"brightness", -3, 3}, {"contrast", -3, 3}, {"saturation", -4, 4}, {"sharpness", -3, 3},
        {"ae_level", -5, 5}, {"agc_gain", 0, 64}, {"gainceiling", 16, 255},
    };
    for (int i = 0; i < sizeof OV3660 / sizeof OV3660[0]; i++)
        if (!strcmp(k->key, OV3660[i].key)) { *lo = OV3660[i].lo; *hi = OV3660[i].hi; }
}

static set_int_fn knob_fn(sensor_t *s, const knob_t *k)
{
    return *(set_int_fn *)((uint8_t *)s + k->fn_off);
}

static framesize_t fs_by_name(const char *name)
{
    if (!name) return FRAMESIZE_INVALID;
    for (int i = 0; i < FRAMESIZE_INVALID; i++)
        if (!strcasecmp(name, FS_NAMES[i])) return (framesize_t)i;
    return FRAMESIZE_INVALID;
}

/* Tamanhos de 4:3 e 16:9 úteis até ao máximo do sensor (sem os quadrados e retratos). */
static bool fs_useful(framesize_t f)
{
    switch (f) {
    case FRAMESIZE_QQVGA: case FRAMESIZE_QVGA: case FRAMESIZE_CIF: case FRAMESIZE_HVGA:
    case FRAMESIZE_VGA: case FRAMESIZE_SVGA: case FRAMESIZE_XGA: case FRAMESIZE_HD:
    case FRAMESIZE_SXGA: case FRAMESIZE_UXGA: case FRAMESIZE_FHD: case FRAMESIZE_QXGA:
    case FRAMESIZE_QHD: case FRAMESIZE_QSXGA: case FRAMESIZE_5MP:
        return true;
    default:
        return false;
    }
}

/* Deita fora quadros antigos (depois de mudar o tamanho ficam no buffer quadros do anterior). */
static void flush_frames(int n)
{
    for (int i = 0; i < n; i++) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (fb) esp_camera_fb_return(fb);
    }
}

/* Deita fora pelo menos min_n quadros e durante pelo menos ms (exposição e cor a acertar). */
static void settle(int min_n, int ms)
{
    int64_t end = esp_timer_get_time() + (int64_t)ms * 1000;
    for (int i = 0; i < min_n || esp_timer_get_time() < end; i++) flush_frames(1);
}

static esp_err_t camx_start(framesize_t fs)
{
    camera_config_t c = {
        .pin_pwdn = -1, .pin_reset = -1, .pin_xclk = PIN_XCLK,
        .pin_sccb_sda = PIN_SIOD, .pin_sccb_scl = PIN_SIOC,
        .pin_d7 = PIN_D7, .pin_d6 = PIN_D6, .pin_d5 = PIN_D5, .pin_d4 = PIN_D4,
        .pin_d3 = PIN_D3, .pin_d2 = PIN_D2, .pin_d1 = PIN_D1, .pin_d0 = PIN_D0,
        .pin_vsync = PIN_VSYNC, .pin_href = PIN_HREF, .pin_pclk = PIN_PCLK,
        .xclk_freq_hz = 20000000,
        .ledc_timer = LEDC_TIMER_0, .ledc_channel = LEDC_CHANNEL_0,
        .pixel_format = PIXFORMAT_JPEG,
        .frame_size = fs,
        .jpeg_quality = 10,
        .fb_count = 2,
        .fb_location = CAMERA_FB_IN_PSRAM,
        .grab_mode = CAMERA_GRAB_LATEST,
    };
    return esp_camera_init(&c);
}

esp_err_t camx_init(void)
{
    s_lock = xSemaphoreCreateMutex();
    /* DMA direto para a PSRAM: sem isto, acima de SVGA (sem binning) o driver copia cada
     * bloco de 1 KB à mão, a tarefa dele ocupa o core todo e o ESP deixa de responder. */
    esp_camera_set_psram_mode(false);
    esp_err_t err = camx_start(FRAMESIZE_UXGA);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_camera_init falhou: 0x%x", err);
        return err;
    }
    sensor_t *s = esp_camera_sensor_get();
    camera_sensor_info_t *info = esp_camera_sensor_get_info(&s->id);
    if (info) {
        s_sensor_name = info->name;
        if (info->max_size > FRAMESIZE_UXGA && info->max_size < FRAMESIZE_INVALID) {
            /* OV3660/OV5640: reiniciar com os buffers do tamanho máximo */
            esp_camera_deinit();
            err = camx_start(info->max_size);
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "sem memória para %s, fica UXGA", FS_NAMES[info->max_size]);
                camx_start(FRAMESIZE_UXGA);
            } else {
                s_max_fs = info->max_size;
            }
            s = esp_camera_sensor_get();
        }
    }
    if (s->id.PID == OV3660_PID) {
        /* No Sense com OV3660 a imagem sai de pernas para o ar e muito saturada */
        s->set_vflip(s, 1);
        s->set_brightness(s, 1);
        s->set_saturation(s, -2);
    }
    s->set_framesize(s, s_stream_fs);
    ESP_LOGI(TAG, "sensor %s (PID 0x%x), máximo %s", s_sensor_name, s->id.PID, FS_NAMES[s_max_fs]);
    return ESP_OK;
}

void camx_stream(bool on) { s_streaming = on; }
bool camx_streaming(void) { return s_streaming; }

int camx_set(const char *key, const cJSON *val)
{
    if (!key || !val) return -3;
    sensor_t *s = esp_camera_sensor_get();
    if (!strcmp(key, "framesize")) {
        framesize_t f = cJSON_IsString(val) ? fs_by_name(val->valuestring)
                                            : (cJSON_IsNumber(val) ? (framesize_t)val->valueint : FRAMESIZE_INVALID);
        if (f >= FRAMESIZE_INVALID || f > s_max_fs) return -3;
        xSemaphoreTake(s_lock, portMAX_DELAY);
        int r = s->set_framesize(s, f);
        if (r == 0) { s_stream_fs = f; settle(3, 250); }   /* o 1.º quadro no tamanho novo vem misturado */
        xSemaphoreGive(s_lock);
        return r ? -2 : 0;
    }
    if (!strcmp(key, "fps_max")) {
        if (!cJSON_IsNumber(val) || val->valueint < 0 || val->valueint > 60) return -3;
        s_fps_max = val->valueint;
        return 0;
    }
    if (!cJSON_IsNumber(val) && !cJSON_IsBool(val)) return -3;
    int v = cJSON_IsBool(val) ? cJSON_IsTrue(val) : val->valueint;
    for (int i = 0; i < N_KNOBS; i++) {
        const knob_t *k = &KNOBS[i];
        if (strcmp(key, k->key)) continue;
        int lo, hi;
        knob_range(s, k, &lo, &hi);
        if (v < lo || v > hi) return -3;
        set_int_fn fn = knob_fn(s, k);
        if (!fn) return -2;
        xSemaphoreTake(s_lock, portMAX_DELAY);
        int r = fn(s, v);
        xSemaphoreGive(s_lock);
        return r ? -2 : 0;
    }
    return -1;
}

int camx_snap(const char *size)
{
    framesize_t f = size ? fs_by_name(size) : s_max_fs;
    if (f >= FRAMESIZE_INVALID || f > s_max_fs) return -3;
    sensor_t *s = esp_camera_sensor_get();
    xSemaphoreTake(s_lock, portMAX_DELAY);
    bool change = f != s_stream_fs;
    if (change) {
        s->set_framesize(s, f);
        settle(3, 400);    /* os primeiros quadros no tamanho novo ainda têm a exposição a acertar */
    }
    camera_fb_t *fb = esp_camera_fb_get();
    int r = -2;
    if (fb) {
        r = link_send(PKT_SNAP, fb->buf, fb->len) ? 0 : -4;
        esp_camera_fb_return(fb);
    }
    if (change) {
        s->set_framesize(s, s_stream_fs);
        flush_frames(2);
    }
    xSemaphoreGive(s_lock);
    return r;
}

void camx_task(void *arg)
{
    int64_t last = 0;
    for (;;) {
        if (!s_streaming || !link_connected()) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        if (s_fps_max > 0) {
            int64_t wait = last + 1000000 / s_fps_max - esp_timer_get_time();
            if (wait > 1000) vTaskDelay(pdMS_TO_TICKS(wait / 1000));
        }
        xSemaphoreTake(s_lock, portMAX_DELAY);
        camera_fb_t *fb = esp_camera_fb_get();
        if (fb) {
            last = esp_timer_get_time();
            if (link_send(PKT_JPEG, fb->buf, fb->len)) {
                s_frames++;
                s_bytes += fb->len;
            } else {
                s_drops++;
            }
            esp_camera_fb_return(fb);
        }
        xSemaphoreGive(s_lock);
        if (!fb) vTaskDelay(pdMS_TO_TICKS(5));
        else taskYIELD();
    }
}

void camx_info_json(cJSON *o)
{
    sensor_t *s = esp_camera_sensor_get();
    cJSON_AddStringToObject(o, "sensor", s_sensor_name);
    cJSON_AddNumberToObject(o, "pid", s->id.PID);
    cJSON_AddStringToObject(o, "max", FS_NAMES[s_max_fs]);
    cJSON_AddStringToObject(o, "framesize", FS_NAMES[s_stream_fs]);
    cJSON_AddNumberToObject(o, "fps_max", s_fps_max);
    cJSON *sizes = cJSON_AddArrayToObject(o, "sizes");
    for (int i = 0; i <= s_max_fs; i++) {
        if (!fs_useful(i)) continue;
        cJSON *e = cJSON_CreateObject();
        cJSON_AddStringToObject(e, "name", FS_NAMES[i]);
        cJSON_AddNumberToObject(e, "w", resolution[i].width);
        cJSON_AddNumberToObject(e, "h", resolution[i].height);
        cJSON_AddItemToArray(sizes, e);
    }
    cJSON *set = cJSON_AddObjectToObject(o, "settings");
    cJSON *rng = cJSON_AddObjectToObject(o, "ranges");
    for (int i = 0; i < N_KNOBS; i++) {
        const knob_t *k = &KNOBS[i];
        if (!knob_fn(s, k)) continue;   /* este sensor não tem o ajuste */
        cJSON_AddNumberToObject(set, k->key, knob_get(s, k));
        int lo, hi;
        knob_range(s, k, &lo, &hi);
        cJSON *r = cJSON_CreateIntArray((const int[]){lo, hi}, 2);
        cJSON_AddItemToObject(rng, k->key, r);
    }
}

void camx_stats(cJSON *o, float dt_s)
{
    cJSON_AddNumberToObject(o, "fps", dt_s > 0 ? s_frames / dt_s : 0);
    cJSON_AddNumberToObject(o, "kbps", dt_s > 0 ? s_bytes / dt_s / 1024.0f : 0);
    cJSON_AddNumberToObject(o, "avg_kb", s_frames ? s_bytes / (float)s_frames / 1024.0f : 0);
    cJSON_AddNumberToObject(o, "drops", s_drops);
    s_frames = s_bytes = 0;
}
