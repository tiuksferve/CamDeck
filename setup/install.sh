#!/usr/bin/env bash
# Instala o lado do uConsole do CamDeck: dependências, regra udev (/dev/esp-cam),
# serviço systemd do utilizador e atalho no menu. Pode correr-se várias vezes.
# Não grava o firmware (ver firmware/flash.sh).
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "== dependências (/usr/bin/python3 e ffmpeg)"
missing=()
/usr/bin/python3 -c "import serial" 2>/dev/null || missing+=(python3-serial)
/usr/bin/python3 -c "import flask" 2>/dev/null || missing+=(python3-flask)
/usr/bin/python3 -c "import PIL" 2>/dev/null || missing+=(python3-pil)
command -v ffmpeg >/dev/null || missing+=(ffmpeg)
if [ ${#missing[@]} -gt 0 ]; then
    sudo apt-get install -y "${missing[@]}"
fi

echo "== regra udev -> /dev/esp-cam"
"$ROOT/setup/make-udev-rule.sh" "$@"
groups | grep -qw dialout || { sudo usermod -aG dialout "$USER"; echo "   (entre de novo na sessão para o grupo dialout valer)"; }

echo "== serviço systemd --user"
mkdir -p ~/.config/systemd/user
ln -sf "$ROOT/setup/camd.service" ~/.config/systemd/user/camd.service
systemctl --user daemon-reload
systemctl --user enable --now camd
systemctl --user --no-pager status camd | head -5

echo "== atalho no menu"
mkdir -p ~/.local/share/applications
cp "$ROOT/setup/camdeck.desktop" ~/.local/share/applications/
echo
echo "Pronto: http://$(hostname -I | awk '{print $1}'):8090/"
[ -f ~/.config/camdeck/initial-password.txt ] && echo "Palavra-passe inicial: ~/.config/camdeck/initial-password.txt"
exit 0
