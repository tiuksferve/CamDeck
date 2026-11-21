"""
analyzer.py: análise de imagem (IA) do CamDeck, do lado do camd.

O trabalho pesado corre noutro processo (host/ai/worker.py, no venv de IA com onnxruntime e
OpenCV), para o Flask continuar fluido e a análise poder usar os outros núcleos. Este módulo:

- arranca o worker quando alguma função é ligada e mata-o quando se desligam todas
  (liberta ~300 MB de memória);
- mantém o vídeo ligado enquanto analisa (como uma gravação) e manda-lhe sempre o quadro
  mais recente; quadros que chegam enquanto o worker está ocupado são saltados;
- envia os resultados aos browsers (SSE "ai") e regista eventos: entradas e saídas de
  pessoas/animais/veículos, pessoas reconhecidas, gestos e códigos lidos;
- regula a carga pela energia e pela temperatura (ver budget()): na corrente usa até metade
  do tempo de 2 núcleos, na bateria um quarto do tempo de 1 núcleo, e pára com bateria fraca,
  problema de alimentação ou CPU quente.
"""

import io
import json
import logging
import os
import subprocess
import threading
import time
from collections import deque

log = logging.getLogger("camdeck.ai")

HOST_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(HOST_DIR, "ai", "worker.py")
AI_HOME = os.path.expanduser("~/.local/share/camdeck")
AI_PYTHON = os.path.join(AI_HOME, "ai-venv", "bin", "python")
MODELS_DIR = os.path.join(AI_HOME, "models")
EVENTS_DIR = os.path.join(AI_HOME, "events")

FEATURES = ("objects", "pose", "faces", "emotion", "age", "names", "codes")
# Ficheiros de que cada função precisa
NEEDS = {
    "objects": ["yolo11n_416.onnx"],
    "pose": ["yolo11n-pose_320.onnx"],
    "faces": ["yunet.onnx"],
    "emotion": ["yunet.onnx", "ferplus.onnx"],
    "age": ["yunet.onnx", "genderage.onnx"],
    "names": ["yunet.onnx", "sface.onnx"],
    "codes": [],
}
MODEL_FILES = {"yolo11n": {320: "yolo11n_320.onnx", 416: "yolo11n_416.onnx"},
               "yolox_nano": {416: "yolox_nano.onnx"}}

DEFAULTS = {
    "features": {k: False for k in FEATURES},
    "conf": 0.45,               # confiança mínima dos objetos
    "model": "yolo11n",         # yolo11n (preciso) | yolox_nano (leve, Apache-2.0)
    "size": 320,                # entrada do detetor de objetos: 320 (rápido) | 416 (objetos pequenos)
    "max_fps": 3,               # análises por segundo pedidas (o regulador pode dar menos)
    "events": True,
    "event_classes": ["pessoa", "cão", "gato", "pássaro", "carro", "mota", "bicicleta", "autocarro", "camião"],
}

ENTER_HITS = 2                  # resultados seguidos para contar uma entrada
LEAVE_S = 4.0                   # segundos sem ver para contar uma saída
MAX_EVENTS = 300

# Regulador de carga: (threads do onnxruntime, máx. análises/s, fração máxima do tempo a analisar)
BUDGET_MAINS = (2, None, 0.5)
BUDGET_BATTERY = (1, 2.0, 0.25)


class EventLog:
    """Registo dos eventos (JSON + miniaturas em ~/.local/share/camdeck/events)."""

    def __init__(self, emit):
        self.emit = emit
        self.file = os.path.join(EVENTS_DIR, "events.json")
        self._lock = threading.Lock()
        os.makedirs(EVENTS_DIR, mode=0o700, exist_ok=True)
        try:
            with open(self.file) as f:
                self.items = deque(json.load(f), maxlen=MAX_EVENTS)
        except (OSError, ValueError):
            self.items = deque(maxlen=MAX_EVENTS)
        self._next = max([e["id"] for e in self.items], default=0) + 1
        self._dirty = False
        threading.Thread(target=self._saver, name="ai-events", daemon=True).start()

    def add(self, kind, label, detail=None, jpeg=None):
        with self._lock:
            ev = {"id": self._next, "t": round(time.time(), 1), "kind": kind, "label": label}
            self._next += 1
            if detail:
                ev["detail"] = detail
            if jpeg:
                ev["thumb"] = self._thumb(ev["id"], jpeg)
            if len(self.items) == self.items.maxlen:
                self._drop(self.items[0])
            self.items.append(ev)
            self._dirty = True
        self.emit("ai_event", ev)
        return ev

    def _thumb(self, eid, jpeg):
        name = f"ev{eid}.jpg"
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(jpeg))
            im.thumbnail((480, 480))
            im.convert("RGB").save(os.path.join(EVENTS_DIR, name), "JPEG", quality=80)
        except Exception as e:      # noqa: BLE001
            log.warning("miniatura do evento: %s", e)
            return None
        return name

    def _drop(self, ev):
        if ev.get("thumb"):
            try:
                os.unlink(os.path.join(EVENTS_DIR, ev["thumb"]))
            except OSError:
                pass

    def list(self, limit=200):
        with self._lock:
            return list(self.items)[-limit:][::-1]

    def clear(self):
        with self._lock:
            for ev in self.items:
                self._drop(ev)
            self.items.clear()
            self._dirty = True

    def thumb_path(self, name):
        if "/" in name or not name.startswith("ev") or not name.endswith(".jpg"):
            raise ValueError("nome inválido")
        return os.path.join(EVENTS_DIR, name)

    def _saver(self):
        while True:
            time.sleep(2)
            if not self._dirty:
                continue
            with self._lock:
                data = list(self.items)
                self._dirty = False
            tmp = self.file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.file)


class Presence:
    """Transforma deteções quadro a quadro em entradas e saídas (com histerese)."""

    def __init__(self):
        self._seen = {}             # chave -> {"hits", "last", "on", "count"}

    def update(self, keys: dict, now: float):
        """keys: {chave: quantidade}. Devolve ([(chave, n) que entraram], [chaves que saíram])."""
        entered, left = [], []
        for k, n in keys.items():
            s = self._seen.setdefault(k, {"hits": 0, "last": 0, "on": False, "count": 0})
            s["hits"] += 1
            s["last"] = now
            s["count"] = n
            if not s["on"] and s["hits"] >= ENTER_HITS:
                s["on"] = True
                entered.append((k, n))
        for k, s in list(self._seen.items()):
            if k in keys:
                continue
            s["hits"] = 0
            if s["on"] and now - s["last"] > LEAVE_S:
                s["on"] = False
                left.append(k)
            if not s["on"] and now - s["last"] > LEAVE_S:
                del self._seen[k]
        return entered, left

    def reset(self):
        self._seen.clear()


class AnalyzerService:
    def __init__(self, cfg, cam, faces, emit, power=None):
        self.cfg = cfg
        self.power = power
        self.cam = cam
        self.faces = faces
        self.emit = emit
        self.events = EventLog(emit)
        self.presence = Presence()
        self._codes_seen = {}       # texto -> última vez lido
        self._proc = None
        self._wlock = threading.Lock()          # escrita no stdin do worker
        self._lock = threading.RLock()
        self._result_cv = threading.Condition()
        self._result = None
        self._replies = {}
        self._reply_cv = threading.Condition()
        self._req_id = 0
        self._gallery_version = -1
        self._feeder = None
        self._stop_feeder = threading.Event()
        self.last = None            # último resultado
        self.fps = 0.0
        self.worker_info = {}
        self.error = None
        self._done = 0
        self._fps_t = time.time()
        self._threads = None            # threads com que o worker atual arrancou
        self._paused_reason = None
        self._holding = False
        if power:
            power.add_listener(self._on_power)

    # ── configuração ───────────────────────────────────────────────────────
    def settings(self) -> dict:
        s = dict(DEFAULTS)
        s.update(self.cfg.get("ai") or {})
        s["features"] = {**DEFAULTS["features"], **(s.get("features") or {})}
        return s

    def missing(self) -> list:
        """Ficheiros em falta (venv ou modelos)."""
        miss = [] if os.path.exists(AI_PYTHON) else ["ai-venv"]
        files = {f for v in NEEDS.values() for f in v} | {f for m in MODEL_FILES.values() for f in m.values()}
        miss += sorted(f for f in files if not os.path.exists(os.path.join(MODELS_DIR, f)))
        return miss

    def update(self, changes: dict):
        s = self.settings()
        if "features" in changes:
            for k, v in (changes["features"] or {}).items():
                if k in FEATURES:
                    s["features"][k] = bool(v)
        if "conf" in changes:
            s["conf"] = min(0.9, max(0.1, float(changes["conf"])))
        if "model" in changes and changes["model"] in MODEL_FILES:
            s["model"] = changes["model"]
        if "size" in changes and int(changes["size"]) in (320, 416):
            s["size"] = int(changes["size"])
        if s["model"] == "yolox_nano":
            s["size"] = 416
        if "max_fps" in changes:
            s["max_fps"] = max(0, min(30, int(changes["max_fps"])))
        if "events" in changes:
            s["events"] = bool(changes["events"])
        if "event_classes" in changes and isinstance(changes["event_classes"], list):
            s["event_classes"] = [str(c) for c in changes["event_classes"]][:80]
        self.cfg.set("ai", s)
        self.apply()
        return s

    def budget(self) -> dict:
        """Quanto a análise pode gastar agora, conforme a energia e a temperatura."""
        p = self.power.state if self.power else {}
        user = self.settings()["max_fps"] or None
        threads, cap, duty = BUDGET_BATTERY if p.get("on_battery") else BUDGET_MAINS
        mode = "bateria" if p.get("on_battery") else "corrente"
        fps = min(x for x in (user, cap) if x) if (user or cap) else None
        if p.get("warm"):
            mode += ", CPU quente"
            fps = min(fps or 2.0, 2.0) / 2
            duty /= 2
        paused = None
        if p.get("level") in ("low", "critical", "fault"):
            paused = p.get("reason")
        elif p.get("hot"):
            paused = f"CPU quente ({p.get('temp')} °C)"
        return {"mode": mode, "threads": threads, "max_fps": fps, "duty": duty, "paused": paused}

    def _on_power(self, st, prev):
        keys = ("level", "on_battery", "warm", "hot")
        if any(st.get(k) != prev.get(k) for k in keys):
            self._emit_state()

    @property
    def active(self) -> bool:
        return any(self.settings()["features"].values())

    def apply(self):
        """Arranca/pára o worker e o feeder conforme as funções ligadas."""
        with self._lock:
            if self.active:
                if self.missing():
                    self.error = "faltam modelos: corra setup/install-ai.sh"
                    self._emit_state()
                    return
                if not self.budget()["paused"]:
                    self._ensure_worker()
                    self._send_cfg()
                if not self._feeder or not self._feeder.is_alive():
                    self._stop_feeder.clear()
                    self._feeder = threading.Thread(target=self._feed, name="ai-feed", daemon=True)
                    self._feeder.start()
            else:
                self._stop_feeder.set()
                self._hold(False)
                self.presence.reset()
                self.last = None
                self._kill_worker()
                self.emit("ai", {"n": None})
        self._emit_state()

    def _hold(self, on: bool):
        if on and not self._holding:
            self.cam.hold("ai")
        elif not on and self._holding:
            self.cam.release("ai")
        self._holding = on

    def start(self):
        if self.active:
            threading.Thread(target=self.apply, daemon=True).start()

    def stop(self):
        self._stop_feeder.set()
        self._kill_worker()

    # ── worker ─────────────────────────────────────────────────────────────
    def _ensure_worker(self):
        if self._proc and self._proc.poll() is None:
            return
        self._threads = int(self.cfg.get("ai_threads") or self.budget()["threads"])
        env = dict(os.environ, CAMDECK_MODELS=MODELS_DIR, CAMDECK_AI_THREADS=str(self._threads))
        log.info("a arrancar o worker de IA (%d thread%s)", self._threads, "s" if self._threads > 1 else "")
        self._proc = subprocess.Popen([AI_PYTHON, "-I", WORKER], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self._gallery_version = -1
        proc = self._proc
        threading.Thread(target=self._reader, args=(proc,), name="ai-read", daemon=True).start()
        threading.Thread(target=self._stderr, args=(proc,), name="ai-err", daemon=True).start()

    def _kill_worker(self):
        p, self._proc = self._proc, None
        if not p:
            return
        try:
            p.stdin.close()
            p.wait(timeout=2)
        except Exception:           # noqa: BLE001
            p.kill()
        log.info("worker de IA parado")

    def _write(self, obj, data=b""):
        p = self._proc
        if not p or p.poll() is not None:
            raise ConnectionError("worker parado")
        if data:
            obj["len"] = len(data)
        with self._wlock:
            p.stdin.write((json.dumps(obj) + "\n").encode() + data)
            p.stdin.flush()

    def _send_cfg(self):
        s = self.settings()
        self._write({"cmd": "cfg", "features": s["features"], "conf": s["conf"],
                     "model": s["model"], "size": s["size"]})
        self._sync_gallery(force=True)

    def _sync_gallery(self, force=False):
        if force or self._gallery_version != self.faces.version:
            self._gallery_version = self.faces.version
            self._write({"cmd": "gallery", "people": self.faces.gallery()})

    def _stderr(self, proc):
        for line in proc.stderr:
            t = line.decode(errors="replace").rstrip()
            if t and "WARN" not in t:
                log.info("worker: %s", t)

    def _reader(self, proc):
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            ev = msg.get("ev")
            if ev == "ready":
                self.worker_info = {"ort": msg.get("ort"), "cv": msg.get("cv")}
                log.info("worker de IA pronto (onnxruntime %s, OpenCV %s)", msg.get("ort"), msg.get("cv"))
            elif ev == "result" or (ev == "error" and msg.get("id") is None):
                if ev == "error":
                    log.warning("worker: %s", msg.get("error"))
                    self.error = msg.get("error")
                with self._result_cv:
                    self._result = msg
                    self._result_cv.notify_all()
            elif ev in ("reply", "error"):
                with self._reply_cv:
                    self._replies[msg.get("id")] = msg
                    self._reply_cv.notify_all()
        rc = proc.wait()
        if proc is self._proc:
            log.warning("o worker de IA terminou (código %s)", rc)
            self.error = f"o worker terminou (código {rc})"
            with self._result_cv:
                self._result = {"ev": "dead"}
                self._result_cv.notify_all()

    def request_embed(self, jpeg: bytes, timeout=20) -> list:
        """Caras e vetores de um JPEG (para registar uma pessoa). Arranca o worker se for preciso."""
        with self._lock:
            started_here = not (self._proc and self._proc.poll() is None)
            if started_here:
                self._ensure_worker()
            self._req_id += 1
            rid = self._req_id
        try:
            self._write({"cmd": "embed", "id": rid}, jpeg)
            end = time.time() + timeout
            with self._reply_cv:
                while rid not in self._replies:
                    left = end - time.time()
                    if left <= 0:
                        raise TimeoutError("o worker não respondeu")
                    self._reply_cv.wait(left)
                msg = self._replies.pop(rid)
        finally:
            if started_here and not self.active:
                self._kill_worker()
        if msg.get("ev") == "error":
            raise RuntimeError(msg.get("error"))
        return msg.get("faces", [])

    # ── ciclo de análise ───────────────────────────────────────────────────
    def _feed(self):
        n = 0
        restarts = 0
        try:
            while not self._stop_feeder.is_set():
                b = self.budget()
                # ── pausa (energia ou temperatura): sem worker e sem vídeo ──
                if b["paused"]:
                    if self._paused_reason != b["paused"]:
                        log.warning("análise em pausa: %s", b["paused"])
                        self._paused_reason = b["paused"]
                        with self._lock:
                            self._kill_worker()
                        self._hold(False)
                        self.last = None
                        self.emit("ai", {"n": None})
                        self._emit_state()
                    self._stop_feeder.wait(2)
                    continue
                if self._paused_reason:
                    log.info("análise retomada")
                    self._paused_reason = None
                    self._emit_state()
                # passou da corrente para a bateria (ou o contrário): reinicia com outras threads
                want = int(self.cfg.get("ai_threads") or b["threads"])
                if self._proc and self._threads != want:
                    with self._lock:
                        self._kill_worker()
                with self._lock:
                    if not (self._proc and self._proc.poll() is None):
                        self._ensure_worker()
                        try:
                            self._send_cfg()
                        except (ConnectionError, OSError):
                            pass
                        self._emit_state()
                self._hold(True)

                n, jpeg = self.cam.wait_frame(n, timeout=2)
                if jpeg is None or self._stop_feeder.is_set():
                    continue
                t0 = time.time()
                try:
                    self._sync_gallery()
                    with self._result_cv:
                        self._result = None
                    self._write({"cmd": "frame", "n": n}, jpeg)
                    with self._result_cv:
                        if self._result is None:
                            self._result_cv.wait(15)
                        r = self._result
                except (ConnectionError, BrokenPipeError, OSError) as e:
                    r = {"ev": "dead", "error": str(e)}
                if r is None or r.get("ev") == "dead":
                    if self._stop_feeder.is_set():
                        break
                    restarts += 1
                    log.warning("worker de IA sem resposta; a reiniciar (%d)", restarts)
                    with self._lock:
                        self._kill_worker()
                    self._stop_feeder.wait(min(30, 2 * restarts))
                    continue
                restarts = 0
                work = 0.0
                if r.get("ev") == "result":
                    self.error = None
                    work = (r.get("total_ms") or 0) / 1000
                    self._on_result(r, jpeg)
                # Regulador: no máximo b["duty"] do tempo a analisar e b["max_fps"] análises/s
                wait = work * (1 - b["duty"]) / b["duty"]
                if b["max_fps"]:
                    wait = max(wait, 1.0 / b["max_fps"] - (time.time() - t0))
                if wait > 0:
                    self._stop_feeder.wait(wait)
        finally:
            self._hold(False)

    def _on_result(self, r, jpeg):
        self.last = r
        self._done += 1
        now = time.time()
        if now - self._fps_t >= 2:
            self.fps = round(self._done / (now - self._fps_t), 1)
            self._done, self._fps_t = 0, now
        r = dict(r)
        r.pop("ev", None)
        r["fps"] = self.fps
        self.emit("ai", r)
        if self.settings()["events"]:
            self._events(r, jpeg, now)

    def _events(self, r, jpeg, now):
        s = self.settings()
        classes = set(s["event_classes"])
        keys = {}
        for o in r.get("objects", []):
            if o["c"] in classes:
                keys["obj:" + o["c"]] = keys.get("obj:" + o["c"], 0) + 1
        for f in r.get("faces", []):
            if f.get("name"):
                keys["name:" + f["name"]] = 1
        for p in r.get("poses", []):
            for g in p.get("g", []):
                keys["pose:" + g] = keys.get("pose:" + g, 0) + 1
        entered, left = self.presence.update(keys, now)
        for k, cnt in entered:
            kind, label = k.split(":", 1)
            if kind == "obj":
                self.events.add("enter", label, f"{cnt}" if cnt > 1 else None, jpeg)
            elif kind == "name":
                f = next((x for x in r.get("faces", []) if x.get("name") == label), {})
                detail = ", ".join(x for x in (f.get("emo"),) if x)
                self.events.add("person", label, detail or None, jpeg)
            elif kind == "pose":
                self.events.add("pose", label, None, jpeg)
        for k in left:
            kind, label = k.split(":", 1)
            if kind in ("obj", "name"):
                self.events.add("leave", label)
        for c in r.get("codes", []):
            if now - self._codes_seen.get(c["d"], 0) > 60:
                self.events.add("code", c["d"][:300], c.get("t"), jpeg)
            self._codes_seen[c["d"]] = now

    # ── estado para a UI ───────────────────────────────────────────────────
    def _emit_state(self):
        self.emit("ai_state", self.public_state())

    def public_state(self) -> dict:
        s = self.settings()
        return {
            **s,
            "missing": self.missing(),
            "running": bool(self._proc and self._proc.poll() is None),
            "active": self.active,
            "fps": self.fps,
            "error": self.error,
            "worker": self.worker_info,
            "people": len(self.faces.list()),
            "budget": self.budget(),
            "paused": self._paused_reason if self.active else None,
        }

