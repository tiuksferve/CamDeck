"""
config.py — configuração persistente do CamDeck em ~/.config/camdeck/config.json.

Guarda a chave de sessão do Flask, o hash da palavra-passe (nunca em claro), a porta,
as pastas dos ficheiros e os últimos ajustes da câmera, que o camd volta a mandar ao
ESP sempre que ele arranca.
"""

import json
import logging
import os
import secrets
import threading

from werkzeug.security import check_password_hash, generate_password_hash

log = logging.getLogger("camdeck.config")

CONFIG_DIR = os.path.expanduser("~/.config/camdeck")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
INITIAL_PASSWORD_PATH = os.path.join(CONFIG_DIR, "initial-password.txt")
# Token para programas do próprio uConsole (ex.: dashd do DashDeck), sem palavra-passe. Só é aceite
# em ligações locais (127.0.0.1/::1) e o ficheiro só é legível pelo utilizador.
LOCAL_TOKEN_PATH = os.path.join(CONFIG_DIR, "local-token")


def local_token() -> str:
    """Lê (ou cria) o token local."""
    try:
        with open(LOCAL_TOKEN_PATH) as f:
            tok = f.read().strip()
        if len(tok) >= 32:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    fd = os.open(LOCAL_TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    return tok

DEFAULTS = {
    "bind_host": "0.0.0.0",
    "bind_port": 8090,
    "device": "/dev/esp-cam",
    "photos_dir": "~/Pictures/CamDeck",
    "videos_dir": "~/Videos/CamDeck",
    "stream_size": "VGA",
    "snap_size": "",            # "" = o maior do sensor
    "fps_max": 0,
    "mic": True,                # gravar som com o vídeo
    "mic_gain": 4,
    "settings": {},             # ajustes do sensor (brightness, vflip...)
    "idle_stop_s": 10,          # pára o stream sem ninguém a ver nem a gravar
    "video_crf": 23,
}


class Config:
    def __init__(self, path: str = CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        self.generated_password = None
        self._load()
        self._ensure_secrets()

    def _load(self):
        try:
            with open(self.path) as f:
                self._data.update(json.load(f))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.error("configuração ilegível (%s); a usar valores por omissão", e)

    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)

    def _ensure_secrets(self):
        changed = False
        if not self._data.get("secret_key"):
            self._data["secret_key"] = secrets.token_hex(32)
            changed = True
        if not self._data.get("password_hash"):
            pw = secrets.token_urlsafe(9)
            self._data["password_hash"] = generate_password_hash(pw)
            self.generated_password = pw
            changed = True
            # O camd corre como serviço: a palavra-passe inicial fica também num ficheiro só do utilizador
            os.makedirs(CONFIG_DIR, exist_ok=True)
            fd = os.open(INITIAL_PASSWORD_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(pw + "\n")
        if changed:
            self.save()

    # ── acesso genérico ────────────────────────────────────────────────────
    def get(self, key, default=None):
        return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value):
        self._data[key] = value
        self.save()

    def update(self, values: dict):
        self._data.update(values)
        self.save()

    def path_of(self, key) -> str:
        p = os.path.expanduser(self.get(key))
        os.makedirs(p, exist_ok=True)
        return p

    # ── palavra-passe ──────────────────────────────────────────────────────
    def check_password(self, pw: str) -> bool:
        h = self._data.get("password_hash")
        return bool(h and pw) and check_password_hash(h, pw)

    def set_password(self, pw: str):
        if not pw or len(pw) < 8:
            raise ValueError("a palavra-passe tem de ter pelo menos 8 caracteres")
        self.set("password_hash", generate_password_hash(pw))
        try:
            os.remove(INITIAL_PASSWORD_PATH)
        except FileNotFoundError:
            pass
