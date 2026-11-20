/*
 * Ligação com o uConsole pela USB nativa do ESP32-S3 (USB-Serial-JTAG, 303a:1001).
 *
 * ESP -> host: pacotes binários com cabeçalho fixo de 20 bytes (little endian):
 *   0  magic  'C' 'D' 'K' 0xA5
 *   4  type   u8   (PKT_*)
 *   5  flags  u8   (reservado, 0)
 *   6  seq    u16  (contador por pacote, para o host contar perdas)
 *   8  len    u32  (bytes do conteúdo)
 *  12  ts     u32  (milissegundos desde o arranque, para sincronizar vídeo e áudio)
 *  16  crc    u32  (CRC-32 do conteúdo, o mesmo do zlib)
 * O host procura o magic, confere o tamanho e o CRC e, se falhar, volta a procurar
 * um byte à frente. Assim, o texto que o ROM escreve no arranque é simplesmente ignorado.
 *
 * host -> ESP: comandos JSON, um por linha ("\n"), até 1 KB.
 */
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>

#define PKT_JPEG   1   /* quadro do stream (JPEG) */
#define PKT_SNAP   2   /* foto pedida por "snap" (JPEG na resolução da foto) */
#define PKT_AUDIO  3   /* PCM 16 bits mono, 16 kHz */
#define PKT_JSON   4   /* resposta ou evento JSON */
#define PKT_LOG    5   /* linha de log em texto */

#define LINK_HDR_LEN 20

void link_init(void);
bool link_connected(void);
/* Envia um pacote inteiro (cabeçalho + conteúdo) sem intercalar com outros. false se a USB falhou. */
bool link_send(uint8_t type, const void *data, size_t len);
/* Envia um objeto JSON já em texto. */
bool link_send_json(const char *json);
/* Lê bytes recebidos (comandos); devolve quantos. */
int link_read(uint8_t *buf, size_t max, uint32_t timeout_ms);
/* Bytes enviados desde o arranque (para o débito). */
uint64_t link_tx_bytes(void);
uint32_t link_tx_errors(void);
