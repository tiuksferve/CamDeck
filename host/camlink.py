"""
camlink.py — ligação USB com o firmware do CamDeck (XIAO ESP32-S3 Sense).

Protocolo descrito em firmware/main/link.h: pacotes binários do ESP com cabeçalho
de 20 bytes e CRC-32; comandos JSON do host, um por linha.

A classe CamLink corre uma thread de leitura que volta a abrir a porta sozinha
quando o ESP é desligado ou reinicia, e entrega cada pacote a callbacks.
"""

import json
import logging
import struct
import threading
import time
import zlib

import serial

log = logging.getLogger("camdeck.link")

MAGIC = b"CDK\xa5"
HDR = struct.Struct("<4sBBHIII")       # magic, type, flags, seq, len, ts, crc
MAX_LEN = 4 * 1024 * 1024

PKT_JPEG, PKT_SNAP, PKT_AUDIO, PKT_JSON, PKT_LOG, PKT_BENCH = 1, 2, 3, 4, 5, 6


class CamLink:
    def __init__(self, port: str, on_packet=None, on_state=None):
        """on_packet(type, payload, ts_ms, seq); on_state(connected: bool)."""
        self.port = port
        self.on_packet = on_packet or (lambda *a: None)
        self.on_state = on_state or (lambda c: None)
        self._ser = None
        self._wlock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.connected = False
        self.rx_bytes = 0
        self.crc_errors = 0
        self.lost = 0
        self._last_seq = None
        self._waiters = {}              # id -> [Event, resposta]
        self._next_id = 1
        self._id_lock = threading.Lock()

    # ── ciclo de vida ──────────────────────────────────────────────────────
    def start(self):
        self._thread = threading.Thread(target=self._run, name="camlink", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass

    def _open(self):
        s = serial.Serial()
        s.port = self.port
        s.baudrate = 115200             # ignorado pela USB-Serial-JTAG
        s.timeout = 0.2
        # DTR/RTS em baixo antes de abrir: com RTS ativo o ESP32-S3 reinicia
        s.dtr = False
        s.rts = False
        s.open()
        return s

    def _set_connected(self, c: bool):
        if c != self.connected:
            self.connected = c
            try:
                self.on_state(c)
            except Exception:
                log.exception("on_state")

    def _run(self):
        while not self._stop.is_set():
            try:
                self._ser = self._open()
            except (serial.SerialException, OSError) as e:
                log.debug("porta %s indisponível: %s", self.port, e)
                self._stop.wait(1.0)
                continue
            log.info("ligado a %s", self.port)
            self._last_seq = None
            self._set_connected(True)
            try:
                self._read_loop()
            except (serial.SerialException, OSError) as e:
                log.warning("ligação perdida: %s", e)
            finally:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None
                self._set_connected(False)
                for w in list(self._waiters.values()):
                    w[0].set()
            self._stop.wait(0.5)

    def _read_loop(self):
        buf = bytearray()
        ser = self._ser
        while not self._stop.is_set():
            chunk = ser.read(max(ser.in_waiting, 1))
            if not chunk:
                continue
            self.rx_bytes += len(chunk)
            buf += chunk
            while True:
                i = buf.find(MAGIC)
                if i < 0:
                    # guarda só o fim, que pode ser o começo de um magic
                    del buf[:max(len(buf) - 3, 0)]
                    break
                if i:
                    del buf[:i]
                if len(buf) < HDR.size:
                    break
                _, typ, _, seq, length, ts, crc = HDR.unpack_from(buf)
                if length > MAX_LEN or typ == 0 or typ > PKT_BENCH:
                    del buf[:1]
                    continue
                end = HDR.size + length
                if len(buf) < end:
                    # pacotes grandes: ler o resto de uma vez é bem mais rápido
                    need = end - len(buf)
                    more = ser.read(need)
                    self.rx_bytes += len(more)
                    buf += more
                    if len(buf) < end:
                        break
                payload = bytes(buf[HDR.size:end])
                if zlib.crc32(payload) != crc:
                    self.crc_errors += 1
                    del buf[:1]
                    continue
                del buf[:end]
                if self._last_seq is not None:
                    gap = (seq - self._last_seq - 1) & 0xFFFF
                    if gap and gap < 1000:
                        self.lost += gap
                self._last_seq = seq
                self._dispatch(typ, payload, ts, seq)

    def _dispatch(self, typ, payload, ts, seq):
        if typ == PKT_JSON:
            try:
                msg = json.loads(payload)
            except ValueError:
                log.warning("JSON inválido do ESP: %r", payload[:80])
                return
            w = self._waiters.get(msg.get("id"))
            if w:
                w[1] = msg
                w[0].set()
            payload = msg
        elif typ == PKT_LOG:
            log.info("esp: %s", payload.decode("utf-8", "replace"))
        try:
            self.on_packet(typ, payload, ts, seq)
        except Exception:
            log.exception("on_packet (tipo %d)", typ)

    # ── comandos ───────────────────────────────────────────────────────────
    def _new_id(self) -> int:
        with self._id_lock:
            cid = self._next_id
            self._next_id += 1
            return cid

    def send(self, cmd: str, _id: int = None, **kw) -> int:
        """Envia um comando sem esperar resposta; devolve o id."""
        cid = _id or self._new_id()
        msg = dict(cmd=cmd, id=cid, **kw)
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        ser = self._ser
        if not ser:
            raise ConnectionError("câmera desligada")
        with self._wlock:
            ser.write(data)
        return cid

    def request(self, cmd: str, timeout: float = 3.0, **kw) -> dict:
        """Envia um comando e espera a resposta JSON com o mesmo id."""
        cid = self._new_id()
        w = [threading.Event(), None]
        self._waiters[cid] = w
        try:
            self.send(cmd, _id=cid, **kw)
            if not w[0].wait(timeout) or w[1] is None:
                raise TimeoutError(f"sem resposta a {cmd}")
            return w[1]
        finally:
            self._waiters.pop(cid, None)


if __name__ == "__main__":
    # Teste rápido: python3 camlink.py /dev/esp-cam
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/esp-cam"
    stats = {"frames": 0, "bytes": 0}

    def on_packet(t, p, ts, seq):
        if t == PKT_JPEG:
            stats["frames"] += 1
            stats["bytes"] += len(p)
        elif t == PKT_JSON and p.get("ev") == "stat":
            print("stat", p)

    link = CamLink(port, on_packet)
    link.start()
    while not link.connected:
        time.sleep(0.1)
    time.sleep(0.5)
    hello = link.request("hello")
    print(json.dumps(hello, indent=1)[:1500])
    print("bench", link.request("bench", ms=3000, timeout=8))
    link.request("stream", on=True)
    for size in ("QVGA", "VGA", "SVGA", "HD", "UXGA"):
        link.request("set", key="framesize", val=size)
        time.sleep(1.0)
        stats.update(frames=0, bytes=0)
        t0 = time.time()
        time.sleep(4)
        dt = time.time() - t0
        print(f"{size:5s}: {stats['frames'] / dt:5.1f} fps, {stats['bytes'] / dt / 1024:6.0f} KB/s, "
              f"{stats['bytes'] / max(stats['frames'], 1) / 1024:5.1f} KB/quadro")
    link.request("stream", on=False)
    t0 = time.time()
    snaps = []
    link.on_packet = lambda t, p, ts, seq: snaps.append(p) if t == PKT_SNAP else None
    print("snap", link.request("snap", timeout=10), f"{time.time() - t0:.2f}s",
          [len(s) for s in snaps])
    if snaps:
        open("/dev/shm/camdeck-snap.jpg", "wb").write(snaps[0])
    print("crc_errors", link.crc_errors, "lost", link.lost)
    link.stop()
