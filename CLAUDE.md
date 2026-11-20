# CamDeck: notas para o Claude Code

Câmara XIAO ESP32-S3 Sense (sensor **OV3660**, máx. QXGA 2048×1536, PSRAM 8 MB, microfone PDM) ligada ao
uConsole CM5 só por USB. Firmware ESP-IDF em `firmware/`, servidor `host/camd.py` (Flask, porta **8090**,
palavra-passe), interface em `host/web/`. Documentação em pt-PT ("ecrã", "ficheiro", "câmara").
Protocolo e decisões técnicas: `docs/arquitetura.md`.

## Como trabalhar
- **Python:** `/usr/bin/python3` (o `python3` do PATH é um venv sem Flask/PIL/pyserial).
- **Serviço:** `systemctl --user restart camd`; logs com `journalctl --user -u camd -f` (os logs do ESP aparecem como `esp: ...`).
- **Firmware:** `firmware/flash.sh` (usa `firmware/env.sh`, toolchain Xtensa; build em `/dev/shm/camdeck-build`;
  pára e volta a ligar o camd). Porta: `$CAMDECK_PORT` ou `/dev/esp-cam`. Subir `CAMDECK_FW_VERSION` em `firmware/main/main.c`.
- **Teste do protocolo sem o camd:** parar o serviço e `python3 host/camlink.py <porta>` (hello, bench, fps por resolução, foto).
- **Ver a interface:** o Chrome headless não espera pelo MJPEG; para capturas usar o protocolo DevTools com espera real.
- **Git:** sem dados pessoais nos ficheiros (o número de série da placa só vai para `/etc/udev` e `~/.config`).

## Estado (20/11/2026)
- Firmware 0.1.0 a funcionar: vídeo até QXGA, fotos, microfone, estatísticas, logs pela USB.
- camd: stream MJPEG, fotos, gravação MP4 com som, galeria, ajustes persistentes, SSE, login.
- Serviço systemd do utilizador ativo. A configuração usa a porta `/dev/serial/by-id/...` da câmara
  porque a regra udev (`setup/make-udev-rule.sh`, precisa de sudo) ainda não foi instalada.

## Cuidados conhecidos
- **Conflito com o DashDeck:** a regra `99-esp-dash.rules` dá `/dev/esp-dash` a qualquer ESP 303a:1001.
  Com a câmara ligada, o link pode apontar para ela. `setup/make-udev-rule.sh` resolve (tira o esp-dash à câmara).
- A USB Full Speed limita o débito a ~890 KB/s: HD ~10 fps, QXGA ~3 fps.
- PSRAM DMA do esp32-camera tem de ficar desligado com o OV3660 (NO-SOI acima de SVGA).

## Ideias seguintes
Timelapse, deteção de movimento, leitor de QR, box "Câmara" no DashDeck (pré-visualização no ecrã 320×172,
via `/api/frame.jpg`), escuta do som ao vivo no browser.
