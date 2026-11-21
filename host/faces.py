"""
faces.py: pessoas conhecidas (reconhecimento facial) do CamDeck.

Cada pessoa tem um nome e uma ou mais amostras: a cara alinhada (JPEG 112×112) e o vetor
do SFace (128 floats, em base64). Fica tudo em ~/.local/share/camdeck/faces, só legível
pelo utilizador, e nunca sai do uConsole.
"""

import json
import os
import threading
import time

FACES_DIR = os.path.expanduser("~/.local/share/camdeck/faces")
MAX_SAMPLES = 12


class FaceDB:
    def __init__(self, path: str = FACES_DIR):
        self.dir = path
        self.file = os.path.join(path, "people.json")
        self._lock = threading.Lock()
        self._data = {"next_id": 1, "people": []}
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        os.chmod(self.dir, 0o700)
        try:
            with open(self.file) as f:
                self._data = json.load(f)
        except FileNotFoundError:
            pass
        self.version = 0                # muda a cada alteração (o analisador recarrega a galeria)

    def _save(self):
        tmp = self.file + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._data, f)
        os.replace(tmp, self.file)
        self.version += 1

    def _find(self, pid):
        for p in self._data["people"]:
            if p["id"] == pid:
                return p
        return None

    # ── leitura ────────────────────────────────────────────────────────────
    def list(self) -> list:
        with self._lock:
            return [{"id": p["id"], "name": p["name"], "created": p.get("created"),
                     "samples": [s["file"] for s in p["samples"]]} for p in self._data["people"]]

    def gallery(self) -> list:
        """Para o worker: [{"id", "name", "embs": [base64...]}]."""
        with self._lock:
            return [{"id": p["id"], "name": p["name"], "embs": [s["emb"] for s in p["samples"]]}
                    for p in self._data["people"]]

    def sample_path(self, name: str) -> str:
        if "/" in name or not name.endswith(".jpg") or name.startswith("."):
            raise ValueError("nome inválido")
        return os.path.join(self.dir, name)

    # ── alterações ─────────────────────────────────────────────────────────
    def add_sample(self, name: str, emb_b64: str, crop_jpeg: bytes, pid: int = None) -> dict:
        """Junta uma amostra à pessoa pid, ou à pessoa com este nome, ou cria uma pessoa nova."""
        name = (name or "").strip()[:40]
        with self._lock:
            p = self._find(pid) if pid else None
            if p is None and name:
                p = next((x for x in self._data["people"] if x["name"].lower() == name.lower()), None)
            if p is None:
                if not name:
                    raise ValueError("falta o nome")
                p = {"id": self._data["next_id"], "name": name, "created": time.time(), "samples": []}
                self._data["next_id"] += 1
                self._data["people"].append(p)
            if len(p["samples"]) >= MAX_SAMPLES:
                old = p["samples"].pop(0)
                try:
                    os.unlink(os.path.join(self.dir, old["file"]))
                except OSError:
                    pass
            fname = f"p{p['id']}_{int(time.time() * 1000)}.jpg"
            fd = os.open(os.path.join(self.dir, fname), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(crop_jpeg)
            p["samples"].append({"file": fname, "emb": emb_b64, "t": time.time()})
            self._save()
            return {"id": p["id"], "name": p["name"], "samples": len(p["samples"])}

    def rename(self, pid: int, name: str):
        name = (name or "").strip()[:40]
        if not name:
            raise ValueError("falta o nome")
        with self._lock:
            p = self._find(pid)
            if not p:
                raise KeyError(pid)
            p["name"] = name
            self._save()

    def delete_sample(self, pid: int, fname: str):
        with self._lock:
            p = self._find(pid)
            if not p:
                raise KeyError(pid)
            p["samples"] = [s for s in p["samples"] if s["file"] != fname]
            try:
                os.unlink(self.sample_path(fname))
            except OSError:
                pass
            if not p["samples"]:
                self._data["people"].remove(p)
            self._save()

    def delete(self, pid: int):
        with self._lock:
            p = self._find(pid)
            if not p:
                raise KeyError(pid)
            for s in p["samples"]:
                try:
                    os.unlink(os.path.join(self.dir, s["file"]))
                except OSError:
                    pass
            self._data["people"].remove(p)
            self._save()
