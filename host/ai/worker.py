"""
worker.py: análise de imagem do CamDeck (corre no venv de IA, lançado pelo camd).

Fala com o camd por stdin/stdout:
  entrada: uma linha JSON por comando; se tiver "len", seguem-se esses bytes (um JPEG)
  saída:   uma linha JSON por resposta ({"ev": "result" | "reply" | "ready" | "error", ...})

Comandos:
  {"cmd":"cfg", "features":{...}, "conf":0.45, "model":"yolo11n", "size":416}
  {"cmd":"gallery", "people":[{"id":..., "name":..., "embs":[base64 float32, ...]}]}
  {"cmd":"frame", "n":123, "len":L}  + JPEG  ->  {"ev":"result", "n":123, ...}
  {"cmd":"embed", "id":7, "len":L}   + JPEG  ->  {"ev":"reply", "id":7, "faces":[...]}

Todas as coordenadas devolvidas são relativas (0..1) à imagem recebida.
Modelos em ~/.local/share/camdeck/models (descarregados por setup/install-ai.sh).

Para não sobrecarregar o uConsole (só CPU, alimentado por baterias):
- corre com prioridade baixa (nice 10) e com poucas threads (CAMDECK_AI_THREADS);
- imagens largas (HD e acima) são descodificadas a metade do tamanho: os modelos usam 320–640 px;
- sem movimento na imagem, devolve o resultado anterior sem correr os modelos (até 3 s);
- com o detetor de objetos ligado, pose e caras só correm quando há indício de pessoa;
- humor, idade e nome de cada cara são guardados por "pista" (a mesma cara de um quadro para o
  outro) e só recalculados de tempos a tempos.
"""

import base64
import json
import os
import sys
import time
import traceback

import cv2
import numpy as np
import onnxruntime as ort

MODELS = os.path.expanduser(os.environ.get("CAMDECK_MODELS", "~/.local/share/camdeck/models"))

COCO_PT = [
    "pessoa", "bicicleta", "carro", "mota", "avião", "autocarro", "comboio", "camião", "barco",
    "semáforo", "boca de incêndio", "sinal de stop", "parquímetro", "banco", "pássaro", "gato",
    "cão", "cavalo", "ovelha", "vaca", "elefante", "urso", "zebra", "girafa", "mochila",
    "guarda-chuva", "mala de mão", "gravata", "mala de viagem", "frisbee", "esquis", "snowboard",
    "bola", "papagaio de papel", "taco de basebol", "luva de basebol", "skate", "prancha de surf",
    "raquete de ténis", "garrafa", "copo de vinho", "chávena", "garfo", "faca", "colher", "taça",
    "banana", "maçã", "sanduíche", "laranja", "brócolos", "cenoura", "cachorro-quente", "piza",
    "dónute", "bolo", "cadeira", "sofá", "vaso com planta", "cama", "mesa", "sanita", "televisão",
    "portátil", "rato", "comando", "teclado", "telemóvel", "micro-ondas", "forno", "torradeira",
    "lava-loiça", "frigorífico", "livro", "relógio", "jarra", "tesoura", "peluche",
    "secador de cabelo", "escova de dentes",
]
assert len(COCO_PT) == 80

# FER+ (ordem do modelo)
EMOTIONS = ["neutro", "feliz", "surpreso", "triste", "zangado", "nojo", "medo", "desprezo"]

SFACE_MATCH = 0.40          # semelhança de cosseno mínima para dar um nome (SFace sugere 0,363)

MOTION_CELL = 10            # diferença (0–255) numa célula da grelha 64×48 para contar como mudança
MOTION_FRAC = 0.004         # fração de células mudadas para haver movimento
STATIC_MAX_S = 3.0          # mesmo sem movimento, analisa pelo menos uma vez neste intervalo
PERSON_HINT = 0.25          # confiança mínima de "pessoa" para correr pose e caras
FULLRES_FACE_PX = 90        # caras mais pequenas do que isto (na imagem reduzida) usam a imagem inteira
# De quantos em quantos quadros analisados se recalcula cada atributo de uma cara seguida
EMO_EVERY, AGE_EVERY, NAME_EVERY, NAME_RETRY = 2, 10, 15, 2


# O protocolo usa uma cópia do stdout; o fd 1 passa a ir para o stderr, para que nenhuma biblioteca
# que escreva no stdout estrague as respostas.
_proto = os.fdopen(os.dup(1), "w", encoding="utf-8")
os.dup2(2, 1)


def _np(o):
    """Tipos do numpy no JSON."""
    return o.tolist() if hasattr(o, "tolist") else str(o)


def out(obj):
    _proto.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=_np) + "\n")
    _proto.flush()


def session(name, threads):
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    so.log_severity_level = 3
    return ort.InferenceSession(os.path.join(MODELS, name), so, providers=["CPUExecutionProvider"])


def letterbox(img, size):
    h, w = img.shape[:2]
    r = min(size / h, size / w)
    nw, nh = int(round(w * r)), int(round(h * r))
    pad = np.full((size, size, 3), 114, np.uint8)
    pad[:nh, :nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    return pad, r


def rel(x, y, bw, bh, w, h):
    """Caixa em píxeis -> [x, y, l, a] relativos (0..1), recortada à imagem."""
    x0, y0 = max(0.0, float(x)), max(0.0, float(y))
    x1, y1 = min(float(w), float(x + bw)), min(float(h), float(y + bh))
    return [round(x0 / w, 4), round(y0 / h, 4), round((x1 - x0) / w, 4), round((y1 - y0) / h, 4)]


def iou(a, b):
    ax1, ay1, bx1, by1 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw, ih = max(0.0, min(ax1, bx1) - max(a[0], b[0])), max(0.0, min(ay1, by1) - max(a[1], b[1]))
    inter = iw * ih
    return inter / (a[2] * a[3] + b[2] * b[3] - inter + 1e-9)


def nms(boxes, scores, conf, iou=0.45):
    idx = cv2.dnn.NMSBoxes(boxes.tolist(), scores.tolist(), conf, iou)
    return np.array(idx, dtype=int).reshape(-1)


class Analyzer:
    def __init__(self):
        self.threads = int(os.environ.get("CAMDECK_AI_THREADS", "2"))
        self.person_max = 0.0
        self.features = {}
        self.conf = 0.45
        self.model = "yolo11n"
        self.size = 416
        self._sess = {}
        self.gallery = []           # [(id, name, matriz N×128 normalizada)]
        self._yunet = None
        self._yunet_size = None
        self._sface = None
        self._qr = None
        self._bar = None
        self._frame_i = 0               # quadros analisados de facto (sem contar os parados)
        self._codes_cache = []
        self._tiny = None               # miniatura do último quadro analisado (deteção de movimento)
        self._last = None
        self._last_t = 0.0
        self._last_poses = []
        self._tracks = []               # pistas das caras: atributos guardados
        self._gallery_n = 0

    # ── modelos (carregados só quando a função é ligada) ──────────────────
    def sess(self, name):
        if name not in self._sess:
            self._sess[name] = session(name, self.threads)
        return self._sess[name]

    def yunet(self, w, h):
        if self._yunet is None:
            self._yunet = cv2.FaceDetectorYN.create(os.path.join(MODELS, "yunet.onnx"), "", (w, h), 0.6, 0.3, 20)
        if self._yunet_size != (w, h):
            self._yunet.setInputSize((w, h))
            self._yunet_size = (w, h)
        return self._yunet

    def sface(self):
        if self._sface is None:
            self._sface = cv2.FaceRecognizerSF.create(os.path.join(MODELS, "sface.onnx"), "")
        return self._sface

    def configure(self, c):
        self.features = c.get("features") or {}
        self.conf = float(c.get("conf", self.conf))
        model = c.get("model", self.model)
        size = int(c.get("size", self.size))
        if model != self.model or size != self.size:
            self._sess = {k: v for k, v in self._sess.items() if not k.startswith(("yolo11n_", "yolox"))}
        self.model, self.size = model, size
        # liberta a memória dos modelos que deixaram de ser usados
        if not self.features.get("pose"):
            self._sess.pop("yolo11n-pose_320.onnx", None)

    def set_gallery(self, people):
        g = []
        for p in people:
            embs = [np.frombuffer(base64.b64decode(e), np.float32) for e in p.get("embs", [])]
            if not embs:
                continue
            m = np.stack(embs)
            m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
            g.append((p["id"], p["name"], m))
        self.gallery = g
        self._gallery_n += 1
        for t in self._tracks:          # a galeria mudou: volta a identificar
            t["name_i"] = -999

    # ── objetos ────────────────────────────────────────────────────────────
    def objects(self, img):
        h, w = img.shape[:2]
        if self.model == "yolox_nano":
            size = 416
            x, r = letterbox(img, size)
            s = self.sess("yolox_nano.onnx")
            o = s.run(None, {s.get_inputs()[0].name: x.transpose(2, 0, 1)[None].astype(np.float32)})[0][0]
            grids, strides = [], []
            for st in (8, 16, 32):
                n = size // st
                xv, yv = np.meshgrid(np.arange(n), np.arange(n))
                grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
                strides.append(np.full((n * n, 1), st))
            g, st = np.concatenate(grids), np.concatenate(strides)
            xy, wh = (o[:, :2] + g) * st, np.exp(o[:, 2:4]) * st
            sc = o[:, 4:5] * o[:, 5:]
        else:
            size = self.size if self.size in (320, 416) else 416
            x, r = letterbox(img, size)
            s = self.sess(f"yolo11n_{size}.onnx")
            blob = (x[..., ::-1].transpose(2, 0, 1)[None] / 255.0).astype(np.float32)
            o = s.run(None, {s.get_inputs()[0].name: blob})[0][0].T
            xy, wh, sc = o[:, :2], o[:, 2:4], o[:, 4:]
        self.person_max = float(sc[:, 0].max()) if len(sc) else 0.0
        cls = sc.argmax(1)
        conf = sc[np.arange(len(sc)), cls]
        k = conf >= self.conf
        if not k.any():
            return []
        boxes = np.concatenate([xy[k] - wh[k] / 2, wh[k]], 1) / r
        conf, cls = conf[k], cls[k]
        res = []
        for i in nms(boxes, conf, self.conf):
            bx, by, bw, bh = boxes[i]
            res.append({"c": COCO_PT[cls[i]], "k": int(cls[i]), "p": round(float(conf[i]), 2),
                        "b": rel(bx, by, bw, bh, w, h)})
        return res

    # ── pose ───────────────────────────────────────────────────────────────
    def poses(self, img):
        h, w = img.shape[:2]
        x, r = letterbox(img, 320)
        s = self.sess("yolo11n-pose_320.onnx")
        blob = (x[..., ::-1].transpose(2, 0, 1)[None] / 255.0).astype(np.float32)
        o = s.run(None, {s.get_inputs()[0].name: blob})[0][0].T     # (N, 56)
        conf = o[:, 4]
        k = conf >= max(0.4, self.conf)
        if not k.any():
            return []
        o, conf = o[k], conf[k]
        boxes = np.concatenate([o[:, :2] - o[:, 2:4] / 2, o[:, 2:4]], 1)
        res = []
        for i in nms(boxes, conf, max(0.4, self.conf)):
            kp = o[i, 5:].reshape(17, 3).copy()
            kp[:, 0] /= r * w
            kp[:, 1] /= r * h
            bx, by, bw, bh = boxes[i] / r
            res.append({"p": round(float(conf[i]), 2),
                        "b": rel(bx, by, bw, bh, w, h),
                        "kp": [[round(float(a), 3) for a in k] for k in kp],
                        "g": gesture(kp, bw / w, bh / h)})
        return res

    # ── caras ──────────────────────────────────────────────────────────────
    def detect_faces(self, img):
        h, w = img.shape[:2]
        sc = min(1.0, 640 / w)
        small = cv2.resize(img, (int(w * sc), int(h * sc))) if sc < 1 else img
        _, f = self.yunet(small.shape[1], small.shape[0]).detect(small)
        if f is None:
            return []
        f = f.copy()
        f[:, :14] /= sc                 # coordenadas da imagem inteira
        return sorted(f, key=lambda r: -r[2] * r[3])[:6]

    def faces(self, img, scale, full_fn, want_emotion, want_age, want_names):
        """img pode estar reduzida (scale = 2); full_fn() descodifica a imagem inteira, se for preciso."""
        h, w = img.shape[:2]
        i = self._frame_i
        res, used = [], set()
        for f in self.detect_faces(img):
            x, y, fw, fh = f[:4]
            b = rel(x, y, fw, fh, w, h)
            item = {"b": b, "p": round(float(f[14]), 2)}
            # pista: a cara com mais sobreposição no quadro anterior
            best, best_iou = None, 0.3
            for t in self._tracks:
                if id(t) in used:
                    continue
                v = iou(b, t["b"])
                if v > best_iou:
                    best, best_iou = t, v
            if best is None:
                best = {"emo_i": -999, "age_i": -999, "name_i": -999, "ages": [], "sexes": []}
                self._tracks.append(best)
            t = best
            used.add(id(t))
            t["b"], t["seen"] = b, i
            if fw * scale < 36:         # cara pequena demais para classificar
                res.append(item)
                continue
            src, fs = img, f
            need = (want_emotion and i - t["emo_i"] >= EMO_EVERY) or \
                   (want_age and (len(t["ages"]) < 3 or i - t["age_i"] >= AGE_EVERY)) or \
                   (want_names and self.gallery and (i - t["name_i"] >= (NAME_EVERY if t.get("name") else NAME_RETRY)))
            if need and scale > 1 and fw < FULLRES_FACE_PX:
                src = full_fn()
                fs = f.copy()
                fs[:14] *= scale
            if want_emotion:
                if i - t["emo_i"] >= EMO_EVERY:
                    t.update(self.emotion(src, fs))
                    t["emo_i"] = i
                if "emo" in t:
                    item["emo"], item["emo_p"] = t["emo"], t["emo_p"]
            if want_age:
                if len(t["ages"]) < 3 or i - t["age_i"] >= AGE_EVERY:
                    ag = self.age_gender(src, fs)
                    t["ages"] = (t["ages"] + [ag["age"]])[-5:]
                    t["sexes"] = (t["sexes"] + [ag["sex"]])[-5:]
                    t["age_i"] = i
                item["age"] = int(round(sum(t["ages"]) / len(t["ages"])))
                item["sex"] = max(set(t["sexes"]), key=t["sexes"].count)
            if want_names and self.gallery:
                if i - t["name_i"] >= (NAME_EVERY if t.get("name") else NAME_RETRY):
                    t.update(self.identify(src, fs))
                    t["name_i"] = i
                if "sim" in t:
                    item["name"], item["sim"] = t.get("name"), t["sim"]
                    if t.get("pid"):
                        item["pid"] = t["pid"]
            res.append(item)
        self._tracks = [t for t in self._tracks if i - t.get("seen", i) <= 10]
        return res

    def emotion(self, img, f):
        x, y, fw, fh = f[:4]
        m = 0.1
        x0, y0 = int(max(0, x - fw * m)), int(max(0, y - fh * m))
        x1, y1 = int(min(img.shape[1], x + fw * (1 + m))), int(min(img.shape[0], y + fh * (1 + m)))
        crop = img[y0:y1, x0:x1]
        if crop.size == 0:
            return {}
        g = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), (64, 64)).astype(np.float32)[None, None]
        s = self.sess("ferplus.onnx")
        o = s.run(None, {s.get_inputs()[0].name: g})[0][0]
        e = np.exp(o - o.max())
        e /= e.sum()
        i = int(e.argmax())
        return {"emo": EMOTIONS[i], "emo_p": round(float(e[i]), 2)}

    def age_gender(self, img, f):
        x, y, fw, fh = f[:4]
        cx, cy = x + fw / 2, y + fh / 2
        sc = 96 / (max(fw, fh) * 1.5)
        M = np.array([[sc, 0, 48 - cx * sc], [0, sc, 48 - cy * sc]], np.float32)
        a = cv2.warpAffine(img, M, (96, 96), borderValue=0)
        blob = cv2.dnn.blobFromImage(a, 1.0, (96, 96), (0, 0, 0), swapRB=True)
        s = self.sess("genderage.onnx")
        p = s.run(None, {s.get_inputs()[0].name: blob})[0][0]
        return {"sex": "M" if p[1] > p[0] else "F", "age": int(round(float(p[2]) * 100))}

    def embedding(self, img, f):
        sf = self.sface()
        al = sf.alignCrop(img, f)
        e = sf.feature(al).reshape(-1).astype(np.float32)
        return e / (np.linalg.norm(e) + 1e-9), al

    def identify(self, img, f):
        e, _ = self.embedding(img, f)
        best, best_s = None, -1.0
        for pid, name, m in self.gallery:
            s = float((m @ e).max())
            if s > best_s:
                best, best_s = (pid, name), s
        if best and best_s >= SFACE_MATCH:
            return {"name": best[1], "pid": best[0], "sim": round(best_s, 2)}
        return {"name": None, "pid": None, "sim": round(best_s, 2)}

    # ── códigos ────────────────────────────────────────────────────────────
    def codes(self, img):
        if self._qr is None:
            self._qr = cv2.QRCodeDetectorAruco()
            self._bar = cv2.barcode.BarcodeDetector()
        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        res = []
        try:
            ok, texts, pts, _ = self._qr.detectAndDecodeMulti(gray)
            if ok:
                for t, p in zip(texts, pts):
                    if t:
                        res.append({"t": "QR", "d": t, "pts": [[round(float(a) / w, 4), round(float(b) / h, 4)] for a, b in p]})
        except cv2.error:
            pass
        try:
            ok, texts, types, pts = self._bar.detectAndDecodeWithType(gray)
            if ok:
                for t, ty, p in zip(texts, types, pts):
                    if t:
                        res.append({"t": ty or "código", "d": t, "pts": [[round(float(a) / w, 4), round(float(b) / h, 4)] for a, b in p]})
        except (cv2.error, AttributeError):
            pass
        return res

    # ── um quadro ──────────────────────────────────────────────────────────
    def analyze(self, jpeg):
        buf = np.frombuffer(jpeg, np.uint8)
        # Movimento: miniatura em cinzento (descodificada a 1/8, quase grátis) contra a do último quadro analisado
        g8 = cv2.imdecode(buf, cv2.IMREAD_REDUCED_GRAYSCALE_8)
        if g8 is None:
            raise ValueError("JPEG inválido")
        tiny = cv2.resize(g8, (64, 48), interpolation=cv2.INTER_AREA).astype(np.int16)
        now = time.time()
        if self._tiny is not None and self._last is not None:
            moved = float((np.abs(tiny - self._tiny) > MOTION_CELL).mean())
            if moved < MOTION_FRAC and now - self._last_t < STATIC_MAX_S:
                r = dict(self._last)
                r.update(static=True, ms={})
                return r
        self._tiny = tiny

        # Imagens largas: metade do tamanho chega para os modelos (320–640 px) e descodifica 4× mais depressa
        scale = 2 if g8.shape[1] * 8 >= 1000 else 1
        img = cv2.imdecode(buf, cv2.IMREAD_REDUCED_COLOR_2 if scale == 2 else cv2.IMREAD_COLOR)
        full = {}

        def full_fn():
            if "img" not in full:
                full["img"] = cv2.imdecode(buf, cv2.IMREAD_COLOR) if scale > 1 else img
            return full["img"]

        f = self.features
        self._frame_i += 1
        i = self._frame_i
        r, ms = {"w": img.shape[1] * scale, "h": img.shape[0] * scale}, {}

        def timed(key, fn, *a):
            t = time.perf_counter()
            v = fn(*a)
            ms[key] = round((time.perf_counter() - t) * 1000, 1)
            return v

        people = True                   # há (ou pode haver) pessoas?
        if f.get("objects"):
            r["objects"] = timed("objects", self.objects, img)
            # se o detetor nem desconfia de uma pessoa, poupa a pose e as caras (mas confirma 1 vez em 4)
            people = self.person_max >= PERSON_HINT or i % 4 == 0
        if f.get("pose"):
            if not people:
                self._last_poses = []
            elif i % 2 == 1 or not self._last_poses or not f.get("objects"):
                self._last_poses = timed("pose", self.poses, img)
            r["poses"] = self._last_poses
        if f.get("faces") or f.get("emotion") or f.get("age") or f.get("names"):
            if people:
                r["faces"] = timed("faces", self.faces, img, scale, full_fn,
                                   f.get("emotion"), f.get("age"), f.get("names"))
            else:
                r["faces"] = []
        if f.get("codes"):
            # os códigos não mudam depressa: lê um quadro em cada três (na imagem inteira: precisam de detalhe)
            if i % 3 == 1:
                self._codes_cache = timed("codes", self.codes, full_fn())
            r["codes"] = self._codes_cache
        r["ms"] = ms
        self._last, self._last_t = {k: v for k, v in r.items() if k != "ms"}, now
        return r

    def embed(self, jpeg):
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("JPEG inválido")
        h, w = img.shape[:2]
        res = []
        for f in self.detect_faces(img):
            e, al = self.embedding(img, f)
            ok, crop = cv2.imencode(".jpg", al, [cv2.IMWRITE_JPEG_QUALITY, 90])
            res.append({"b": rel(f[0], f[1], f[2], f[3], w, h),
                        "p": round(float(f[14]), 2), "px": int(f[2]),
                        "emb": base64.b64encode(e.tobytes()).decode(),
                        "crop": base64.b64encode(crop.tobytes()).decode() if ok else None})
        return res


def gesture(kp, bw, bh):
    """Gestos simples a partir dos 17 pontos COCO (x, y, confiança)."""
    NOSE, LSH, RSH, LWR, RWR, LHIP, RHIP = 0, 5, 6, 9, 10, 11, 12
    ok = lambda i: kp[i, 2] > 0.4  # noqa: E731
    tags = []
    if ok(LSH) and ok(RSH) and ok(LHIP) and ok(RHIP):
        sh = (kp[LSH, :2] + kp[RSH, :2]) / 2
        hp = (kp[LHIP, :2] + kp[RHIP, :2]) / 2
        dx, dy = abs(sh[0] - hp[0]) * bw, abs(sh[1] - hp[1]) * bh
        if dx > dy * 1.3 and bw > bh:
            tags.append("deitado")
    head_y = kp[NOSE, 1] if ok(NOSE) else min(kp[LSH, 1], kp[RSH, 1]) if ok(LSH) and ok(RSH) else None
    if head_y is not None:
        up_l = ok(LWR) and kp[LWR, 1] < head_y
        up_r = ok(RWR) and kp[RWR, 1] < head_y
        if up_l and up_r:
            tags.append("mãos no ar")
        elif up_l or up_r:
            tags.append("mão levantada")
    return tags


def main():
    try:
        os.nice(10)                     # o ambiente de trabalho e o camd passam à frente
    except OSError:
        pass
    cv2.setNumThreads(1)
    a = Analyzer()
    inp = sys.stdin.buffer
    out({"ev": "ready", "pid": os.getpid(), "ort": ort.__version__, "cv": cv2.__version__})
    while True:
        line = inp.readline()
        if not line:
            return
        try:
            cmd = json.loads(line)
        except ValueError:
            continue
        data = inp.read(cmd["len"]) if cmd.get("len") else b""
        c = cmd.get("cmd")
        try:
            if c == "cfg":
                a.configure(cmd)
            elif c == "gallery":
                a.set_gallery(cmd.get("people", []))
            elif c == "frame":
                t = time.perf_counter()
                r = a.analyze(data)
                r.update(ev="result", n=cmd.get("n"), total_ms=round((time.perf_counter() - t) * 1000, 1))
                out(r)
            elif c == "embed":
                out({"ev": "reply", "id": cmd.get("id"), "faces": a.embed(data)})
        except Exception as e:      # noqa: BLE001 — o processo nunca morre por um quadro
            out({"ev": "error", "id": cmd.get("id"), "n": cmd.get("n"), "error": f"{type(e).__name__}: {e}",
                 "trace": traceback.format_exc(limit=3)})


if __name__ == "__main__":
    main()
