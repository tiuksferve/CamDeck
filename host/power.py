"""
power.py: energia e temperatura do uConsole, para o CamDeck não o sobrecarregar.

Lê o chip de energia AXP228 (/sys/class/power_supply), a temperatura do CPU e o estado do
firmware do Pi (vcgencmd get_throttled: subtensão, limitação). Daqui sai um nível:

  ok        na corrente, ou bateria com carga suficiente
  low       na bateria (sem corrente há 10 s) com ≤ 20 %: pede para ligar à corrente; a análise pára
  critical  na bateria com ≤ 10 % ou tensão média ≤ 3,30 V: a análise pára e uma gravação em curso é
            terminada (para o MP4 ficar bom)

Os níveis têm histerese (só saem quando a carga sobe 3 %) e o aviso no ambiente de trabalho não se
repete mais do que uma vez a cada 15 min.
  fault     o chip de energia não responde ou há subtensão: a análise pára

Os avisos vão para os browsers (SSE "power") e para o ambiente de trabalho do uConsole
(notificação), para quem está ao ecrã sem o CamDeck aberto.
"""

import logging
import os
import subprocess
import threading
import time
from collections import deque

log = logging.getLogger("camdeck.power")

PS = "/sys/class/power_supply"
THERMAL = "/sys/class/thermal/thermal_zone0/temp"

LOW_PCT, CRIT_PCT = 20, 10              # carga da bateria (%), lida do medidor do AXP228
CRIT_V = 3.30                           # tensão média (30 s) abaixo da qual o AXP está perto de desligar
CLEAR_PCT = 3                           # histerese: para sair de um nível, a carga tem de subir isto
BATTERY_POLLS = 2                       # leituras seguidas sem corrente para contar como "na bateria"
NOTIFY_REPEAT_S = 15 * 60               # o mesmo aviso no ambiente de trabalho no máximo a cada 15 min
# A tensão sob carga cai muito (3,45 V com 49 % medidos a 22/11): só serve de travão final, não de aviso.
WARM_C, HOT_C, COOL_C = 72.0, 80.0, 70.0


def _read(path):
    with open(path) as f:
        return f.read().strip()


class PowerMonitor:
    def __init__(self, emit=None, interval=5.0):
        self.emit = emit or (lambda ev, data: None)
        self.interval = interval
        self.state = {"available": False, "level": "ok"}
        self._listeners = []            # fn(state, previous_state)
        self._hot = False
        self._notified = {}             # nível -> hora do último aviso no ambiente de trabalho
        self._volts = deque(maxlen=6)   # média de 30 s: uma queda momentânea sob carga não dispara
        self._bat_level = "ok"          # nível da bateria com histerese (ok/low/critical)
        self._no_ac = 0                 # leituras seguidas sem corrente
        self._stop = threading.Event()
        self._bat = self._ac = None
        for name in sorted(os.listdir(PS)) if os.path.isdir(PS) else []:
            try:
                typ = _read(os.path.join(PS, name, "type"))
            except OSError:
                continue
            if typ == "Battery" and not self._bat:
                self._bat = os.path.join(PS, name)
            elif typ in ("Mains", "USB") and not self._ac:
                self._ac = os.path.join(PS, name)

    def start(self):
        self.poll()
        threading.Thread(target=self._loop, name="power", daemon=True).start()

    def stop(self):
        self._stop.set()

    def add_listener(self, fn):
        self._listeners.append(fn)

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self.poll()
            except Exception as e:      # noqa: BLE001
                log.warning("energia: %s", e)

    # ── leitura ────────────────────────────────────────────────────────────
    def _battery(self):
        """Devolve (dict, pmic_ok)."""
        if not self._bat:
            return {}, True
        b, ok = {}, True
        for key, conv in (("status", str), ("capacity", int), ("voltage_now", int), ("current_now", int)):
            try:
                b[key] = conv(_read(os.path.join(self._bat, key)))
            except OSError:
                ok = False              # ENXIO: o chip não responde no I2C
            except ValueError:
                pass
        return b, ok

    @staticmethod
    def _throttled():
        try:
            out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2).stdout
            return int(out.strip().split("=")[1], 16)
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return None

    def poll(self):
        bat, pmic_ok = self._battery()
        ac = None
        if self._ac:
            try:
                ac = _read(os.path.join(self._ac, "online")) == "1"
            except OSError:
                pmic_ok = False
        try:
            temp = int(_read(THERMAL)) / 1000
        except (OSError, ValueError):
            temp = None
        thr = self._throttled()

        v_now = bat.get("voltage_now", 0) / 1e6 or None
        if v_now:
            self._volts.append(v_now)
        volts = sum(self._volts) / len(self._volts) if v_now and self._volts else v_now
        amps = bat.get("current_now", 0) / 1e6 if "current_now" in bat else None
        no_ac = ac is False or (ac is None and bat.get("status") == "Discharging")
        self._no_ac = self._no_ac + 1 if no_ac else 0
        on_battery = self._no_ac >= BATTERY_POLLS
        pct = bat.get("capacity")
        undervolt = bool(thr is not None and thr & 0x1)

        if temp is not None:
            if temp >= HOT_C:
                self._hot = True
            elif temp <= COOL_C:
                self._hot = False

        level, reason = "ok", None
        charge = f"{pct} %" if pct is not None else ""
        if volts:
            charge += (", " if charge else "") + f"{volts:.2f} V"
        if not pmic_ok and self._bat:
            level, reason = "fault", "o chip de energia não responde"
        elif undervolt:
            level, reason = "fault", "subtensão na alimentação"
        else:
            # Nível da bateria com histerese: sobe logo, só desce com margem (ou com corrente)
            crit = (pct is not None and pct <= CRIT_PCT) or (volts is not None and len(self._volts) >= 3 and volts <= CRIT_V)
            low = pct is not None and pct <= LOW_PCT
            cur = self._bat_level
            if not on_battery:
                cur = "ok"
            elif crit:
                cur = "critical"
            elif cur == "critical" and pct is not None and pct >= CRIT_PCT + CLEAR_PCT:
                cur = "low" if low else "ok"
            elif low and cur == "ok":
                cur = "low"
            elif cur == "low" and pct is not None and pct >= LOW_PCT + CLEAR_PCT:
                cur = "ok"
            self._bat_level = cur
            if cur == "critical":
                level, reason = "critical", f"bateria quase vazia ({charge})"
            elif cur == "low":
                level, reason = "low", f"bateria fraca ({charge})"

        st = {
            "available": bool(self._bat or self._ac),
            "source": "bateria" if on_battery else "corrente",
            "on_battery": on_battery,
            "pmic_ok": pmic_ok,
            "battery_pct": pct,
            "battery_status": bat.get("status"),
            "battery_v": round(volts, 2) if volts else None,
            "battery_a": round(amps, 2) if amps is not None else None,
            "battery_w": round(volts * amps, 1) if volts and amps is not None else None,
            "temp": round(temp, 1) if temp is not None else None,
            "warm": temp is not None and temp >= WARM_C,
            "hot": self._hot,
            "undervolt": undervolt,
            "throttled": bool(thr is not None and thr & 0x4),
            "level": level,
            "reason": reason,
        }
        prev, self.state = self.state, st
        for fn in list(self._listeners):
            try:
                fn(st, prev)
            except Exception as e:      # noqa: BLE001
                log.warning("energia (ouvinte): %s", e)
        if st["level"] != prev.get("level") or st["source"] != prev.get("source"):
            log.info("energia: %s, %s%s", st["source"], st["level"], f" ({reason})" if reason else "")
            self._desktop_notify(st)
        self.emit("power", st)
        return st

    # ── aviso no ambiente de trabalho ──────────────────────────────────────
    def _desktop_notify(self, st):
        lv = st["level"]
        if lv == "ok":
            return
        now = time.time()
        if now - self._notified.get(lv, 0) < NOTIFY_REPEAT_S:
            return
        self._notified[lv] = now
        if st["level"] == "fault":
            title, body = "CamDeck: problema de energia", \
                f"{st['reason'].capitalize()}. A análise de imagem foi pausada. Ligue o uConsole à corrente."
        elif st["level"] == "critical":
            title, body = "CamDeck: bateria quase vazia", \
                f"Sem corrente: o uConsole está nas baterias, a {st['battery_pct']} %. Verifique o carregador ou o powerbank já. A análise parou e a gravação foi terminada."
        else:
            title, body = "CamDeck: bateria fraca", \
                f"Sem corrente: o uConsole está nas baterias, a {st['battery_pct']} %. Verifique o carregador ou o powerbank. A análise de imagem está em pausa."
        self.notify(title, body, sticky=st["level"] != "low")

    def notify(self, title, body, sticky=False):
        """Notificação no ambiente de trabalho do uConsole (também usada pela análise)."""
        try:
            subprocess.Popen(
                ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
                 "--object-path", "/org/freedesktop/Notifications", "--method", "org.freedesktop.Notifications.Notify",
                 "CamDeck", "0", "battery-caution", title, body, "[]",
                 "{'urgency': <byte 2>}", "0" if sticky else "15000"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            log.warning("notificação: %s", e)
