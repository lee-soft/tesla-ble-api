#!/usr/bin/env python3
"""
Tesla BLE REST API — a tiny, dependency-free HTTP wrapper around
`tesla-control` (teslamotors/vehicle-command) that drives a Tesla over
Bluetooth Low Energy.

Why this exists
---------------
Modern Teslas (2021+ and all 2024 Highland/Juniper cars) require every command
to be *signed*; the old Owner API is gone. `tesla-control` speaks the signed
BLE protocol locally — no Tesla cloud, no billing, works offline. This server
exposes a handful of whitelisted verbs over a LAN-only HTTP endpoint with a
bearer token, so a smart-home trigger (a doorbell, a button, a cron job) can
"open the trunk" with a single HTTP call.

The important design choice
---------------------------
It keeps ONE long-lived `tesla-control` interactive session open instead of
spawning a process per command. Each fresh `tesla-control` run re-initialises
the BLE adapter (an HCI down/up transition in user-channel mode). On some
onboard Wi-Fi+BT combo chips (e.g. Broadcom AP6275P / BCM43752) that transition
*wedges the controller* after a few cycles — only a reboot recovers it. One
persistent session inits the adapter once and reuses the connection, which
sidesteps the churn entirely. See README.md for the full story and the
USB-dongle fix.

Security: bind to the LAN (or localhost) only, behind a bearer token read from
a file (never hard-coded). Only whitelisted verbs run.
"""
import json
import os
import subprocess
import threading
import time
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- config
HOME        = os.path.expanduser("~")
# tesla-control binary (built from teslamotors/vehicle-command, with the
# interactive-BLE patch from patches/ applied — see README).
TC          = os.environ.get("TESLA_CONTROL_BIN", os.path.join(HOME, "bin", "tesla-control"))
# Directory holding the enrolled key material + session cache.
TESLA_DIR   = os.environ.get("TESLA_DIR", os.path.join(HOME, "tesla"))
VIN         = os.environ.get("TESLA_VIN", "").strip().upper()
KEY_FILE    = os.environ.get("TESLA_KEY_FILE",   os.path.join(TESLA_DIR, "private_key.pem"))
CACHE_FILE  = os.environ.get("TESLA_CACHE_FILE", os.path.join(TESLA_DIR, "cache.json"))
TOKEN_FILE  = os.environ.get("TESLA_API_TOKEN_FILE", os.path.join(TESLA_DIR, "api_token"))
PORT        = int(os.environ.get("TESLA_API_PORT", "8710"))
BIND        = os.environ.get("TESLA_API_BIND", "0.0.0.0")
# hci0 = typical onboard adapter; hci1 = a USB BT dongle. A USB dongle handles
# the adapter down/up cycle cleanly and is strongly recommended on combo chips.
BT_ADAPTER  = os.environ.get("TESLA_BT_ADAPTER", "hci0")

CMD_TIMEOUT    = "20s"   # tesla-control's own per-command timeout
CONNECT_TIMEOUT= "25s"   # initial BLE connect timeout
PROMPT         = b"> "   # tesla-control interactive prompt
SPAWN_COOLDOWN = 3       # seconds to wait between respawn attempts

# name -> (tesla-control command string, needs_wake, description)
ACTIONS = {
    "trunk-open":       ("trunk-open",        True,  "Open/pop the trunk"),
    "trunk-close":      ("trunk-close",       True,  "Close the trunk (powered tailgate cars only)"),
    "frunk-open":       ("frunk-open",        True,  "Pop the frunk (front) - no remote close"),
    "honk":             ("honk",              True,  "Honk the horn"),
    "flash":            ("flash-lights",      True,  "Flash the headlights"),
    "flash-lights":     ("flash-lights",      True,  "Flash the headlights"),
    "windows-vent":     ("windows-vent",      True,  "Crack all windows open"),
    "windows-close":    ("windows-close",     True,  "Close all windows"),
    "charge-port-open": ("charge-port-open",  True,  "Open charge-port door"),
    "charge-port-close":("charge-port-close", True,  "Close charge-port door"),
    "climate-on":       ("climate-on",        True,  "Turn on climate"),
    "climate-off":      ("climate-off",       True,  "Turn off climate"),
    "media-toggle":     ("media-toggle-playback", True, "Play/pause media"),
    "media-volume-up":  ("media-volume-up",   True,  "Media volume up"),
    "media-volume-down":("media-volume-down", True,  "Media volume down"),
    "sentry-on":        ("sentry-mode on",    True,  "Sentry mode on"),
    "sentry-off":       ("sentry-mode off",   True,  "Sentry mode off"),
    "lock":             ("lock",              False, "Lock the car"),
    "unlock":           ("unlock",            False, "Unlock the car"),
    "wake":             ("wake",              False, "Wake the car"),
    "ping":             ("ping",              False, "Ping the car over BLE"),
}
COMBOS = {
    "scare":  ["flash-lights", "honk"],
    "treats": ["trunk-open"],
}

# classify shell output
FAIL_MARKERS   = ("Failed to execute command", "You must provide a private key")
ADAPTER_MARKER = "Error:"               # adapter/connect error at startup
MAYHAVE_MARKER = "Couldn't verify success"
# link is dead -> respawn the session and retry once
CONN_HINTS     = ("connection", "disconnected", "EOF", "broken pipe", "closed",
                  "context deadline", "no response", "transport", "not connected",
                  "failed to enable device", "can't init hci",
                  "listner timed out", "can't accept", "timed out")
SLEEP_HINTS    = ("asleep", "could not wake", "not awake", "is asleep")


class TeslaShell:
    """Owns one persistent `tesla-control` interactive subprocess."""

    def __init__(self):
        self.proc = None
        self.buf = bytearray()
        self.buf_lock = threading.Lock()
        self.cmd_lock = threading.RLock()     # one command through the pipe at a time
        self.reader = None
        self._last_spawn = 0.0
        self.connected = False
        self.last_error = ""

    # ---- lifecycle ----
    def _argv(self):
        return [TC, "-ble", "-vin", VIN, "-key-file", KEY_FILE,
                "-session-cache", CACHE_FILE,
                "-bt-adapter", BT_ADAPTER,
                "-command-timeout", CMD_TIMEOUT,
                "-connect-timeout", CONNECT_TIMEOUT]

    def _reader_loop(self, proc):
        try:
            while True:
                chunk = proc.stdout.read(1)
                if not chunk:
                    break
                with self.buf_lock:
                    self.buf.extend(chunk)
        except Exception:
            pass

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        """Spawn the subprocess and wait for the first prompt (= connected)."""
        with self.cmd_lock:
            if self.alive():
                return True
            dt = time.time() - self._last_spawn
            if dt < SPAWN_COOLDOWN:
                time.sleep(SPAWN_COOLDOWN - dt)
            self._last_spawn = time.time()
            self.connected = False
            with self.buf_lock:
                self.buf = bytearray()
            try:
                self.proc = subprocess.Popen(
                    self._argv(), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
            except Exception as e:
                self.last_error = "spawn failed: %s" % e
                self.proc = None
                return False
            self.reader = threading.Thread(target=self._reader_loop,
                                           args=(self.proc,), daemon=True)
            self.reader.start()
            # wait for first prompt OR process exit
            out = self._wait_prompt(timeout=40)
            if out is None:
                # no prompt: connect failed / process died
                captured = self._drain()
                self.last_error = captured.strip() or "no prompt within timeout"
                self._kill()
                return False
            self.connected = True
            self.last_error = ""
            return True

    def _kill(self):
        try:
            if self.proc:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass
        self.proc = None
        self.connected = False

    def stop(self):
        with self.cmd_lock:
            # try a clean exit first
            try:
                if self.alive():
                    self.proc.stdin.write(b"exit\n")
                    self.proc.stdin.flush()
                    self.proc.wait(timeout=5)
            except Exception:
                pass
            self._kill()

    # ---- buffer helpers ----
    def _drain(self):
        with self.buf_lock:
            s = bytes(self.buf).decode("utf-8", "replace")
            self.buf = bytearray()
        return s

    def _wait_prompt(self, timeout):
        """Wait until the buffer ends with the prompt. Returns the captured
        output (without the trailing prompt), or None on timeout/exit."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is None or self.proc.poll() is not None:
                # process exited
                time.sleep(0.05)
                return None
            with self.buf_lock:
                data = bytes(self.buf)
            if data.endswith(PROMPT):
                # small quiet settle to be sure nothing more is coming
                time.sleep(0.08)
                with self.buf_lock:
                    data2 = bytes(self.buf)
                    if data2.endswith(PROMPT):
                        out = data2[:-len(PROMPT)]
                        self.buf = bytearray()
                        return out.decode("utf-8", "replace")
            time.sleep(0.03)
        return None

    # ---- run one shell command ----
    def _send(self, line, timeout):
        with self.buf_lock:
            self.buf = bytearray()
        try:
            self.proc.stdin.write((line + "\n").encode())
            self.proc.stdin.flush()
        except Exception as e:
            return None, "write failed: %s" % e
        out = self._wait_prompt(timeout=timeout)
        if out is None:
            captured = self._drain()
            return None, (captured.strip() or "timeout waiting for prompt")
        return out, None

    def _classify(self, out):
        low = out.lower()
        if any(m.lower() in low for m in FAIL_MARKERS):
            return False, "car_error"
        if MAYHAVE_MARKER.lower() in low:
            return True, "may_have_succeeded"
        if out.strip().startswith("Error:") or "error:" in low[:12]:
            return False, "adapter_error"
        return True, "ok"

    def run(self, cmd_str, needs_wake, cmd_timeout=75):
        """Optimistic first attempt (instant when the car is already awake); on
        an asleep/transport failure, start a fresh session, wake infotainment
        if this command needs it, and retry once. No wake penalty when awake."""
        with self.cmd_lock:
            steps = []

            def attempt(line):
                if not self.alive():
                    if not self.start():
                        return None, "not connected: %s" % self.last_error
                return self._send(line, cmd_timeout)

            def retryable(text):
                t = (text or "").lower()
                return any(h in t for h in CONN_HINTS) or any(h in t for h in SLEEP_HINTS)

            # 1) optimistic attempt
            out, err = attempt(cmd_str)
            steps.append({"cmd": cmd_str, "err": err, "out": (out or "").strip()[:200]})

            # 2) if it failed like an asleep/dropped-link (incl. "may have
            #    succeeded" via context-deadline), reset + wake + retry once.
            if err is not None or (out is not None and retryable(out)):
                self._kill()
                self.start()
                if needs_wake:
                    self._send("wake", cmd_timeout)
                    time.sleep(4)            # let infotainment finish booting
                    steps.append({"cmd": "wake", "note": "woke before retry"})
                out, err = attempt(cmd_str)
                steps.append({"cmd": cmd_str + " (retry)", "err": err,
                             "out": (out or "").strip()[:200]})

            if err is not None:
                return {"ok": False, "reason": "transport", "error": err,
                        "output": "", "steps": steps}
            ok, reason = self._classify(out)
            return {"ok": ok, "reason": reason, "output": out.strip(), "steps": steps}

    def status(self):
        return {"alive": self.alive(), "connected": self.connected,
                "last_error": self.last_error}


SHELL = TeslaShell()

# ---- light pulse ("keep the drive lit") ---------------------------------
# There is no steady-headlight-on command on a Tesla, so "lights on" is
# emulated by flashing on a repeating interval.
PULSE_DEFAULT_SECONDS = 300
PULSE_DEFAULT_EVERY    = 4
PULSE_MAX_SECONDS      = 3600
_pulse_lock   = threading.Lock()
_pulse_thread = None
_pulse_stop   = threading.Event()
_pulse_info   = {"active": False}


def _pulse_run(seconds, every):
    deadline = time.time() + seconds
    flashes = 0
    while not _pulse_stop.is_set() and time.time() < deadline:
        SHELL.run("flash-lights", True)   # wake-capable so the first flash works on a sleeping car
        flashes += 1
        _pulse_info["flashes"] = flashes
        if _pulse_stop.wait(every):
            break
    _pulse_info["active"] = False
    _pulse_info["ended"] = time.strftime("%H:%M:%S")


def start_pulse(seconds, every):
    global _pulse_thread
    seconds = max(1, min(int(seconds), PULSE_MAX_SECONDS))
    every   = max(2, int(every))
    with _pulse_lock:
        stop_pulse()
        _pulse_stop.clear()
        _pulse_info.clear()
        _pulse_info.update({"active": True, "seconds": seconds, "every": every,
                            "flashes": 0, "started": time.strftime("%H:%M:%S")})
        _pulse_thread = threading.Thread(target=_pulse_run, args=(seconds, every),
                                         daemon=True)
        _pulse_thread.start()
    return dict(_pulse_info)


def stop_pulse():
    global _pulse_thread
    _pulse_stop.set()
    t = _pulse_thread
    if t and t.is_alive():
        t.join(timeout=10)
    _pulse_thread = None
    _pulse_info["active"] = False
    return dict(_pulse_info)


# ---- keep-awake (optional: prevents the sleep -> reconnect -> wedge chain) -
# On a flaky onboard combo chip the adapter wedges on the user-channel
# transition that happens when the session reconnects, and a reconnect only
# happens after the car sleeps and drops the BLE link. Firing `wake` over the
# LIVE session every KA_INTERVAL seconds (well inside the ~5-7 min sleep
# timeout) keeps the link up so there is never a reconnect, so the adapter
# never wedges. NOT needed with a USB dongle (it reconnects cleanly) — leave
# it off and let the car sleep normally. Off by default.
KA_INTERVAL      = int(os.environ.get("TESLA_KEEPALIVE_INTERVAL", "180"))
KA_AUTOSTART_MIN = int(os.environ.get("TESLA_KEEPALIVE_AUTOSTART_MIN", "0"))  # 0 = off
_ka_lock   = threading.Lock()
_ka_thread = None
_ka_stop   = threading.Event()
_ka_info   = {"active": False}


def _ka_run(until_ts):
    wakes = 0
    while not _ka_stop.is_set():
        if until_ts and time.time() >= until_ts:
            break
        r = SHELL.run("wake", False)
        wakes += 1
        _ka_info.update({"wakes": wakes, "last_ok": bool(r.get("ok")),
                        "last": time.strftime("%H:%M:%S")})
        if _ka_stop.wait(KA_INTERVAL):
            break
    _ka_info["active"] = False
    _ka_info["ended"] = time.strftime("%H:%M:%S")


def start_keepalive(minutes=None):
    global _ka_thread
    with _ka_lock:
        stop_keepalive()
        _ka_stop.clear()
        until_ts = (time.time() + minutes * 60) if minutes else None
        _ka_info.clear()
        _ka_info.update({"active": True, "interval": KA_INTERVAL,
                        "until": (time.strftime("%H:%M:%S", time.localtime(until_ts))
                                  if until_ts else "indefinite"),
                        "wakes": 0, "started": time.strftime("%H:%M:%S")})
        _ka_thread = threading.Thread(target=_ka_run, args=(until_ts,), daemon=True)
        _ka_thread.start()
    return dict(_ka_info)


def stop_keepalive():
    global _ka_thread
    _ka_stop.set()
    t = _ka_thread
    if t and t.is_alive():
        t.join(timeout=2)   # daemon thread exits after its current wake finishes
    _ka_thread = None
    _ka_info["active"] = False
    return dict(_ka_info)


# ---------------------------------------------------------------- HTTP
def _load_token():
    try:
        with open(TOKEN_FILE) as fh:
            return fh.read().strip() or None
    except OSError:
        return None

API_TOKEN = _load_token()


class Handler(BaseHTTPRequestHandler):
    server_version = "TeslaBLE/2.0"

    def _send(self, code, payload):
        body = json.dumps(payload, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not API_TOKEN:
            return False
        supplied = None
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[7:].strip()
        if not supplied:
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            if "token" in q:
                supplied = q["token"][0]
        return bool(supplied) and hmac.compare_digest(supplied, API_TOKEN)

    def _path(self):
        from urllib.parse import urlparse
        return urlparse(self.path).path.strip("/").lower()

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.client_address[0], fmt % args), flush=True)

    def do_GET(self):
        p = self._path()
        if p in ("", "health", "healthz"):
            return self._send(200, {
                "ok": True, "service": "tesla-ble-api", "version": 2, "vin": VIN,
                "token_configured": bool(API_TOKEN),
                "session": SHELL.status(),
                "actions": sorted(ACTIONS.keys()),
                "combos": sorted(COMBOS.keys()),
                "lights": ["lights-on", "lights-pulse", "lights-off"],
                "prepare": ["warm"],
                "pulse": dict(_pulse_info),
                "keepalive": dict(_ka_info),
            })
        if p == "actions":
            return self._send(200, {
                "actions": {k: v[2] for k, v in ACTIONS.items()},
                "combos": COMBOS,
            })
        if p in ("state", "status"):
            if not self._authed():
                return self._send(401, {"ok": False, "error": "unauthorized"})
            r = SHELL.run("body-controller-state", False)
            return self._send(200 if r["ok"] else 502,
                             {"ok": r["ok"], "state": r["output"],
                              "reason": r["reason"]})
        return self._send(404, {"ok": False, "error": "not found",
                               "hint": "GET /health or /actions; POST /<action>"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"ok": False, "error": "unauthorized"})
        p = self._path()

        if p == "warm":
            # Pre-wake the car + warm the BLE session so a following burst of
            # commands fires instantly (no first-command auto-wake delay).
            t0 = time.time()
            rw = SHELL.run("wake", False)
            time.sleep(3)                       # let infotainment finish booting
            rs = SHELL.run("body-controller-state", False)
            st = rs.get("output", "") or ""
            awake = "VEHICLE_SLEEP_STATUS_AWAKE" in st
            return self._send(200, {"ok": bool(rw["ok"]), "action": "warm",
                                   "awake": awake,
                                   "elapsed_s": round(time.time() - t0, 1),
                                   "state": st,
                                   "note": "car woken + session warm; fire commands now"})

        if p in ("lights-on", "lights-pulse"):
            body = self._read_json() or {}
            info = start_pulse(body.get("seconds", PULSE_DEFAULT_SECONDS),
                               body.get("every", PULSE_DEFAULT_EVERY))
            return self._send(200, {"ok": True, "action": "lights-on",
                                   "note": "flashing every %ss for %ss (no steady-on verb exists)"
                                           % (info["every"], info["seconds"]),
                                   "pulse": info})
        if p in ("lights-off", "lights-stop"):
            return self._send(200, {"ok": True, "action": "lights-off",
                                   "pulse": stop_pulse()})

        if p in ("keepalive-on", "keepalive"):
            body = self._read_json() or {}
            minutes = body.get("minutes")
            info = start_keepalive(int(minutes) if minutes else None)
            return self._send(200, {"ok": True, "action": "keepalive-on",
                                   "note": "firing wake every %ss to stop the car sleeping (prevents the BLE wedge)" % KA_INTERVAL,
                                   "keepalive": info})
        if p in ("keepalive-off", "keepalive-stop"):
            return self._send(200, {"ok": True, "action": "keepalive-off",
                                   "keepalive": stop_keepalive()})

        if p in COMBOS:
            steps = self._read_steps_override() or COMBOS[p]
            results, allok = [], True
            for name in steps:
                spec = ACTIONS.get(name)
                if not spec:
                    results.append({"action": name, "ok": False, "error": "unknown action"})
                    allok = False
                    continue
                r = SHELL.run(spec[0], spec[1]); r["action"] = name
                results.append(r); allok = allok and r["ok"]
            return self._send(200 if allok else 502,
                             {"ok": allok, "combo": p, "results": results})

        spec = ACTIONS.get(p)
        if not spec:
            return self._send(404, {"ok": False, "error": "unknown action: %s" % p,
                                   "actions": sorted(ACTIONS.keys())})
        r = SHELL.run(spec[0], spec[1]); r["action"] = p
        return self._send(200 if r["ok"] else 502, r)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0:
                return None
            data = json.loads(self.rfile.read(n).decode())
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _read_steps_override(self):
        data = self._read_json() or {}
        steps = data.get("steps")
        if isinstance(steps, list) and all(isinstance(s, str) for s in steps):
            return steps
        return None


def main():
    if not VIN:
        raise SystemExit("ERROR: set TESLA_VIN to your car's VIN (17 chars).")
    if not API_TOKEN:
        print("WARNING: no API token at %s - rejecting all requests. "
              "Create one, e.g.: openssl rand -hex 24 > %s && chmod 600 %s"
              % (TOKEN_FILE, TOKEN_FILE, TOKEN_FILE), flush=True)
    print("tesla-ble-api v2 (persistent session) on %s:%d vin=%s adapter=%s"
          % (BIND, PORT, VIN, BT_ADAPTER), flush=True)
    if KA_AUTOSTART_MIN > 0:
        print("keep-awake autostart: %d min (wake every %ds)" % (KA_AUTOSTART_MIN, KA_INTERVAL), flush=True)
        start_keepalive(KA_AUTOSTART_MIN)
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SHELL.stop()


if __name__ == "__main__":
    main()
