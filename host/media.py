"""
media.py — fotos e vídeos guardados: lista, miniaturas, metadados e remoção.

Fotos em photos_dir (IMG_*.jpg), vídeos em videos_dir (VID_*.mp4). As miniaturas e os
metadados ficam em ~/.cache/camdeck, e podem ser apagados à vontade (são refeitos).
"""

import io
import json
import logging
import os
import re
import shutil
import subprocess
import time

from PIL import Image

log = logging.getLogger("camdeck.media")

CACHE = os.path.expanduser("~/.cache/camdeck")
THUMB_W = 360
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
EXT = {"photos": (".jpg", ".jpeg"), "videos": (".mp4",)}


class Media:
    def __init__(self, cfg):
        self.cfg = cfg
        os.makedirs(os.path.join(CACHE, "thumbs"), exist_ok=True)
        os.makedirs(os.path.join(CACHE, "meta"), exist_ok=True)

    def dir_of(self, kind: str) -> str:
        if kind not in EXT:
            raise ValueError("tipo inválido")
        return self.cfg.path_of("photos_dir" if kind == "photos" else "videos_dir")

    def path(self, kind: str, name: str) -> str:
        """Caminho de um ficheiro da galeria; recusa nomes com barras ou '..'."""
        if not SAFE_NAME.match(name or "") or name.startswith("."):
            raise ValueError("nome inválido")
        if not name.lower().endswith(EXT[kind]):
            raise ValueError("extensão inválida")
        return os.path.join(self.dir_of(kind), name)

    # ── metadados ──────────────────────────────────────────────────────────
    def _meta_path(self, kind, name):
        return os.path.join(CACHE, "meta", f"{kind}_{name}.json")

    def write_meta(self, kind, name, meta: dict):
        with open(self._meta_path(kind, name), "w") as f:
            json.dump(meta, f)

    def read_meta(self, kind, name) -> dict:
        try:
            with open(self._meta_path(kind, name)) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
        meta = {}
        try:
            if kind == "photos":
                with Image.open(self.path(kind, name)) as im:
                    meta = {"w": im.width, "h": im.height}
            else:
                meta = self._probe(self.path(kind, name))
        except Exception as e:
            log.debug("metadados de %s: %s", name, e)
            return {}
        self.write_meta(kind, name, meta)
        return meta

    @staticmethod
    def _probe(path) -> dict:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration:stream=width,height,codec_type", "-of", "json", path],
                           capture_output=True, text=True, timeout=20)
        j = json.loads(r.stdout or "{}")
        meta = {"duration": round(float(j.get("format", {}).get("duration", 0)), 2)}
        for s in j.get("streams", []):
            if s.get("codec_type") == "video":
                meta.update(w=s.get("width"), h=s.get("height"))
            elif s.get("codec_type") == "audio":
                meta["audio"] = True
        return meta

    # ── lista ──────────────────────────────────────────────────────────────
    def list(self, kind: str = None) -> list:
        kinds = [kind] if kind else list(EXT)
        items = []
        for k in kinds:
            d = self.dir_of(k)
            for name in os.listdir(d):
                if name.startswith(".") or not name.lower().endswith(EXT[k]) or ".part." in name:
                    continue
                p = os.path.join(d, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                item = {"kind": k, "name": name, "bytes": st.st_size, "mtime": st.st_mtime}
                item.update(self.read_meta(k, name))
                items.append(item)
        items.sort(key=lambda i: i["mtime"], reverse=True)
        return items

    def disk(self) -> dict:
        u = shutil.disk_usage(self.dir_of("videos"))
        return {"free": u.free, "total": u.total}

    # ── fotos ──────────────────────────────────────────────────────────────
    def save_photo(self, jpeg: bytes) -> str:
        d = self.dir_of("photos")
        base = time.strftime("IMG_%Y%m%d_%H%M%S")
        name, i = f"{base}.jpg", 1
        while os.path.exists(os.path.join(d, name)):
            name, i = f"{base}_{i}.jpg", i + 1
        with open(os.path.join(d, name), "wb") as f:
            f.write(jpeg)
        try:
            with Image.open(io.BytesIO(jpeg)) as im:
                self.write_meta("photos", name, {"w": im.width, "h": im.height})
        except Exception:
            pass
        return name

    # ── miniaturas ─────────────────────────────────────────────────────────
    def _thumb_path(self, kind, name):
        return os.path.join(CACHE, "thumbs", f"{kind}_{name}.jpg")

    def save_video_thumb(self, name_noext: str, jpeg: bytes):
        """Primeiro quadro da gravação; guardado já com o nome final do MP4."""
        try:
            self._write_thumb(Image.open(io.BytesIO(jpeg)), self._thumb_path("videos", f"{name_noext}.mp4"))
        except Exception as e:
            log.debug("miniatura do vídeo: %s", e)

    @staticmethod
    def _write_thumb(im, dest):
        im = im.convert("RGB")
        im.thumbnail((THUMB_W, THUMB_W * 3 // 4 + 90))
        im.save(dest, "JPEG", quality=80)

    def thumb(self, kind: str, name: str) -> str:
        """Caminho da miniatura, feita na primeira vez que é pedida."""
        src = self.path(kind, name)
        dest = self._thumb_path(kind, name)
        if os.path.exists(dest):        # os ficheiros não mudam; a miniatura sai com o delete
            return dest
        if kind == "photos":
            with Image.open(src) as im:
                im.draft("RGB", (THUMB_W * 2, THUMB_W * 2))   # decodifica em escala reduzida: bem mais rápido
                self._write_thumb(im, dest)
        else:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                            "-frames:v", "1", "-vf", f"scale={THUMB_W}:-2", dest],
                           capture_output=True, timeout=30)
        return dest

    # ── remoção ────────────────────────────────────────────────────────────
    def delete(self, kind: str, name: str):
        os.remove(self.path(kind, name))
        for p in (self._thumb_path(kind, name), self._meta_path(kind, name)):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
