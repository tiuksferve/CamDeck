#pragma once
#include <stdbool.h>
#include "esp_err.h"

#define MIC_RATE 16000

esp_err_t mic_init(void);
void mic_task(void *arg);
void mic_enable(bool on);
bool mic_enabled(void);
/* Ganho digital (1 = sem ganho, até 16). */
void mic_set_gain(int g);
int mic_gain(void);
/* Nível de pico (0..32767) desde a última chamada. */
int mic_peak(void);
