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

## Programas locais (DashDeck)

- **Token local:** o camd cria `~/.config/camdeck/local-token` (permissões 600) ao arrancar. Um pedido
  com o cabeçalho `X-CamDeck-Token` igual ao ficheiro entra sem palavra-passe, mas só a partir do
  próprio uConsole (`127.0.0.1` ou `::1`). É assim que o `dashd` do DashDeck usa a câmara (box Câmara).
- **`GET /api/frame.jpg?after=n`:** espera (até 5 s) por um quadro mais novo do que `n` e devolve o
  número dele no cabeçalho `X-Frame`. Sem `after`, devolve logo o último quadro. Um visor que pede
  sempre com o `X-Frame` anterior recebe cada quadro uma vez, à cadência da câmara.

## Análise de imagem (IA)

```
camd (Python do sistema)                         worker (venv de IA: onnxruntime + OpenCV)
 CameraService ── último JPEG ──► AnalyzerService ══ stdin ═══► Analyzer
                                   ├─ SSE "ai"     ◄═ stdout ══  YOLO11n · YuNet · FER+ · genderage · SFace · QR
                                   ├─ EventLog (~/.local/share/camdeck/events)
                                   └─ FaceDB  (~/.local/share/camdeck/faces) ── galeria ──► worker
```

- **Processo à parte:** o Flask não perde fluidez, o GIL não é partilhado e o worker pode ser morto
  quando todas as funções se desligam (liberta ~300 MB). Os modelos só são carregados quando a função
  é ligada.
- **Protocolo:** uma linha JSON por comando; se tiver `"len"`, seguem-se esses bytes (um JPEG). As
  respostas são linhas JSON (`ready`, `result`, `reply`, `error`). O worker duplica o stdout para o
  protocolo e manda o fd 1 para o stderr, para nenhuma biblioteca estragar as respostas.

  | comando | resposta |
  |---|---|
  | `{"cmd":"cfg","features":{...},"conf":0.45,"model":"yolo11n","size":320}` | nenhuma |
  | `{"cmd":"gallery","people":[{"id","name","embs":[base64 float32]}]}` | nenhuma |
  | `{"cmd":"frame","n":123,"len":L}` + JPEG | `{"ev":"result","n":123,"objects":[...],"faces":[...],"poses":[...],"codes":[...],"ms":{...}}` |
  | `{"cmd":"embed","id":7,"len":L}` + JPEG | `{"ev":"reply","id":7,"faces":[{"b","px","emb","crop"}]}` |

  Coordenadas relativas (0..1) à imagem recebida; a interface desenha-as num `<canvas>` por cima do
  `<img>`, tendo em conta o `object-fit: contain`.
- **Um quadro de cada vez:** o feeder manda o quadro mais recente, espera pelo resultado e só então
  pega no seguinte. Se o worker não responder em 15 s ou morrer, é reiniciado (espera crescente até 30 s).
- **Regulador (`AnalyzerService.budget()`):** depois de cada análise que demorou `t`, espera
  `t·(1−duty)/duty` e respeita `max_fps`. Na corrente: 2 threads, `duty` 0,5; na bateria: 1 thread, 2/s,
  `duty` 0,25; CPU ≥ 72 °C: metade. Mudar de corrente para bateria reinicia o worker com outras threads.
  Em pausa (bateria fraca, falha de energia, CPU ≥ 80 °C) o worker é morto e o vídeo deixa de ser pedido.
- **Poupança no worker:** nice 10; HD descodificado com `IMREAD_REDUCED_COLOR_2`; movimento medido numa
  miniatura 64×48 (descodificada a 1/8) contra a do último quadro analisado: sem mudança, devolve o resultado
  anterior com `"static": true` (no máximo 3 s); pose e caras só com `pessoa ≥ 0,25` no detetor (e 1 quadro
  em 4); pose em quadros alternados; caras seguidas por sobreposição (IoU ≥ 0,3) com humor a cada 2 análises,
  idade a cada 10 (média das últimas 5) e nome a cada 15 (a cada 2 enquanto desconhecida).
- **Energia (`host/power.py`):** lê `/sys/class/power_supply` (AXP228), `thermal_zone0` e
  `vcgencmd get_throttled` a cada 5 s. Uma leitura com erro (ENXIO) quer dizer que o chip não responde no
  I2C. Níveis `ok`/`low`/`critical`/`fault`, evento SSE `power`, notificação por D-Bus
  (`org.freedesktop.Notifications`) e, em `critical`, fim da gravação em curso.
- **Caras:** YuNet sobre a imagem reduzida a 640 px de largura; as coordenadas voltam à imagem inteira
  para o alinhamento do SFace, o recorte do FER+ (cinzento 64×64) e o do genderage (96×96). Caras com
  menos de 36 px não são classificadas. Um nome é dado com semelhança de cosseno ≥ 0,40 com qualquer
  amostra da pessoa (máx. 12 amostras por pessoa).
- **API:** `GET/POST /api/ai` (estado e opções), `POST /api/ai/enroll` (`{"name"}` ou `{"pid"}`: regista a
  maior cara de um quadro novo), `PATCH/DELETE /api/ai/people/<id>`, `DELETE /api/ai/people/<id>/<amostra>`,
  `GET/DELETE /api/ai/events`, imagens em `/ai/face/<f>` e `/ai/event/<f>`. Eventos SSE: `ai` (cada
  resultado), `ai_state`, `ai_event`.
