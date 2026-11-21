#!/bin/bash
# install-ai.sh: instala a análise de imagem (IA) do CamDeck no uConsole.
#
#   - venv próprio em ~/.local/share/camdeck/ai-venv (onnxruntime + OpenCV), separado do Python
#     do sistema que o camd usa;
#   - modelos ONNX em ~/.local/share/camdeck/models (~110 MB);
#   - o YOLO11n (objetos e pose) é exportado para ONNX com o pacote da Ultralytics num venv
#     temporário com o PyTorch (~1,5 GB, apagado no fim).
#
# Pode correr-se outra vez: só descarrega o que faltar. Não precisa de sudo.
set -euo pipefail

BASE="$HOME/.local/share/camdeck"
VENV="$BASE/ai-venv"
MODELS="$BASE/models"
EXPORT="$HOME/.cache/camdeck/export"
PY=/usr/bin/python3

mkdir -p "$MODELS"

echo "== venv de IA ($VENV)"
if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q --upgrade onnxruntime opencv-contrib-python-headless numpy
"$VENV/bin/python" -c "import onnxruntime, cv2; print('   onnxruntime', onnxruntime.__version__, '· OpenCV', cv2.__version__)"

# nome  url  sha256
fetch() {
  local name="$1" url="$2" sum="$3" f="$MODELS/$1"
  if [ -s "$f" ] && echo "$sum  $f" | sha256sum -c --status; then
    echo "   $name: ok"; return
  fi
  echo "   $name: a descarregar"
  curl -fL --retry 3 -o "$f.part" "$url"
  echo "$sum  $f.part" | sha256sum -c --status || { echo "   $name: checksum errado"; rm -f "$f.part"; exit 1; }
  mv "$f.part" "$f"
}

echo "== modelos ($MODELS)"
ZOO=https://github.com/opencv/opencv_zoo/raw/main/models
# YuNet (caras) e SFace (reconhecimento): OpenCV Zoo, Apache-2.0
fetch yunet.onnx "$ZOO/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
  8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
fetch sface.onnx "$ZOO/face_recognition_sface/face_recognition_sface_2021dec.onnx" \
  0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79
# FER+ (humor): ONNX Model Zoo, MIT
fetch ferplus.onnx https://github.com/onnx/models/raw/main/validated/vision/body_analysis/emotion_ferplus/model/emotion-ferplus-8.onnx \
  a2a2ba6a335a3b29c21acb6272f962bd3d47f84952aaffa03b60986e04efa61c
# YOLOX-nano (objetos, alternativa leve): Megvii, Apache-2.0
fetch yolox_nano.onnx https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx \
  c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d

# Idade e género: InsightFace (buffalo_s), só para uso não comercial
GA_SUM=4fde69b1c810857b88c64a335084f1c3fe8f01246c9a191b48c7bb756d6652fb
if [ -s "$MODELS/genderage.onnx" ] && echo "$GA_SUM  $MODELS/genderage.onnx" | sha256sum -c --status; then
  echo "   genderage.onnx: ok"
else
  echo "   genderage.onnx: a descarregar o pacote buffalo_s (~130 MB, só se guarda 1,3 MB)"
  tmp=$(mktemp -d)
  curl -fL --retry 3 -o "$tmp/buffalo_s.zip" https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_s.zip
  unzip -q -o -j "$tmp/buffalo_s.zip" genderage.onnx -d "$tmp"
  echo "$GA_SUM  $tmp/genderage.onnx" | sha256sum -c --status || { echo "   genderage.onnx: checksum errado"; rm -rf "$tmp"; exit 1; }
  mv "$tmp/genderage.onnx" "$MODELS/"
  rm -rf "$tmp"
fi

# YOLO11n (objetos e pose): Ultralytics, AGPL-3.0. Só os pesos são usados (exportados para ONNX
# aqui); o CamDeck não inclui nem importa código da Ultralytics.
need=()
for f in yolo11n_320.onnx yolo11n_416.onnx yolo11n-pose_320.onnx; do
  [ -s "$MODELS/$f" ] || need+=("$f")
done
if [ ${#need[@]} -eq 0 ]; then
  echo "   yolo11n: ok"
else
  echo "== exportar o YOLO11n para ONNX (venv temporário com o PyTorch, pode demorar ~10 min)"
  EV="$HOME/.cache/camdeck/export-venv"
  [ -x "$EV/bin/python" ] || "$PY" -m venv "$EV"
  "$EV/bin/pip" install -q --upgrade pip
  "$EV/bin/pip" install -q --index-url https://download.pytorch.org/whl/cpu torch torchvision
  "$EV/bin/pip" install -q ultralytics onnx onnxslim
  mkdir -p "$EXPORT"
  (
    cd "$EXPORT"
    for spec in yolo11n:320 yolo11n:416 yolo11n-pose:320; do
      m=${spec%%:*}; s=${spec##*:}
      [ -s "$MODELS/${m}_$s.onnx" ] && continue
      echo "   $m @ $s px"
      "$EV/bin/yolo" export model="$m.pt" format=onnx imgsz="$s" opset=17 simplify=True >/dev/null
      mv "$m.onnx" "$MODELS/${m}_$s.onnx"
    done
  )
  rm -rf "$EV" "$EXPORT"
fi

du -sh "$MODELS" | awk '{print "== pronto: modelos em " $2 " (" $1 ")"}'
if systemctl --user is-active -q camd 2>/dev/null; then
  systemctl --user restart camd && echo "   camd reiniciado"
fi
