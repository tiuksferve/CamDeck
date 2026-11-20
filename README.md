# CamDeck

Câmara **Seeed XIAO ESP32-S3 Sense** ligada ao **ClockworkPi uConsole (CM5)** só por USB, usada
a partir do browser: vídeo ao vivo, fotos na resolução máxima do sensor e gravação de vídeo com som.
Faz parte da família MeshDeckWeb / DashDeck e usa a mesma aparência.

- **Sem rádio:** o firmware nunca liga o Wi-Fi nem o Bluetooth (dispensa a antena).
- **Tudo pela USB nativa** (USB-Serial-JTAG): JPEG da câmara, som do microfone PDM e comandos.
- **Servidor no uConsole** (`camd`, porta **8090**), com palavra-passe. Abre-se em qualquer aparelho
  da rede: `http://<IP do uConsole>:8090/`.

## Ecrãs

| Ecrã | O que tem |
|---|---|
| **Ao vivo** | vídeo (MJPEG), Foto, Gravar/Parar, resolução do vídeo e das fotos, som, ecrã inteiro. Teclas: espaço (foto), R (gravar), F (ecrã inteiro). |
| **Galeria** | fotos e vídeos com miniaturas; visor com anterior/seguinte, transferir e apagar. |
| **Ajustes** | imagem, exposição, cor, resolução, limite de fps, compressão JPEG e correções do sensor, com pré-visualização. Os ajustes ficam guardados no uConsole e são repostos sempre que a câmara arranca. |
| **Dispositivo** | firmware, sensor, temperatura, memória, débito da USB, perdas, nível do microfone, disco, palavra-passe, reiniciar a câmara. |

## Desempenho medido (OV3660, firmware 0.1.0)

A USB do ESP32-S3 é Full Speed: o débito útil fica perto de **890 KB/s**, e é isso que limita os
quadros por segundo nas resoluções altas.

| Resolução | fps | KB por quadro |
|---|---|---|
| VGA 640×480 | 28 | 13 |
| SVGA 800×600 | 28 | 20 |
| XGA 1024×768 | 23 | 32 |
| HD 1280×720 | 10 | 75 |
| UXGA 1600×1200 | 4,3 | 183 |
| QXGA 2048×1536 | 3 | 255 |

Uma foto QXGA demora ~0,8 s (o vídeo pausa durante esse tempo). Durante uma gravação, a foto é o
quadro atual do vídeo, para não interromper a gravação.

## Instalação

```bash
setup/install.sh            # dependências, regra udev (/dev/esp-cam), serviço systemd, atalho no menu
firmware/flash.sh           # compila e grava o firmware (pára o camd enquanto grava)
```

- A palavra-passe inicial fica em `~/.config/camdeck/initial-password.txt` (apagado quando a muda).
  Também se muda com `host/camd.py --set-password`.
- Configuração: `~/.config/camdeck/config.json` (porta, pastas, ajustes guardados).
- Fotos em `~/Pictures/CamDeck`, vídeos (MP4 H.264 + AAC) em `~/Videos/CamDeck`.
- Logs: `journalctl --user -u camd -f`; reiniciar: `systemctl --user restart camd`.

### Dois ESP32 no mesmo uConsole

Todos os ESP32 em USB-Serial-JTAG têm o mesmo `303a:1001`, e a regra do DashDeck dá `/dev/esp-dash`
a qualquer um. `setup/make-udev-rule.sh` cria `/dev/esp-cam` pelo número de série da câmara e tira-lhe
o `esp-dash`, para o `dashd` nunca abrir a câmara por engano.

## Estrutura

```
firmware/        ESP-IDF 5.5 (ESP32-S3): main.c (comandos), cam.c (câmara), mic.c (microfone), link.c (protocolo)
host/camd.py     servidor Flask: páginas, API, stream MJPEG, eventos (SSE)
host/camlink.py  protocolo USB (também serve de teste: python3 camlink.py /dev/esp-cam)
host/camera.py   liga/desliga o vídeo conforme a procura e repõe os ajustes
host/recorder.py gravação (JPEG + PCM em bruto, depois MP4 com o ffmpeg)
host/media.py    galeria, miniaturas, metadados
host/web/        interface (HTML, CSS, JS sem dependências)
setup/           serviço systemd, regra udev, atalho, install.sh
docs/            protocolo e notas técnicas
```

Licença: MIT.
