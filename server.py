#!/usr/bin/env python3
"""Leitstand: Ollama, ComfyUI und die GPU dieses Rechners im Browser.

Nur Standardbibliothek. Liest GPU-Werte ueber nvidia-smi, fragt die APIs von
Ollama und ComfyUI ab und startet/stoppt deren systemd-Dienste ueber
systemctl (erlaubt per polkit-Regel, siehe install.sh).
"""

import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import chat
import comfyservers

HOST = os.environ.get("LEITSTAND_HOST", "0.0.0.0")
PORT = int(os.environ.get("LEITSTAND_PORT", "8100"))
OLLAMA = os.environ.get("LEITSTAND_OLLAMA", "http://127.0.0.1:11434")
COMFY = os.environ.get("LEITSTAND_COMFY", "http://127.0.0.1:8188")   # der eigene; weitere: comfyservers.py
COMFY_DIR = os.environ.get("LEITSTAND_COMFY_DIR", str(Path.home() / "Documents/devel/ComfyUI"))
STATIC = Path(__file__).resolve().parent / "static"

SERVICES = {"ollama": "ollama.service", "comfy": "comfyui.service"}
VERBS = {"start", "stop", "restart"}
HISTORY_SECONDS = 600
MODEL_EXT = (".safetensors", ".sft", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".onnx")

STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
}


def run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)


def http_json(url, body=None, timeout=1.5):
    """GET (oder POST mit body) und JSON zurueck; None wenn nicht erreichbar."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else {}


def try_json(url, timeout=1.5):
    try:
        return http_json(url, timeout=timeout)
    except (OSError, ValueError, urllib.error.URLError):
        return None


# --- GPU / System ----------------------------------------------------------

GPU_QUERY = ["name", "memory.total", "memory.used", "utilization.gpu", "temperature.gpu",
             "power.draw", "power.limit", "fan.speed", "clocks.gr", "clocks.max.gr", "clocks.mem"]


def num(s):
    try:
        return float(s)
    except ValueError:
        return None


def owner_of(pid):
    """Ordnet einen GPU-Prozess Ollama, ComfyUI oder 'sonst' zu."""
    try:
        cg = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return "sonst", ""
    if "ollama.service" in cg:
        return "ollama", "ollama"
    if "comfyui.service" in cg:
        return "comfy", "ComfyUI"
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        cmd = ""
    if "ollama" in cmd:
        return "ollama", "ollama"
    if "main.py" in cmd and ("ComfyUI" in cmd or os.path.realpath(f"/proc/{pid}/cwd") == COMFY_DIR):
        return "comfy", "ComfyUI"
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        comm = str(pid)
    return "sonst", comm


class Sampler(threading.Thread):
    """Sammelt jede Sekunde GPU-, CPU- und RAM-Werte in einen Ringpuffer."""

    def __init__(self):
        super().__init__(daemon=True)
        self.samples = deque(maxlen=HISTORY_SECONDS)
        self.gpu_name = None
        self.processes = []
        self.lock = threading.Lock()
        self._cpu_prev = None

    def cpu_percent(self):
        try:
            parts = Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]
        except OSError:
            return None
        vals = list(map(int, parts))
        idle, total = vals[3] + vals[4], sum(vals)
        prev, self._cpu_prev = self._cpu_prev, (idle, total)
        if prev is None or total == prev[1]:
            return None
        return round(100.0 * (1 - (idle - prev[0]) / (total - prev[1])), 1)

    @staticmethod
    def memory():
        info = {}
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        except (OSError, ValueError):
            return None, None
        total = info.get("MemTotal")
        return (total - info.get("MemAvailable", 0)) if total else None, total

    def sample(self):
        s = {"t": round(time.time(), 1), "cpu": self.cpu_percent()}
        s["ram"], s["ram_total"] = self.memory()
        code, out, _ = run(["nvidia-smi", "--query-gpu=" + ",".join(GPU_QUERY),
                            "--format=csv,noheader,nounits"], timeout=5)
        if code == 0 and out.strip():
            f = [x.strip() for x in out.strip().splitlines()[0].split(",")]
            self.gpu_name = f[0]
            s.update(mem_total=num(f[1]), mem=num(f[2]), util=num(f[3]), temp=num(f[4]),
                     power=num(f[5]), power_limit=num(f[6]), fan=num(f[7]),
                     clock=num(f[8]), clock_max=num(f[9]), clock_mem=num(f[10]))
        procs = []
        code, out, _ = run(["nvidia-smi", "--query-compute-apps=pid,used_memory",
                            "--format=csv,noheader,nounits"], timeout=5)
        if code == 0:
            for line in out.strip().splitlines():
                pid, _, mem = (x.strip() for x in line.partition(","))
                if pid.isdigit():
                    who, label = owner_of(pid)
                    procs.append({"pid": int(pid), "owner": who, "name": label, "mem": num(mem)})
        by = {"ollama": 0.0, "comfy": 0.0}
        for p in procs:
            if p["owner"] in by and p["mem"]:
                by[p["owner"]] += p["mem"]
        s["vram_ollama"], s["vram_comfy"] = by["ollama"], by["comfy"]
        with self.lock:
            self.samples.append(s)
            self.processes = procs

    def run(self):
        while True:
            start = time.monotonic()
            try:
                self.sample()
            except Exception as e:  # der Sampler darf nie sterben
                print("sampler:", e, flush=True)
            time.sleep(max(0.1, 1.0 - (time.monotonic() - start)))

    def since(self, t):
        with self.lock:
            return [s for s in self.samples if s["t"] > t], list(self.processes)


SAMPLER = Sampler()


# --- Dienste ---------------------------------------------------------------

def unit_status(unit):
    props = "LoadState,ActiveState,SubState,UnitFileState,MainPID,StateChangeTimestampMonotonic,Result"
    code, out, err = run(["systemctl", "show", unit, "--no-pager", "-p", props])
    st = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    since = None
    mono = st.get("StateChangeTimestampMonotonic", "0")
    if mono.isdigit() and int(mono) > 0:
        since = round(time.time() - (time.monotonic() - int(mono) / 1e6))
    return {
        "unit": unit,
        "exists": st.get("LoadState") == "loaded",
        "active": st.get("ActiveState", "unknown"),
        "sub": st.get("SubState", ""),
        "enabled": st.get("UnitFileState", ""),
        "pid": int(st["MainPID"]) if st.get("MainPID", "0").isdigit() and st["MainPID"] != "0" else None,
        "result": st.get("Result", ""),
        "since": since,
    }


def journal(unit, n):
    code, out, err = run(["journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "json"], timeout=10)
    lines = []
    for raw in out.splitlines():
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        msg = e.get("MESSAGE", "")
        if isinstance(msg, list):
            msg = bytes(msg).decode(errors="replace")
        t = int(e.get("__REALTIME_TIMESTAMP", "0")) / 1e6
        lines.append({"t": t, "msg": msg})
    return {"lines": lines, "error": err.strip() if code else ""}


# --- Ollama ----------------------------------------------------------------

def ollama_state():
    st = {"service": unit_status(SERVICES["ollama"]), "url": OLLAMA}
    ver = try_json(OLLAMA + "/api/version", timeout=1)
    st["up"] = ver is not None
    st["version"] = (ver or {}).get("version")
    st["running"], st["installed"] = [], []
    if st["up"]:
        ps = try_json(OLLAMA + "/api/ps") or {}
        for m in ps.get("models", []):
            d = m.get("details") or {}
            st["running"].append({
                "name": m.get("name"), "size": m.get("size"), "vram": m.get("size_vram"),
                "expires": m.get("expires_at"), "context": m.get("context_length"),
                "params": d.get("parameter_size"), "quant": d.get("quantization_level"),
            })
        tags = try_json(OLLAMA + "/api/tags") or {}
        for m in tags.get("models", []):
            d = m.get("details") or {}
            st["installed"].append({
                "name": m.get("name"), "size": m.get("size"), "modified": m.get("modified_at"),
                "params": d.get("parameter_size"), "quant": d.get("quantization_level"),
                "family": d.get("family"),
            })
        st["installed"].sort(key=lambda m: m["name"] or "")
    return st


def ollama_keep_alive(model, keep_alive):
    body = {"model": model}
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    try:
        http_json(OLLAMA + "/api/generate", body, timeout=600)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except ValueError:
            pass
        return False, detail
    except (OSError, urllib.error.URLError) as e:
        return False, str(e)
    return True, ""


# --- ComfyUI ---------------------------------------------------------------

def workflow_models(prompt):
    """Modelldateien, die ein Workflow ueber seine Loader-Knoten anfordert."""
    found, seen = [], set()
    for node in (prompt or {}).values():
        if not isinstance(node, dict):
            continue
        for key, val in (node.get("inputs") or {}).items():
            if isinstance(val, str) and val.lower().endswith(MODEL_EXT) and (key, val) not in seen:
                seen.add((key, val))
                found.append({"input": key, "file": val, "node": node.get("class_type")})
    return found


SERVERS = comfyservers.Servers(COMFY)
_comfy_quiet_until = {}   # entfernter Server antwortet nicht: nicht bei jedem Abruf eine Sekunde warten


def comfy_state():
    srv = SERVERS.active()
    url = srv["url"]
    st = {"service": unit_status(SERVICES["comfy"]), "url": url, "server": srv, "servers": SERVERS.list()}
    if not srv["local"] and time.monotonic() < _comfy_quiet_until.get(url, 0):
        stats = None
    else:
        stats = try_json(url + "/system_stats", timeout=1)
        if stats is None and not srv["local"]:
            _comfy_quiet_until[url] = time.monotonic() + 8
    st["up"] = stats is not None
    st["version"] = st["queue"] = st["loaded"] = st["workflow"] = None
    st["devices"] = []
    if not st["up"]:
        return st
    st["version"] = stats.get("system", {}).get("comfyui_version")
    st["argv"] = stats.get("system", {}).get("argv")
    st["devices"] = [{"name": d.get("name"), "vram_total": d.get("vram_total"), "vram_free": d.get("vram_free"),
                      "torch_total": d.get("torch_vram_total"), "torch_free": d.get("torch_vram_free")}
                     for d in stats.get("devices", [])]
    q = try_json(url + "/queue") or {}
    running, pending = q.get("queue_running", []), q.get("queue_pending", [])
    st["queue"] = {"running": len(running), "pending": len(pending)}
    st["loaded"] = try_json(url + "/leitstand/models")  # None ohne den Custom Node
    source = None
    if running and len(running[0]) > 2:
        source = {"state": "läuft", "prompt": running[0][2]}
    else:
        hist = try_json(url + "/history?max_items=1") or {}
        for entry in hist.values():
            p = entry.get("prompt") or []
            status = (entry.get("status") or {}).get("status_str")
            if len(p) > 2:
                source = {"state": "fertig" if status == "success" else (status or "?"), "prompt": p[2]}
    if source:
        st["workflow"] = {"state": source["state"], "models": workflow_models(source["prompt"])}
    return st


def comfy_free():
    try:
        http_json(SERVERS.active()["url"] + "/free", {"unload_models": True, "free_memory": True}, timeout=30)
    except (OSError, ValueError, urllib.error.URLError) as e:
        return False, str(e)
    return True, ""


# --- HTTP ------------------------------------------------------------------

def service_action(key, verb):
    code, out, err = run(["systemctl", "--no-ask-password", verb, SERVICES[key]], timeout=120)
    return code == 0, (err or out).strip()


class Handler(BaseHTTPRequestHandler):
    server_version = "Leitstand/1"

    def log_message(self, fmt, *args):
        pass  # das Polling wuerde sonst das Journal fuellen

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if chat.handle(self, OLLAMA, SERVERS.active()):   # /chat/: der Chat mit Ollama (chat.py)
            return
        url = urlparse(self.path)
        if url.path in STATIC_FILES:
            name, ctype = STATIC_FILES[url.path]
            try:
                body = (STATIC / name).read_bytes()
            except OSError:
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        q = parse_qs(url.query)
        if url.path == "/api/state":
            since = float(q.get("since", ["0"])[0] or 0)
            samples, procs = SAMPLER.since(since)
            return self.send_json({
                "host": socket.gethostname(), "time": time.time(),
                "gpu": {"name": SAMPLER.gpu_name, "samples": samples, "processes": procs},
                "ollama": ollama_state(), "comfy": comfy_state(),
            })
        if url.path == "/api/comfy/servers":
            return self.send_json({"servers": SERVERS.list(), "active": SERVERS.active()["id"]})
        m = re.fullmatch(r"/api/logs/(\w+)", url.path)
        if m and m.group(1) in SERVICES:
            n = max(10, min(1000, int(q.get("n", ["150"])[0])))
            return self.send_json(journal(SERVICES[m.group(1)], n))
        self.send_error(404)

    def do_POST(self):
        if chat.handle(self, OLLAMA, SERVERS.active()):   # prueft den Header selbst, liest den Body selbst
            return
        # Eigener Header erzwingt bei fremden Webseiten einen CORS-Preflight,
        # den wir nie beantworten: kein Start/Stopp per untergeschobenem Formular.
        if self.headers.get("X-Leitstand") != "1":
            return self.send_json({"ok": False, "error": "X-Leitstand-Header fehlt"}, 403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except ValueError:
            return self.send_json({"ok": False, "error": "kein gültiges JSON"}, 400)
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/service/(\w+)/(\w+)", path)
        if m and m.group(1) in SERVICES and m.group(2) in VERBS:
            ok, err = service_action(m.group(1), m.group(2))
        elif path in ("/api/ollama/load", "/api/ollama/unload") and isinstance(body.get("model"), str):
            ok, err = ollama_keep_alive(body["model"], 0 if path.endswith("unload") else None)
        elif path == "/api/comfy/free":
            ok, err = comfy_free()
        elif path == "/api/comfy/servers":
            return self.servers_action(lambda: SERVERS.add(body.get("name"), body.get("url")))
        elif m := re.fullmatch(r"/api/comfy/servers/(\w+)/(select|delete)", path):
            return self.servers_action(lambda: (SERVERS.select if m[2] == "select" else SERVERS.remove)(m[1]))
        else:
            return self.send_json({"ok": False, "error": "unbekannte Aktion"}, 404)
        self.send_json({"ok": ok, "error": err}, 200 if ok else 500)

    def servers_action(self, fn):
        """Auswahl, Hinzufuegen, Entfernen eines ComfyUI-Servers: die Liste danach zurueck."""
        try:
            added = fn()
        except ValueError as e:
            return self.send_json({"ok": False, "error": str(e)}, 400)
        except OSError as e:
            return self.send_json({"ok": False, "error": f"Einstellungen nicht speicherbar: {e}"}, 500)
        out = {"ok": True, "error": "", "servers": SERVERS.list(), "active": SERVERS.active()["id"]}
        if isinstance(added, dict):
            out["added"] = added
            out["reachable"] = try_json(added["url"] + "/system_stats", timeout=3) is not None
        self.send_json(out)

    # Chats speichern und loeschen; sonst gibt es kein PUT und DELETE.
    def do_PUT(self):
        if not chat.handle(self, OLLAMA, SERVERS.active()):
            self.send_error(405)

    do_DELETE = do_PUT


def main():
    SAMPLER.start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    print(f"Leitstand auf http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
