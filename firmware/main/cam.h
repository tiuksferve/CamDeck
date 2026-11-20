#pragma once
#include <stdbool.h>
#include "esp_err.h"
#include "cJSON.h"

esp_err_t camx_init(void);
/* Tarefa do stream: envia quadros PKT_JPEG enquanto o stream estiver ligado. */
void camx_task(void *arg);

void camx_stream(bool on);
bool camx_streaming(void);
/* Ajuste do sensor ou do stream pelo nome ("brightness", "framesize", "fps_max"...).
 * 0 = feito, -1 = nome desconhecido, -2 = o sensor não suporta, -3 = valor inválido. */
int camx_set(const char *key, const cJSON *val);
/* Tira uma foto no tamanho pedido (nome, ex.: "UXGA"; NULL = o maior) e envia-a como PKT_SNAP. */
int camx_snap(const char *size);
/* Sensor, tamanhos e valores atuais de todos os ajustes. */
void camx_info_json(cJSON *o);
/* Estatísticas do stream desde a última chamada. */
void camx_stats(cJSON *o, float dt_s);
