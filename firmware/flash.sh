#!/usr/bin/env bash
# Compila e grava o firmware do CamDeck. Pára o camd enquanto grava (ele ocupa a porta).
# Porta: $CAMDECK_PORT, ou /dev/esp-cam (setup/make-udev-rule.sh).
# Argumentos extra vão para o idf.py (ex.: ./flash.sh build, ./flash.sh monitor).
set -e
cd "$(dirname "$0")"
. ./env.sh
BUILD=/dev/shm/camdeck-build          # build na RAM: poupa o cartão (é refeito após reboot)
PORT="${CAMDECK_PORT:-/dev/esp-cam}"
systemctl --user stop camd 2>/dev/null || true
trap 'systemctl --user start camd 2>/dev/null || true' EXIT
if [ $# -gt 0 ]; then
    idf.py -B "$BUILD" -p "$PORT" "$@"
    exit
fi
idf.py -B "$BUILD" -p "$PORT" flash
