#!/usr/bin/python3
"""
camd.py — servidor do CamDeck no uConsole (porta 8090 por omissão).

Serve a interface web, o stream MJPEG (/stream.mjpg), fotos, gravações e os ajustes da
câmera XIAO ESP32-S3 Sense ligada por USB. O estado ao vivo vai para o browser por
Server-Sent Events (/api/events).

Uso:
  camd.py                    arranca o servidor
  camd.py --set-password     muda a palavra-passe (pede-a no terminal)
"""

import argparse
import base64
import getpass
import hmac
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from datetime import timedelta
from functools import wraps

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory, session

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from analyzer import AnalyzerService             # noqa: E402
from camera import CameraError, CameraService   # noqa: E402
from config import INITIAL_PASSWORD_PATH, Config, local_token  # noqa: E402
from faces import FaceDB                         # noqa: E402
from media import Media                          # noqa: E402
from power import PowerMonitor                   # noqa: E402
from recorder import Recorder                    # noqa: E402

VERSION = "0.2.0"
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
log = logging.getLogger("camdeck")


# ── eventos para os browsers (SSE) ──────────────────────────────────────────
class EventHub:
    def __init__(self):
        self._clients = set()
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=100)
        with self._lock:
            self._clients.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._clients.discard(q)

    def emit(self, event: str, data):
        msg = f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass                    # cliente lento: perde eventos, não atrasa os outros


class LoginThrottle:
    """Atrasa tentativas falhadas por IP (5 livres, depois espera crescente)."""

    def __init__(self):
        self._fails = {}

    def wait_s(self, ip) -> float:
        n, last = self._fails.get(ip, (0, 0))
        if n < 5:
            return 0
        return max(0.0, last + min(2 ** (n - 5), 300) - time.time())

    def fail(self, ip):
        n, _ = self._fails.get(ip, (0, 0))
        self._fails[ip] = (n + 1, time.time())

    def ok(self, ip):
        self._fails.pop(ip, None)


def create_app(cfg: Config, cam: CameraService, rec: Recorder, media: Media, hub: EventHub,
               ai: AnalyzerService, faces: FaceDB, power: PowerMonitor):
    app = Flask(__name__, static_folder=None)
    app.secret_key = cfg.get("secret_key")
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=30), MAX_CONTENT_LENGTH=64 * 1024)
    throttle = LoginThrottle()
    token = local_token()

    def authed():
        if session.get("camdeck_auth") is True:
            return True
        # programas locais (dashd): cabeçalho X-CamDeck-Token, só a partir do próprio uConsole
        t = request.headers.get("X-CamDeck-Token", "")
        return bool(t) and request.remote_addr in ("127.0.0.1", "::1") and hmac.compare_digest(t, token)

    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not authed():
                return jsonify(error="sessão expirada"), 401
            return fn(*a, **kw)
        return wrapper

    def body():
        return request.get_json(silent=True) or {}

    def cam_call(fn, *a, **kw):
        try:
            return fn(*a, **kw), None
        except CameraError as e:
            return None, (jsonify(error=str(e)), 409)
        except (TimeoutError, ConnectionError) as e:
            return None, (jsonify(error=f"câmera sem resposta ({e})"), 504)

    @app.after_request
    def no_cache_api(resp):
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    # ── páginas e ficheiros estáticos ──────────────────────────────────────
    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/<path:p>")
    def static_files(p):
        if p.split("/")[0] not in ("css", "js", "img"):
            abort(404)
        return send_from_directory(WEB_DIR, p)

    # ── sessão ─────────────────────────────────────────────────────────────
    @app.get("/api/session")
    def api_session():
        return jsonify(authed=authed(), version=VERSION,
                       first_run=os.path.exists(INITIAL_PASSWORD_PATH))

    @app.post("/api/login")
    def api_login():
        ip = request.remote_addr
        wait = throttle.wait_s(ip)
        if wait > 0:
            return jsonify(error=f"demasiadas tentativas; espere {int(wait) + 1} s"), 429
        if not cfg.check_password(body().get("password", "")):
            throttle.fail(ip)
            time.sleep(0.5)
            return jsonify(error="palavra-passe errada"), 403
        throttle.ok(ip)
        session.clear()
        session["camdeck_auth"] = True
        session.permanent = True
        return jsonify(ok=True)

    @app.post("/api/logout")
    def api_logout():
        session.clear()
        return jsonify(ok=True)

    @app.post("/api/password")
    @login_required
    def api_password():
        b = body()
        if not cfg.check_password(b.get("current", "")):
            return jsonify(error="a palavra-passe atual está errada"), 403
        try:
            cfg.set_password(b.get("new", ""))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True)

    # ── estado ─────────────────────────────────────────────────────────────
    @app.get("/api/state")
    @login_required
    def api_state():
        return jsonify(camera=cam.public_state(), stat=cam.public_stat(), rec=rec.state(),
                       disk=media.disk(), ai=ai.public_state(), power=power.state, version=VERSION)

    @app.get("/api/events")
    @login_required
    def api_events():
        q = hub.subscribe()

        def gen():
            try:
                yield "retry: 3000\n\n"
                yield f"event: state\ndata: {json.dumps(cam.public_state())}\n\n"
                yield f"event: rec\ndata: {json.dumps(rec.state())}\n\n"
                yield f"event: ai_state\ndata: {json.dumps(ai.public_state())}\n\n"
                yield f"event: power\ndata: {json.dumps(power.state)}\n\n"
                while True:
                    try:
                        yield q.get(timeout=15)
                    except queue.Empty:
                        yield ": ping\n\n"
            finally:
                hub.unsubscribe(q)

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ── stream ─────────────────────────────────────────────────────────────
    @app.get("/stream.mjpg")
    @login_required
    def stream():
        def gen():
            cam.add_viewer()
            n = 0
            try:
                while True:
                    n, frame = cam.wait_frame(n, timeout=5)
                    if frame is None:
                        continue
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
            finally:
                cam.remove_viewer()

        return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/frame.jpg")
    @login_required
    def frame():
        """Último quadro do stream (pré-visualizações, DashDeck). Com ?after=n espera por um quadro
        mais novo do que n; o número do quadro vem no cabeçalho X-Frame."""
        after = request.args.get("after", type=int)
        cam.add_viewer()
        try:
            if after is None:
                after = 0 if cam.latest_frame() is None else -1
            n, f = cam.wait_frame(after, timeout=5)
        finally:
            cam.remove_viewer()
        if not f:
            abort(503)
        return Response(f, mimetype="image/jpeg", headers={"Cache-Control": "no-store", "X-Frame": str(n)})

    # ── comandos ───────────────────────────────────────────────────────────
    @app.post("/api/set")
    @login_required
    def api_set():
        b = body()
        key, val = b.get("key"), b.get("val")
        if not isinstance(key, str):
            return jsonify(error="falta key"), 400
        if key == "framesize" and rec.active:
            return jsonify(error="não se muda a resolução durante a gravação"), 409
        _, err = cam_call(cam.set, key, val)
        return err or jsonify(ok=True, camera=cam.public_state())

    @app.post("/api/reset")
    @login_required
    def api_reset():
        if rec.active:
            return jsonify(error="a gravar"), 409
        _, err = cam_call(cam.reset_settings)
        return err or jsonify(ok=True)

    @app.post("/api/prefs")
    @login_required
    def api_prefs():
        """Preferências do host: tamanho das fotos, som na gravação, ganho do microfone."""
        b = body()
        if "snap_size" in b:
            sizes = [s["name"] for s in cam.info.get("sizes", [])]
            if b["snap_size"] and b["snap_size"] not in sizes:
                return jsonify(error="tamanho inválido"), 400
            cfg.set("snap_size", b["snap_size"])
        if "mic" in b:
            cfg.set("mic", bool(b["mic"]))
        if "mic_gain" in b:
            g = max(1, min(16, int(b["mic_gain"])))
            cfg.set("mic_gain", g)
            if cam.ready:
                cam_call(cam.set_mic, cam.info.get("mic", False), g)
        hub.emit("state", cam.public_state())
        return jsonify(ok=True, camera=cam.public_state())

    @app.post("/api/snap")
    @login_required
    def api_snap():
        size = body().get("size") or cfg.get("snap_size") or None
        if rec.active:
            size = None                 # a gravar: foto do quadro atual, sem parar o stream
            f = cam.latest_frame()
            if not f:
                return jsonify(error="sem imagem"), 503
            data = f
        else:
            data, err = cam_call(cam.snap, size)
            if err:
                return err
        name = media.save_photo(data)
        log.info("foto %s (%d KB)", name, len(data) // 1024)
        hub.emit("media", {"kind": "photos", "name": name})
        return jsonify(ok=True, name=name, bytes=len(data))

    @app.post("/api/record")
    @login_required
    def api_record():
        on = bool(body().get("on"))
        try:
            if on:
                if not cam.ready:
                    return jsonify(error="câmera desligada"), 409
                name = rec.start()
            else:
                name = rec.stop()
        except RuntimeError as e:
            return jsonify(error=str(e)), 409
        return jsonify(ok=True, name=name, rec=rec.state())

    @app.post("/api/reboot")
    @login_required
    def api_reboot():
        if rec.active:
            rec.stop()
        _, err = cam_call(cam.reboot)
        return err or jsonify(ok=True)

    # ── galeria ────────────────────────────────────────────────────────────
    @app.get("/api/media")
    @login_required
    def api_media():
        kind = request.args.get("kind") or None
        try:
            items = media.list(kind)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(items=items, processing=rec.state()["processing"], disk=media.disk())

    def media_path(kind, name, thumb=False):
        try:
            return media.thumb(kind, name) if thumb else media.path(kind, name)
        except ValueError:
            abort(400)
        except FileNotFoundError:
            abort(404)

    @app.get("/media/<kind>/<name>")
    @login_required
    def media_file(kind, name):
        p = media_path(kind, name)
        if not os.path.exists(p):
            abort(404)
        return send_file(p, conditional=True, as_attachment=request.args.get("dl") == "1",
                         download_name=name, max_age=3600)

    @app.get("/thumb/<kind>/<name>")
    @login_required
    def media_thumb(kind, name):
        p = media_path(kind, name, thumb=True)
        if not os.path.exists(p):
            abort(404)
        return send_file(p, mimetype="image/jpeg", max_age=86400)

    @app.delete("/api/media/<kind>/<name>")
    @login_required
    def media_delete(kind, name):
        try:
            media.delete(kind, name)
        except ValueError:
            abort(400)
        except FileNotFoundError:
            abort(404)
        hub.emit("media", {"kind": kind, "deleted": name})
        return jsonify(ok=True)

    # ── análise (IA) ───────────────────────────────────────────────────────
    @app.get("/api/ai")
    @login_required
    def api_ai():
        return jsonify(state=ai.public_state(), people=faces.list())

    @app.post("/api/ai")
    @login_required
    def api_ai_set():
        try:
            ai.update(body())
        except (TypeError, ValueError) as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True, state=ai.public_state())

    @app.get("/api/ai/events")
    @login_required
    def api_ai_events():
        return jsonify(items=ai.events.list())

    @app.delete("/api/ai/events")
    @login_required
    def api_ai_events_clear():
        ai.events.clear()
        return jsonify(ok=True)

    @app.get("/ai/event/<name>")
    @login_required
    def ai_event_thumb(name):
        try:
            p = ai.events.thumb_path(name)
        except ValueError:
            abort(400)
        if not os.path.exists(p):
            abort(404)
        return send_file(p, mimetype="image/jpeg", max_age=86400)

    @app.post("/api/ai/enroll")
    @login_required
    def api_ai_enroll():
        """Regista a cara maior do quadro atual com o nome dado (ou junta-a à pessoa pid)."""
        b = body()
        name, pid = (b.get("name") or "").strip(), b.get("pid")
        if not name and not pid:
            return jsonify(error="falta o nome"), 400
        if ai.missing():
            return jsonify(error="faltam modelos: corra setup/install-ai.sh"), 409
        cam.add_viewer()
        try:
            n, f = cam.wait_frame(cam.frame_n, timeout=5)   # um quadro novo, não o último guardado
        finally:
            cam.remove_viewer()
        if not f:
            return jsonify(error="sem imagem da câmara"), 503
        try:
            found = ai.request_embed(f)
        except (TimeoutError, RuntimeError, ConnectionError) as e:
            return jsonify(error=f"análise falhou: {e}"), 504
        if not found:
            return jsonify(error="não encontrei nenhuma cara na imagem"), 422
        face = max(found, key=lambda x: x["px"])
        if face["px"] < 60:
            return jsonify(error="a cara está muito pequena; aproxime-se da câmara"), 422
        try:
            r = faces.add_sample(name, face["emb"], base64.b64decode(face["crop"]), pid=pid)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        log.info("cara registada: %s (%d amostras)", r["name"], r["samples"])
        hub.emit("ai_state", ai.public_state())
        return jsonify(ok=True, person=r, faces=len(found), people=faces.list())

    @app.patch("/api/ai/people/<int:pid>")
    @login_required
    def api_ai_rename(pid):
        try:
            faces.rename(pid, body().get("name"))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        except KeyError:
            abort(404)
        return jsonify(ok=True, people=faces.list())

    @app.delete("/api/ai/people/<int:pid>")
    @login_required
    def api_ai_forget(pid):
        try:
            faces.delete(pid)
        except KeyError:
            abort(404)
        hub.emit("ai_state", ai.public_state())
        return jsonify(ok=True, people=faces.list())

    @app.delete("/api/ai/people/<int:pid>/<sample>")
    @login_required
    def api_ai_forget_sample(pid, sample):
        try:
            faces.delete_sample(pid, sample)
        except (KeyError, ValueError):
            abort(404)
        return jsonify(ok=True, people=faces.list())

    @app.get("/ai/face/<name>")
    @login_required
    def ai_face(name):
        try:
            p = faces.sample_path(name)
        except ValueError:
            abort(400)
        if not os.path.exists(p):
            abort(404)
        return send_file(p, mimetype="image/jpeg", max_age=86400)

    return app


def main():
    ap = argparse.ArgumentParser(description="CamDeck: câmera USB do uConsole no browser")
    ap.add_argument("--set-password", action="store_true", help="mudar a palavra-passe e sair")
    ap.add_argument("--port", type=int, help="porta HTTP (por omissão a da configuração, 8090)")
    ap.add_argument("--device", help="porta série do ESP (por omissão /dev/esp-cam)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    cfg = Config()

    if args.set_password:
        pw = getpass.getpass("Nova palavra-passe (mín. 8): ")
        if pw != getpass.getpass("Repita: "):
            print("não coincidem")
            return 1
        cfg.set_password(pw)
        print("palavra-passe alterada")
        return 0

    if args.device:
        cfg._data["device"] = args.device
    if cfg.generated_password:
        log.warning("palavra-passe inicial: %s  (também em %s; mude-a na página Dispositivo)",
                    cfg.generated_password, INITIAL_PASSWORD_PATH)

    hub = EventHub()
    media = Media(cfg)
    cam = CameraService(cfg, hub.emit)
    rec = Recorder(cfg, cam, media, hub.emit)
    rec.recover()
    faces = FaceDB()
    power = PowerMonitor(hub.emit)
    ai = AnalyzerService(cfg, cam, faces, hub.emit, power)

    def on_power(st, prev):
        # Bateria quase vazia: termina a gravação já, para o MP4 ficar completo antes de o uConsole se desligar
        if st["level"] == "critical" and prev.get("level") != "critical" and rec.active:
            log.warning("bateria quase vazia: a terminar a gravação")
            try:
                rec.stop()
            except RuntimeError:
                pass
    power.add_listener(on_power)
    power.start()
    cam.start()
    ai.start()

    # Estado da gravação (tempo decorrido) a cada segundo, enquanto grava
    def rec_ticker():
        while True:
            time.sleep(1)
            if rec.active:
                hub.emit("rec", rec.state())
    threading.Thread(target=rec_ticker, daemon=True).start()

    app = create_app(cfg, cam, rec, media, hub, ai, faces, power)

    def shutdown(*_):
        log.info("a terminar")
        if rec.active:
            rec.stop()
            time.sleep(0.5)
        ai.stop()
        cam.stop()
        os._exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    host, port = cfg.get("bind_host"), args.port or int(cfg.get("bind_port"))
    log.info("CamDeck %s em http://%s:%d (câmera em %s)", VERSION, host, port, cfg.get("device"))
    from werkzeug.serving import make_server
    srv = make_server(host, port, app, threaded=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main() or 0)
