"""
camera.py — serviço da câmera: liga o CamLink ao resto do camd.

- Guarda o último quadro JPEG e acorda quem espera por ele (stream MJPEG, gravação).
- Liga o stream só quando há quem veja ou grave, e desliga-o passados idle_stop_s
  (o ESP aquece menos e a USB fica livre).
- Sempre que o ESP (re)arranca, volta a mandar os ajustes guardados.
"""

import logging
import threading
import time

from camlink import CamLink, PKT_AUDIO, PKT_JPEG, PKT_JSON, PKT_SNAP

log = logging.getLogger("camdeck.camera")

# Códigos de resposta do firmware a "set" e "snap"
ERRORS = {-1: "ajuste desconhecido", -2: "o sensor não suporta", -3: "valor inválido",
          -4: "falha na USB"}


class CameraError(Exception):
    pass


class CameraService:
    def __init__(self, cfg, emit=None):
        """emit(evento, dados): avisa os browsers (Server-Sent Events)."""
        self.cfg = cfg
        self.emit = emit or (lambda ev, data: None)
        self.link = CamLink(cfg.get("device"), self._on_packet, self._on_state)
        self.info = {}                  # resposta ao hello/info: sensor, tamanhos, ajustes
        self.stat = {}                  # último {"ev":"stat"} do ESP
        self.ready = False              # ligado e configurado

        self._frame = None
        self._frame_ts = 0
        self._frame_n = 0
        self._cond = threading.Condition()

        self._viewers = 0
        self._last_viewer = 0.0
        self._holds = set()             # outros motivos para manter o stream (gravação, ...)
        self._streaming = False
        self._lock = threading.RLock()

        self._snap_data = None
        self._snap_lock = threading.Lock()

        self._frame_listeners = []      # fn(jpeg, ts_ms)
        self._audio_listeners = []      # fn(pcm, ts_ms)

        self._cfg_started = 0.0         # hora do último _configure (evita repeti-lo no arranque)
        self._rx_frames = 0             # quadros recebidos no último segundo (medido no host)
        self.host_fps = 0.0
        self._stop = threading.Event()

    # ── ciclo de vida ──────────────────────────────────────────────────────
    def start(self):
        self.link.start()
        threading.Thread(target=self._supervisor, name="cam-demand", daemon=True).start()

    def stop(self):
        self._stop.set()
        try:
            if self.link.connected:
                self.link.send("stream", on=False)
                self.link.send("mic", on=False)
        except Exception:
            pass
        self.link.stop()

    # ── eventos da ligação ─────────────────────────────────────────────────
    def _on_state(self, connected: bool):
        if connected:
            # A thread de leitura não pode esperar por respostas: configura noutra thread
            threading.Thread(target=self._configure, name="cam-config", daemon=True).start()
        else:
            self.ready = False
            self._streaming = False
            self.emit("state", self.public_state())

    def _configure(self):
        """Hello e reposição dos ajustes guardados; corre a cada ligação e a cada arranque do ESP."""
        self._cfg_started = time.time()
        time.sleep(0.3)
        try:
            hello = self.link.request("hello", timeout=5)
        except (TimeoutError, ConnectionError) as e:
            log.warning("sem hello do ESP: %s", e)
            return
        log.info("ESP: firmware %s, sensor %s (máx. %s)", hello.get("fw"), hello.get("sensor"),
                 hello.get("max"))
        try:
            size = self.cfg.get("stream_size")
            if size and size != hello.get("framesize"):
                self.link.request("set", key="framesize", val=size)
            if self.cfg.get("fps_max"):
                self.link.request("set", key="fps_max", val=int(self.cfg.get("fps_max")))
            for key, val in (self.cfg.get("settings") or {}).items():
                r = self.link.request("set", key=key, val=val)
                if r.get("r"):
                    log.warning("ajuste %s=%s recusado: %s", key, val, ERRORS.get(r["r"], r["r"]))
            self.link.request("mic", on=self._mic_wanted(), gain=int(self.cfg.get("mic_gain")))
            self.info = self.link.request("info", timeout=5)
            self.info["fw"] = hello.get("fw")
            self.info["psram_total"] = hello.get("psram_total")
        except (TimeoutError, ConnectionError) as e:
            log.warning("falha a configurar o ESP: %s", e)
            return
        with self._lock:
            self._streaming = False     # o ESP arranca parado; o supervisor volta a ligar
        self.ready = True
        self.emit("state", self.public_state())

    def _on_packet(self, typ, payload, ts, seq):
        if typ == PKT_JPEG:
            with self._cond:
                self._frame = payload
                self._frame_ts = ts
                self._frame_n += 1
                self._cond.notify_all()
            self._rx_frames += 1
            for fn in list(self._frame_listeners):
                fn(payload, ts)
        elif typ == PKT_AUDIO:
            for fn in list(self._audio_listeners):
                fn(payload, ts)
        elif typ == PKT_SNAP:
            self._snap_data = payload
        elif typ == PKT_JSON:
            ev = payload.get("ev")
            if ev == "stat":
                self.stat = payload
                self.emit("stat", self.public_stat())
            elif ev == "boot":
                log.info("o ESP reiniciou (firmware %s)", payload.get("fw"))
                if time.time() - self._cfg_started < 3:
                    return              # abrir a porta reinicia o ESP: a configuração já está a decorrer
                self.ready = False
                threading.Thread(target=self._configure, name="cam-config", daemon=True).start()

    # ── procura do stream ──────────────────────────────────────────────────
    def add_viewer(self):
        with self._lock:
            self._viewers += 1
            self._last_viewer = time.time()
        self._update_stream()

    def remove_viewer(self):
        with self._lock:
            self._viewers = max(0, self._viewers - 1)
            self._last_viewer = time.time()

    def hold(self, reason: str):
        with self._lock:
            self._holds.add(reason)
        self._update_stream()

    def release(self, reason: str):
        with self._lock:
            self._holds.discard(reason)
            self._last_viewer = time.time()

    def _wanted(self) -> bool:
        with self._lock:
            if self._viewers or self._holds:
                return True
            return time.time() - self._last_viewer < float(self.cfg.get("idle_stop_s"))

    def _update_stream(self):
        if not self.ready:
            return
        want = self._wanted()
        with self._lock:
            if want == self._streaming:
                return
            self._streaming = want
        try:
            self.link.request("stream", on=want)
            log.info("stream %s", "ligado" if want else "desligado")
        except (TimeoutError, ConnectionError) as e:
            log.warning("stream: %s", e)
            with self._lock:
                self._streaming = not want
        self.emit("state", self.public_state())

    def _supervisor(self):
        while not self._stop.wait(1.0):
            self.host_fps = self._rx_frames
            self._rx_frames = 0
            self._update_stream()

    # ── quadros ────────────────────────────────────────────────────────────
    def wait_frame(self, last_n: int, timeout: float = 5.0):
        """Espera por um quadro mais novo do que last_n; devolve (n, jpeg) ou (last_n, None)."""
        with self._cond:
            if self._frame_n == last_n:
                self._cond.wait(timeout)
            if self._frame_n == last_n:
                return last_n, None
            return self._frame_n, self._frame

    def latest_frame(self):
        return self._frame

    def add_listener(self, frame_fn=None, audio_fn=None):
        if frame_fn:
            self._frame_listeners.append(frame_fn)
        if audio_fn:
            self._audio_listeners.append(audio_fn)

    def remove_listener(self, frame_fn=None, audio_fn=None):
        if frame_fn in self._frame_listeners:
            self._frame_listeners.remove(frame_fn)
        if audio_fn in self._audio_listeners:
            self._audio_listeners.remove(audio_fn)

    # ── comandos ───────────────────────────────────────────────────────────
    def _require(self):
        if not self.link.connected or not self.ready:
            raise CameraError("câmera desligada")

    def set(self, key: str, val):
        self._require()
        r = self.link.request("set", key=key, val=val)
        if r.get("r"):
            raise CameraError(ERRORS.get(r["r"], f"erro {r['r']}"))
        if key == "framesize":
            self.cfg.set("stream_size", val)
        elif key == "fps_max":
            self.cfg.set("fps_max", int(val))
        else:
            settings = dict(self.cfg.get("settings") or {})
            settings[key] = val
            self.cfg.set("settings", settings)
        self.refresh_info()

    def reset_settings(self):
        """Esquece os ajustes guardados e reinicia o ESP (volta aos valores do sensor)."""
        self.cfg.set("settings", {})
        self.reboot()

    def refresh_info(self):
        info = self.link.request("info", timeout=5)
        info["fw"] = self.info.get("fw")
        info["psram_total"] = self.info.get("psram_total")
        self.info = info
        self.emit("state", self.public_state())

    def snap(self, size: str = None) -> bytes:
        self._require()
        with self._snap_lock:
            self._snap_data = None
            kw = {"size": size} if size else {}
            r = self.link.request("snap", timeout=15, **kw)
            if r.get("r"):
                raise CameraError(ERRORS.get(r["r"], f"erro {r['r']}"))
            data, self._snap_data = self._snap_data, None
            if not data:
                raise CameraError("a foto não chegou")
            return data

    def _mic_wanted(self) -> bool:
        return "record" in self._holds and bool(self.cfg.get("mic"))

    def set_mic(self, on: bool, gain: int = None):
        self._require()
        kw = {"on": bool(on)}
        if gain is not None:
            kw["gain"] = int(gain)
        r = self.link.request("mic", **kw)
        self.info["mic"] = r.get("on")
        self.info["mic_gain"] = r.get("gain")
        return r

    def reboot(self):
        self._require()
        self.link.send("reboot")
        self.ready = False

    # ── estado para a UI ───────────────────────────────────────────────────
    def public_stat(self) -> dict:
        s = dict(self.stat)
        s.pop("ev", None)
        s["host_fps"] = self.host_fps
        s["usb_lost"] = self.link.lost
        s["usb_crc"] = self.link.crc_errors
        s["viewers"] = self._viewers
        return s

    def public_state(self) -> dict:
        return {
            "connected": self.link.connected,
            "ready": self.ready,
            "streaming": self._streaming,
            "info": self.info,
            "stream_size": self.cfg.get("stream_size"),
            "snap_size": self.cfg.get("snap_size") or self.info.get("max"),
            "fps_max": self.cfg.get("fps_max"),
            "mic_record": bool(self.cfg.get("mic")),
            "mic_gain": self.cfg.get("mic_gain"),
        }
