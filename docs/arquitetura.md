# Arquitetura e protocolo

```
XIAO ESP32-S3 Sense                       uConsole CM5 (camd)                       browser
 OV3660 ──JPEG──┐                          CamLink (thread de leitura)
 mic PDM ─PCM───┼── USB-Serial-JTAG ──────► CameraService ─┬─ /stream.mjpg (MJPEG) ──► <img>
 comandos ◄─────┘   (pacotes binários)     Recorder ───────┤─ /api/* (JSON)
                                           Media ──────────┘─ /api/events (SSE)
```

## Pacotes ESP → uConsole

Cabeçalho fixo de 20 bytes, little endian (`firmware/main/link.h`, `host/camlink.py`):

| offset | campo | tipo | notas |
|---|---|---|---|
| 0 | magic | 4 bytes | `C` `D` `K` `0xA5` |
| 4 | type | u8 | 1 JPEG do vídeo, 2 foto, 3 áudio, 4 JSON, 5 log, 6 bench |
| 5 | flags | u8 | 0 |
| 6 | seq | u16 | contador por pacote (o host conta as perdas) |
| 8 | len | u32 | bytes do conteúdo |
| 12 | ts | u32 | ms desde o arranque do ESP (hora do **envio**) |
| 16 | crc | u32 | CRC-32 do conteúdo (igual ao `zlib.crc32`) |

O host procura o magic, valida o tamanho e o CRC, e em caso de erro avança um byte. O texto que o
ROM escreve no arranque é ignorado assim. Os logs do ESP-IDF e o `ets_printf` dos drivers (ex.:
`cam_hal: NO-SOI`) chegam como pacotes de log e aparecem no journal do camd como `esp: ...`.

## Comandos uConsole → ESP

JSON, um por linha. Todos aceitam `"id"`, devolvido na resposta.

| comando | resposta |
|---|---|
| `{"cmd":"hello"}` | firmware, sensor, tamanhos, ajustes e intervalos |
| `{"cmd":"info"}` | o mesmo, sem a versão |
| `{"cmd":"stream","on":true}` | `{"ev":"stream","on":true}` |
| `{"cmd":"set","key":"brightness","val":1}` | `{"ev":"set","r":0}` (`-1` desconhecido, `-2` não suportado, `-3` valor inválido) |
| `{"cmd":"snap","size":"QXGA"}` | pacote tipo 2 e depois `{"ev":"snap","r":0}` |
| `{"cmd":"mic","on":true,"gain":4}` | `{"ev":"mic","on":true,"gain":4}` |
| `{"cmd":"bench","ms":3000}` | débito máximo da USB em KB/s |
| `{"cmd":"reboot"}` | reinicia |

Eventos espontâneos: `{"ev":"boot"}` no arranque e `{"ev":"stat",...}` a cada segundo (fps, KB/s,
temperatura, memória, falhas de envio, pico do microfone).

## Decisões e cuidados

- **Escritas na USB em blocos de 4 KB.** O driver `usb_serial_jtag` recusa (não parte) escritas
  maiores do que o espaço livre do buffer: um quadro de 32 KB num buffer de 32 KB falhava sempre.
- **PSRAM DMA desligado.** Com o OV3660 acima de SVGA, o modo de DMA direto para a PSRAM dá `NO-SOI`
  em todos os quadros. No modo normal o driver copia cada bloco à mão; por isso a tarefa dele corre
  no core 1 (`CONFIG_CAMERA_CORE1`), longe dos comandos, do estado e do microfone (core 0). No core 0
  bloqueava o ESP inteiro.
- **Câmara iniciada no tamanho máximo** (QXGA no OV3660): o esp32-camera reserva os buffers pelo
  tamanho do arranque, e só assim a foto pode usar a resolução máxima com o vídeo num tamanho menor.
- **Depois de mudar de resolução**, o primeiro quadro vem misturado e a exposição demora a acertar:
  o firmware descarta quadros durante 250 ms (vídeo) ou 400 ms (foto).
- **Intervalos por sensor:** o OV3660 aceita brilho ±3, saturação ±4, compensação ±5, ganho 0–64, e
  o limite do ganho é o valor bruto do registo (16 = 1×). O firmware manda os intervalos no `info`
  e a interface constrói os controlos a partir deles.
- **Abrir a porta reinicia o ESP** (o kernel ativa DTR/RTS ao abrir). O camd configura-o à
  ligação e ignora o `boot` que chega logo a seguir.
- **Vídeo só quando é preciso:** o stream liga com o primeiro espectador ou uma gravação e desliga
  10 s depois do último (`idle_stop_s`).
- **Som da gravação:** o carimbo de tempo é o do envio, e vários blocos podem sair seguidos depois
  de um quadro grande. O relógio só alinha o primeiro bloco com o primeiro quadro; o resto é escrito
  em sequência (o I2S dá exatamente 16 000 amostras/s). No MP4, o som é completado com silêncio
  (`apad`) até ao fim do vídeo.
- **MP4 por software:** o CM5 não tem codificador H.264 por hardware; o libx264 `veryfast` converte
  8 s de HD em ~3 s.
