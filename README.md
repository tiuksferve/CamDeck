# CamDeck

Câmara **Seeed XIAO ESP32-S3 Sense** ligada ao **ClockworkPi uConsole (CM5)** só por USB, usada
a partir do browser: vídeo ao vivo, fotos na resolução máxima do sensor, gravação de vídeo com som e
análise de imagem com IA (objetos, caras, humor, idade, reconhecimento de pessoas, pose, códigos QR).
Faz parte da família MeshDeckWeb / DashDeck e usa a mesma aparência.

- **Sem rádio:** o firmware nunca liga o Wi-Fi nem o Bluetooth (dispensa a antena).
- **Tudo pela USB nativa** (USB-Serial-JTAG): JPEG da câmara, som do microfone PDM e comandos.
- **Servidor no uConsole** (`camd`, porta **8090**), com palavra-passe. Abre-se em qualquer aparelho
  da rede: `http://<IP do uConsole>:8090/`.

## Ecrãs

| Ecrã | O que tem |
|---|---|
| **Ao vivo** | vídeo (MJPEG), Foto, Gravar/Parar, resolução do vídeo e das fotos, som, ecrã inteiro. Teclas: espaço (foto), R (gravar), F (ecrã inteiro). |
| **Análise** | funções de IA (liga/desliga cada uma), vídeo com as deteções desenhadas, pessoas conhecidas (registar, juntar amostras, mudar o nome, esquecer), eventos com miniatura, opções e tempos de cada modelo. No Ao vivo há botões rápidos para cada função e um resumo do que se vê. |
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

## Análise de imagem (IA)

Corre no processador do CM5 (não há acelerador), num processo à parte (`host/ai/worker.py`), com o
onnxruntime e o OpenCV num venv próprio. Por omissão está tudo desligado; enquanto houver uma função
ligada, o vídeo da câmara fica sempre ativo (como numa gravação) e a análise volta a ligar-se sozinha
quando o camd arranca.

| Função | Modelo | Licença | Tempo por quadro* |
|---|---|---|---|
| Objetos e pessoas (80 classes COCO) | YOLO11n, 320 ou 416 px | AGPL-3.0 (Ultralytics) | 85–200 ms |
| (alternativa leve) | YOLOX-nano, 416 px | Apache-2.0 | ~65 ms |
| Pose (17 pontos) e gestos | YOLO11n-pose, 320 px | AGPL-3.0 | 70–120 ms |
| Caras | YuNet | Apache-2.0 | 30–45 ms |
| Humor (8 emoções) | FER+ | MIT | ~5 ms por cara |
| Idade e género | InsightFace genderage | não comercial | ~6 ms por cara |
| Reconhecer pessoas | SFace | Apache-2.0 | ~25 ms por cara |
| Códigos QR e de barras | OpenCV | Apache-2.0 | ~80 ms, 1 quadro em 3 |

\* tempo de cada modelo medido no CM5 com imagem HD e o PyCharm aberto.

**Carga controlada.** Uma vez, com o uConsole nas baterias e o CPU no máximo durante vários minutos,
o chip de energia (AXP228) deixou de responder: ecrã apagado, USB parada. Por isso a análise:

- corre com prioridade baixa e poucas threads, e descodifica as imagens HD a metade do tamanho;
- sem movimento na imagem, repete o resultado anterior sem correr os modelos (pelo menos 1 análise a cada 3 s);
- com o detetor de objetos ligado, só procura pose e caras quando há indício de pessoa;
- guarda o humor, a idade e o nome de cada cara seguida, e só os recalcula de vez em quando;
- é regulada pela energia: **na corrente** até metade do tempo de 2 núcleos; **na bateria** até 2
  análises/s com um quarto do tempo de 1 núcleo; com o CPU acima de 72 °C, metade disso;
- **sem corrente, só corre com a bateria acima de 70 %**: ao chegar aos 70 % pára e avisa no ecrã do uConsole
  e na página; volta sozinha na corrente ou quando a carga subir a 75 % (o mínimo muda-se com
  `"ai_battery_min"` em `~/.config/camdeck/config.json`);
- **pára sozinha** com problema de alimentação (o chip de energia sem
  resposta ou subtensão) ou CPU acima de 80 °C, e volta quando tudo normalizar.

Resultado com objetos, pose, caras, humor e idade ligados: uma análise completa demora ~100 ms (antes ~420 ms)
e, numa cena parada, o worker usa ~15 % de um núcleo (antes ~150 %).

**Aviso de energia.** O cabeçalho mostra a bateria. Com bateria fraca, aparece uma faixa a pedir para ligar o
uConsole à corrente, e também uma notificação no ambiente de trabalho do uConsole (no máximo uma a cada
15 min por nível; os níveis têm histerese de 3 %). A tensão sob carga cai muito, por isso só serve de travão final. Com a bateria quase vazia
(≤ 10 % ou tensão média de 30 s ≤ 3,30 V), uma gravação em curso é terminada para o MP4 ficar guardado. O painel "Energia do
uConsole" (aba Dispositivo) mostra a fonte, a carga, a tensão, a corrente, a temperatura e o estado do chip.

**Licenças.** Escolhi o YOLO11n porque é bem mais preciso do que as alternativas Apache com a mesma
velocidade (mAP COCO 39,5 contra 25,8 do YOLOX-nano). Os pesos são exportados para ONNX na instalação e
nunca entram no repositório, e o CamDeck não usa código da Ultralytics, por isso o código continua MIT.
Para uso pessoal no uConsole nem a AGPL nem a licença não comercial do modelo de idade/género trazem
obrigações; para outro uso, o YOLOX-nano está disponível nas Opções.

**Privacidade.** As caras registadas (imagem alinhada 112×112 e vetor do SFace) ficam em
`~/.local/share/camdeck/faces` e os eventos em `~/.local/share/camdeck/events`, só legíveis pelo
utilizador. Nada sai do uConsole.

**Eventos:** entradas e saídas de pessoas, animais e veículos (com histerese: 2 deteções seguidas para
entrar, 4 s sem ver para sair), pessoas reconhecidas, gestos (mão levantada, mãos no ar, deitado) e
códigos lidos (o mesmo código só volta a contar passados 60 s). Guardam-se os últimos 300.

## Instalação

```bash
setup/install.sh            # dependências, regra udev (/dev/esp-cam), serviço systemd, atalho no menu
setup/install-ai.sh         # análise de imagem: venv com onnxruntime + OpenCV e modelos (~110 MB)
firmware/flash.sh           # compila e grava o firmware (pára o camd enquanto grava)
```

- A palavra-passe inicial fica em `~/.config/camdeck/initial-password.txt` (apagado quando a muda).
  Também se muda com `host/camd.py --set-password`.
- Programas do próprio uConsole (o DashDeck, por exemplo) entram com o token de
  `~/.config/camdeck/local-token`, sem palavra-passe (ver `docs/arquitetura.md`).
- Configuração: `~/.config/camdeck/config.json` (porta, pastas, ajustes guardados).
- Fotos em `~/Pictures/CamDeck`, vídeos (MP4 H.264 + AAC) em `~/Videos/CamDeck`.
- Logs: `journalctl --user -u camd -f`; reiniciar: `systemctl --user restart camd`.

### Dois ESP32 no mesmo uConsole

Todos os ESP32 em USB-Serial-JTAG têm o mesmo `303a:1001`, por isso cada placa é identificada pelo
número de série USB. `setup/make-udev-rule.sh` cria `/dev/esp-cam` pelo número de série da câmara. A
regra do DashDeck (`99-esp-dash.rules`) deve filtrar também pelo número de série da placa dele, para o
`dashd` nunca abrir a câmara por engano (o script avisa se não filtrar).

## Estrutura

```
firmware/        ESP-IDF 5.5 (ESP32-S3): main.c (comandos), cam.c (câmara), mic.c (microfone), link.c (protocolo)
host/camd.py     servidor Flask: páginas, API, stream MJPEG, eventos (SSE)
host/camlink.py  protocolo USB (também serve de teste: python3 camlink.py /dev/esp-cam)
host/camera.py   liga/desliga o vídeo conforme a procura e repõe os ajustes
host/recorder.py gravação (JPEG + PCM em bruto, depois MP4 com o ffmpeg)
host/media.py    galeria, miniaturas, metadados
host/analyzer.py análise de imagem: gere o worker, manda-lhe quadros, eventos
host/ai/worker.py  modelos de IA (corre no venv de IA; stdin/stdout)
host/faces.py    pessoas conhecidas (reconhecimento facial)
host/power.py    bateria, chip de energia, temperatura: avisos e travão da análise
host/web/        interface (HTML, CSS, JS sem dependências)
setup/           serviço systemd, regra udev, atalho, install.sh, install-ai.sh
docs/            protocolo e notas técnicas
```

Licença: MIT.
