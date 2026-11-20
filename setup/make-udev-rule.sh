#!/usr/bin/env bash
# Cria /dev/esp-cam para o XIAO ESP32-S3 da câmera, identificado pelo número de série USB
# (o endereço MAC; fica só em /etc, não no repositório).
#
# Todos os ESP32 em USB-Serial-JTAG têm o mesmo 303a:1001, e a regra do DashDeck dá
# /dev/esp-dash a qualquer um deles. Esta regra corre depois (99-espcam > 99-esp-dash) e
# tira o esp-dash da câmera, para o dashd nunca abrir a porta errada.
#
# Uso: make-udev-rule.sh [/dev/ttyACMx]   (sem argumento: o ESP32 que não é /dev/esp-dash)
set -e
dev="$1"
if [ -z "$dev" ]; then
    dash=$(readlink -f /dev/esp-dash 2>/dev/null || true)
    for d in /dev/serial/by-id/usb-Espressif_USB_JTAG*; do
        real=$(readlink -f "$d")
        [ "$real" = "$dash" ] && continue
        dev="$real"
    done
fi
[ -n "$dev" ] || { echo "nenhum ESP32 da câmera encontrado"; exit 1; }
serial=$(udevadm info -q property -n "$dev" | sed -n 's/^ID_SERIAL_SHORT=//p')
[ -n "$serial" ] || { echo "sem número de série em $dev"; exit 1; }
echo "câmera: $dev (serial $serial)"
sudo tee /etc/udev/rules.d/99-espcam.rules >/dev/null <<RULE
# CamDeck: XIAO ESP32-S3 Sense (gerado por setup/make-udev-rule.sh)
SUBSYSTEM=="tty", ATTRS{idVendor}=="303a", ATTRS{idProduct}=="1001", ATTRS{serial}=="$serial", SYMLINK-="esp-dash", SYMLINK+="esp-cam", MODE="0660", GROUP="dialout"
RULE
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=tty
sleep 1
ls -l /dev/esp-cam /dev/esp-dash
