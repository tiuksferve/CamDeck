# Ativação mínima do ESP-IDF v5.5.6 para o ESP32-S3 (toolchain Xtensa), sem o export.sh completo.
unset VIRTUAL_ENV
export IDF_PATH="$HOME/esp/esp-idf"
export IDF_TOOLS_PATH="$HOME/.espressif"
export IDF_PYTHON_ENV_PATH="$HOME/.espressif/python_env/idf5.5_py3.11_env"
export ESP_ROM_ELF_DIR="$HOME/.espressif/tools/esp-rom-elfs/"
_tc=$(ls -d $HOME/.espressif/tools/xtensa-esp-elf/*/xtensa-esp-elf/bin | head -1)
export PATH="$IDF_PATH/tools:$IDF_PYTHON_ENV_PATH/bin:$_tc:$(echo "$PATH" | tr ':' '\n' | grep -vi venv | paste -sd:)"
unset _tc
