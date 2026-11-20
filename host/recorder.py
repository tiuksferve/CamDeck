"""
recorder.py — gravação de vídeo com som.

Enquanto grava, escreve os JPEG tal como chegam (video.mjpeg) e o PCM do microfone
(audio.raw) numa pasta temporária, sem recodificar nada. Ao parar, o ffmpeg junta
tudo num MP4 H.264 + AAC (o CM5 não tem codificador H.264 por hardware; libx264
"veryfast" chega bem para estas resoluções), que qualquer browser reproduz.

A cadência do vídeo sai dos carimbos de tempo do ESP (primeiro e último quadro), e o
som é alinhado pelo mesmo relógio: começa com silêncio ou é cortado para coincidir com
o primeiro quadro, e os buracos (perdas na USB) são preenchidos com silêncio.

Se o camd morrer a meio, a pasta .rec-* fica com meta.json e é convertida no arranque.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
import time

log = logging.getLogger("camdeck.rec")

RATE = 16000                 # Hz, 16 bits mono
BYTES_PER_MS = RATE * 2 // 1000


class Recorder:
    def __init__(self, cfg, camera, media, emit=None):
        self.cfg = cfg
        self.camera = camera
        self.media = media
        self.emit = emit or (lambda ev, data: None)
        self._lock = threading.Lock()
        self.active = None           # dict da gravação em curso
        self.processing = {}         # nome -> progresso, para a galeria

    # ── estado ─────────────────────────────────────────────────────────────
    def state(self) -> dict:
        a = self.active
        return {
            "recording": bool(a),
            "elapsed": time.time() - a["started"] if a else 0,
            "frames": a["frames"] if a else 0,
            "bytes": a["bytes"] if a else 0,
            "audio": bool(a and a["audio"]),
            "processing": sorted(self.processing),
        }

    # ── iniciar / parar ────────────────────────────────────────────────────
    def start(self):
        with self._lock:
            if self.active:
                raise RuntimeError("já está a gravar")
            name = time.strftime("VID_%Y%m%d_%H%M%S")
            tmp = os.path.join(self.cfg.path_of("videos_dir"), f".rec-{name}")
            os.makedirs(tmp, exist_ok=True)
            audio = bool(self.cfg.get("mic"))
            self.active = a = {
                "name": name, "dir": tmp, "started": time.time(),
                "video": open(os.path.join(tmp, "video.mjpeg"), "wb"),
                "audio_f": open(os.path.join(tmp, "audio.raw"), "wb") if audio else None,
                "audio": audio,
                "frames": 0, "bytes": 0, "t0": None, "t_last": None,
                "audio_pos_ms": None,     # tempo (relógio do ESP) até onde já há som escrito
                "size": self.camera.cfg.get("stream_size"),
            }
            self._write_meta(a)
        self.camera.add_listener(self._on_frame, self._on_audio)
        self.camera.hold("record")
        if audio:
            try:
                self.camera.set_mic(True)
            except Exception as e:
                log.warning("microfone: %s", e)
        log.info("gravação %s iniciada", name)
        self.emit("rec", self.state())
        return name

    def stop(self):
        with self._lock:
            a, self.active = self.active, None
        if not a:
            return None
        self.camera.remove_listener(self._on_frame, self._on_audio)
        self.camera.release("record")
        try:
            self.camera.set_mic(False)
        except Exception:
            pass
        a["video"].close()
        if a["audio_f"]:
            a["audio_f"].close()
        self._write_meta(a, done=True)
        vid_s = (a["t_last"] - a["t0"]) / 1000 if a["t0"] is not None and a["t_last"] else 0
        aud_s = os.path.getsize(os.path.join(a["dir"], "audio.raw")) / (RATE * 2) if a["audio"] else 0
        log.info("gravação %s parada: %d quadros, vídeo %.1f s, som %.1f s (1.º bloco a %s ms)",
                 a["name"], a["frames"], vid_s, aud_s, a.get("first_audio_gap"))
        self.processing[a["name"]] = True
        self.emit("rec", self.state())
        threading.Thread(target=self._finish, args=(a["dir"],), name="rec-mux", daemon=True).start()
        return a["name"]

    @staticmethod
    def _write_meta(a, done=False):
        meta = {k: a[k] for k in ("name", "started", "frames", "t0", "t_last", "audio", "size")}
        meta["done"] = done
        with open(os.path.join(a["dir"], "meta.json"), "w") as f:
            json.dump(meta, f)

    # ── dados que chegam ───────────────────────────────────────────────────
    def _on_frame(self, jpeg: bytes, ts: int):
        a = self.active
        if not a:
            return
        if a["t0"] is None:
            a["t0"] = ts
        if a["frames"] == 4:            # miniatura do 5.º quadro: os primeiros podem vir com a exposição a acertar
            self.media.save_video_thumb(a["name"], jpeg)
        a["video"].write(jpeg)
        a["frames"] += 1
        a["bytes"] += len(jpeg)
        a["t_last"] = ts
        if a["frames"] % 50 == 0:
            self._write_meta(a)

    def _on_audio(self, pcm: bytes, ts: int):
        """O carimbo do ESP é a hora de envio, não a de captura: quando o microfone espera
        por um quadro grande, vários blocos saem seguidos com quase a mesma hora. Por isso
        o relógio só alinha o primeiro bloco com o primeiro quadro; depois os blocos são
        juntos em sequência (o ritmo do I2S é exato, 16 000 amostras/s)."""
        a = self.active
        if not a or not a["audio_f"] or a["t0"] is None:
            return
        if a["audio_pos_ms"] is None:
            start = ts - len(pcm) // BYTES_PER_MS
            gap = start - a["t0"]
            a["first_audio_gap"] = gap
            if gap > 0:                 # o som começou depois do primeiro quadro
                a["audio_f"].write(b"\0" * (gap * BYTES_PER_MS))
            elif gap < 0:               # bloco começou antes: corta o excesso
                pcm = pcm[min(-gap * BYTES_PER_MS, len(pcm)):]
            a["audio_pos_ms"] = 0
        a["audio_f"].write(pcm)

    # ── conversão para MP4 ─────────────────────────────────────────────────
    def recover(self):
        """Converte gravações que ficaram a meio (camd terminado durante a gravação)."""
        vdir = self.cfg.path_of("videos_dir")
        for d in sorted(os.listdir(vdir)):
            path = os.path.join(vdir, d)
            if d.startswith(".rec-") and os.path.isdir(path):
                log.info("a recuperar a gravação %s", d)
                self.processing[d[5:]] = True
                threading.Thread(target=self._finish, args=(path,), daemon=True).start()

    def _finish(self, tmp: str):
        try:
            with open(os.path.join(tmp, "meta.json")) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {"name": os.path.basename(tmp)[5:]}
        name = meta.get("name") or os.path.basename(tmp)[5:]
        try:
            self._mux(tmp, meta, name)
        except Exception:
            log.exception("falha a converter %s", name)
        finally:
            self.processing.pop(name, None)
            self.emit("rec", self.state())
            self.emit("media", {"kind": "videos"})

    def _mux(self, tmp, meta, name):
        video = os.path.join(tmp, "video.mjpeg")
        audio = os.path.join(tmp, "audio.raw")
        if not os.path.exists(video) or os.path.getsize(video) == 0:
            log.warning("gravação %s vazia; a apagar", name)
            shutil.rmtree(tmp, ignore_errors=True)
            return
        frames = meta.get("frames") or 0
        t0, t1 = meta.get("t0"), meta.get("t_last")
        fps = (frames - 1) * 1000.0 / (t1 - t0) if frames > 1 and t0 is not None and t1 and t1 > t0 else 10.0
        fps = max(1.0, min(fps, 60.0))
        out = os.path.join(self.cfg.path_of("videos_dir"), f"{name}.mp4")
        part = out + ".part.mp4"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "mjpeg", "-framerate", f"{fps:.3f}", "-i", video]
        has_audio = meta.get("audio") and os.path.exists(audio) and os.path.getsize(audio) > 0
        if has_audio:
            cmd += ["-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", audio]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(self.cfg.get("video_crf")),
                "-pix_fmt", "yuv420p",
                # dimensões pares (o yuv420p exige)
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        if has_audio:
            # apad + shortest: o som é completado com silêncio até ao fim do vídeo, nunca o corta
            cmd += ["-c:a", "aac", "-b:a", "64k", "-af", "highpass=f=80,apad", "-shortest"]
        cmd += ["-movflags", "+faststart", part]
        t = time.time()
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg: {r.stderr.strip()[-500:]}")
        os.replace(part, out)
        self.media.write_meta("videos", os.path.basename(out), {
            "frames": frames, "fps": round(fps, 2), "duration": round(frames / fps, 2),
            "audio": bool(has_audio), "size": meta.get("size")})
        shutil.rmtree(tmp, ignore_errors=True)
        log.info("%s pronto (%.1f fps, convertido em %.1f s)", os.path.basename(out), fps, time.time() - t)
