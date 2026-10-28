"""Bilder und Videos fuer den Chat: ein Workflow aus workflows/ an ComfyUI, das Ergebnis zurueck.

Ein Workflow ist ComfyUIs API-Format ("Export (API)") mit einem kleinen Kopf
darum: welcher Knoten den Prompt, die Groesse, den Seed, die Schritte und (bei
Videos) die Laenge bekommt, und mit welchen Werten er sonst laeuft (siehe
workflows/*.json). "kind": "video" macht ein Video daraus (SaveVideo, .mp4).

Der Ablauf meldet sich als Folge von Zustaenden, die chat.py als NDJSON an die
Seite streamt: ComfyUI startet, Ollama gibt den Grafikspeicher frei, wartet in
der Schlange, rechnet (mit Schritt x von y, den ComfyUI ueber seinen Websocket
meldet), fertig (mit dem Bild oder Video) -- oder ein Fehler. Geht die Seite
weg (Stopp, Tab zu), bricht ComfyUI den Auftrag ab.

Grafikspeicher: Ollamas grosse Modelle und ComfyUIs Bildmodell passen nicht
zusammen auf die Karte. Mit `free_vram` entlaedt ein Bild vorher die
Ollama-Modelle, und ComfyUI gibt seins erst wieder her, wenn der Chat das
naechste Mal Ollama fragt (release_for_chat) -- mehrere Bilder hintereinander
laden das Bildmodell so nur einmal. Ein Workflow mit "card": "exclusive"
(MiniMax H3: fast 24 GB) raeumt die Karte immer, auch ohne `free_vram`. Nur fuer
den eigenen Server: ein entfernter rechnet auf einer anderen Karte, und starten
kann der Leitstand ihn auch nicht.
"""

import base64
import json
import os
import random
import re
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent / "workflows"
WORKFLOW_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
MEDIA_NAME = re.compile(r"[0-9a-f]{32}\.(png|mp4)")
START_TIMEOUT = 120      # ComfyUI braucht beim ersten Start eine Weile
RUN_TIMEOUT = 600        # ein Workflow kann mehr verlangen ("timeout")

# Der ComfyUI-Server (URL), dessen Modelle ein Bild geladen hat und die der
# Chat vor seiner naechsten Antwort wieder freigeben soll ("" = keiner).
_comfy_holds_vram = ""


def _json(url, body=None, timeout=10):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else {}


def _up(comfy):
    try:
        _json(comfy + "/system_stats", timeout=2)
        return True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def workflows():
    """Die Workflows in workflows/: id, Name, Vorgaben."""
    out = []
    for f in sorted(WORKFLOWS.glob("*.json")):
        if not WORKFLOW_ID.fullmatch(f.stem):
            continue
        try:
            w = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError) as e:
            print(f"imagegen: {f.name}: {e}", flush=True)
            continue
        out.append({"id": f.stem, "name": w.get("name", f.stem), "about": w.get("about", ""),
                    "kind": w.get("kind", "image"), "defaults": w.get("defaults", {}),
                    "sets": sorted(w.get("set", {}))})
    return out


def _load(workflow_id):
    if not WORKFLOW_ID.fullmatch(workflow_id or ""):
        raise ValueError("unbekannter Workflow")
    try:
        return json.loads((WORKFLOWS / f"{workflow_id}.json").read_text("utf-8"))
    except OSError:
        raise ValueError(f"Workflow {workflow_id} gibt es nicht")


def _error_text(e):
    """ComfyUIs Fehlerantwort auf /prompt, lesbar: was an welchem Knoten nicht stimmt."""
    try:
        body = json.loads(e.read().decode(errors="replace"))
    except ValueError:
        return f"ComfyUI lehnt den Workflow ab (HTTP {e.code})"
    parts = [body.get("error", {}).get("message", "")]
    for node, err in (body.get("node_errors") or {}).items():
        for x in err.get("errors", []):
            parts.append(f"{err.get('class_type', node)}: {x.get('message', '')} {x.get('details', '')}".strip())
    return "; ".join(p for p in parts if p) or f"ComfyUI lehnt den Workflow ab (HTTP {e.code})"


class _Progress:
    """Hoert auf ComfyUIs Websocket mit und merkt sich je Auftrag den letzten
    Schritt ("progress": value von max). ComfyUI meldet das nur dem Client, der
    den Auftrag geschickt hat -- also mit derselben client_id und vor dem Auftrag
    verbunden. Ohne Websocket (aelteres ComfyUI, Proxy davor) bleibt es leer;
    dann gibt es eben keine Schritte, sonst laeuft alles wie vorher."""

    def __init__(self, comfy, client_id, node=None):
        self.steps = {}           # prompt_id -> (value, max)
        self.node = node          # nur die Schritte dieses Knotens (des Samplers)
        self.sock = None
        parts = urllib.parse.urlsplit(comfy)
        try:
            s = socket.create_connection((parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)),
                                         timeout=5)
            if parts.scheme == "https":
                s = ssl.create_default_context().wrap_socket(s, server_hostname=parts.hostname)
            key = base64.b64encode(os.urandom(16)).decode()
            s.sendall((f"GET /ws?clientId={client_id} HTTP/1.1\r\nHost: {parts.netloc}\r\n"
                       f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                       "Sec-WebSocket-Version: 13\r\n\r\n").encode())
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = s.recv(4096)
                if not chunk:
                    raise OSError("Verbindung zu")
                head += chunk
            if b" 101 " not in head.split(b"\r\n", 1)[0]:
                raise OSError("kein Websocket")
            s.settimeout(None)
            self.sock, self.buf = s, head.split(b"\r\n\r\n", 1)[1]
        except (OSError, ValueError):
            return
        threading.Thread(target=self._listen, daemon=True).start()

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("Verbindung zu")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _listen(self):
        message = b""
        try:
            while True:
                b0, b1 = self._read(2)
                n = b1 & 0x7F
                if n == 126:
                    n = int.from_bytes(self._read(2), "big")
                elif n == 127:
                    n = int.from_bytes(self._read(8), "big")
                mask = self._read(4) if b1 & 0x80 else None
                data = self._read(n)
                if mask:
                    data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
                op = b0 & 0x0F
                if op == 8:        # close
                    return
                if op == 9:        # ping: ein Client antwortet maskiert
                    m = os.urandom(4)
                    self.sock.sendall(bytes([0x8A, 0x80 | len(data)]) + m
                                      + bytes(c ^ m[i % 4] for i, c in enumerate(data)))
                    continue
                if op in (1, 0):   # Text (oder seine Fortsetzung); Binaeres sind Vorschaubilder
                    message += data
                    if b0 & 0x80:
                        self._note(message)
                        message = b""
        except (OSError, ValueError):
            pass

    def _note(self, raw):
        try:
            m = json.loads(raw)
        except ValueError:
            return
        d = m.get("data") or {}
        if m.get("type") == "progress" and d.get("prompt_id") and (self.node is None or str(d.get("node")) == self.node):
            self.steps[d["prompt_id"]] = (d.get("value"), d.get("max"))

    def close(self):
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
                self.sock.close()
            except OSError:
                pass


def _cancel(comfy, pid):
    """Bricht einen Auftrag ab, ob er wartet oder schon rechnet."""
    try:
        _json(comfy + f"/api/jobs/{pid}/cancel", {}, timeout=10)
    except (OSError, ValueError, urllib.error.URLError):
        try:   # ComfyUI vor der Jobs-API: aus der Schlange, und unterbrechen nur, wenn er rechnet
            _json(comfy + "/queue", {"delete": [pid]}, timeout=10)
            if any(len(x) > 1 and x[1] == pid for x in _json(comfy + "/queue", timeout=5).get("queue_running", [])):
                _json(comfy + "/interrupt", {}, timeout=10)
        except (OSError, ValueError, urllib.error.URLError):
            pass


def release_for_chat(wait=20):
    """Vor einer Chat-Antwort: gibt ComfyUI den Grafikspeicher her, den ein Bild
    oder Video belegt hat. /free setzt in ComfyUI nur ein Zeichen, das sein
    Arbeitsfaden zwischen zwei Auftraegen abarbeitet -- also warten, bis die Karte
    wirklich frei ist: ein Video (MiniMax H3) haelt sonst die ganzen 24 GB, wenn
    Ollama sein Modell laden will. Rechnet ComfyUI gerade etwas anderes, gibt es
    nichts her; dann nicht warten."""
    global _comfy_holds_vram
    url, _comfy_holds_vram = _comfy_holds_vram, ""
    if not url:
        return
    try:
        _json(url + "/free", {"unload_models": True, "free_memory": True}, timeout=30)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if _json(url + "/queue", timeout=5).get("queue_running"):
                return
            dev = (_json(url + "/system_stats", timeout=5).get("devices") or [{}])[0]
            if dev.get("vram_free", 0) >= 0.5 * dev.get("vram_total", 1):
                return
            time.sleep(0.3)
    except (OSError, ValueError, urllib.error.URLError):
        pass


def run(req, ollama, server, images_dir):
    """Erzeugt ein Bild oder Video auf `server` (comfyservers.py: name, url, local)
    und liefert unterwegs Zustaende (dicts); der letzte ist {"state": "done", ...}
    oder {"state": "error", "error": ...}."""
    global _comfy_holds_vram
    started = time.monotonic()
    comfy, local = server["url"], server.get("local", False)
    try:
        w = _load(req.get("workflow") or "z-image-turbo")
    except ValueError as e:
        yield {"state": "error", "error": str(e)}
        return
    kind = w.get("kind", "image")
    share = local and (req.get("free_vram", True) or w.get("card") == "exclusive")
    prompt = (req.get("prompt") or "").strip()
    if not prompt:
        yield {"state": "error", "error": "Keine Beschreibung" if kind == "video" else "Keine Bildbeschreibung"}
        return
    d = w.get("defaults", {})
    values = {"prompt": prompt}
    multiple = int(w.get("multiple", 16))   # Latents wollen Vielfache von 16 (H3: 32)
    for key, lo, hi in (("width", 256, 2048), ("height", 256, 2048), ("steps", 1, 100)):
        v = req.get(key) or d.get(key)
        if v is not None:
            v = max(lo, min(hi, int(v)))
            values[key] = max(multiple, v - v % multiple) if key != "steps" else v
    # Laenge eines Videos: Sekunden -> Bilder, auf das Raster des Modells
    # (H3: 17k+5 Bilder bei 24 fps; 124 = gut 5 s).
    if fr := w.get("frames"):
        try:
            secs = max(1.0, min(float(fr.get("max_seconds", 15)), float(req.get("seconds") or d.get("seconds") or 5)))
        except (TypeError, ValueError):
            secs = float(d.get("seconds") or 5)
        n = max(int(fr.get("plus", 1)), round(secs * fr.get("fps", 24)))
        every, plus = int(fr.get("every", 1)), int(fr.get("plus", 0))
        values["frames"] = n + (plus - n % every) % every
    seed = req.get("seed")
    values["seed"] = int(seed) if isinstance(seed, (int, float)) or (isinstance(seed, str) and seed.isdigit()) \
        else random.randrange(1, 2 ** 50)

    if not _up(comfy) and not local:
        yield {"state": "error", "error": f"ComfyUI auf {server.get('name')} ({comfy}) antwortet nicht"}
        return
    # Der eigene laeuft nicht: der Leitstand darf den Dienst starten (polkit-Regel).
    if not _up(comfy):
        yield {"state": "starting"}
        p = subprocess.run(["systemctl", "--no-ask-password", "start", "comfyui.service"],
                           capture_output=True, text=True, timeout=60)
        if p.returncode:
            yield {"state": "error", "error": "ComfyUI läuft nicht und ließ sich nicht starten: " + (p.stderr or p.stdout).strip()}
            return
        deadline = time.monotonic() + START_TIMEOUT
        while not _up(comfy):
            if time.monotonic() > deadline:
                yield {"state": "error", "error": "ComfyUI ist nach dem Start nicht erreichbar"}
                return
            time.sleep(1)

    if share:
        try:
            loaded = _json(ollama + "/api/ps", timeout=5).get("models", [])
        except (OSError, ValueError, urllib.error.URLError):
            loaded = []
        if loaded:
            yield {"state": "vram", "models": [m.get("name") for m in loaded]}
            for m in loaded:
                try:
                    _json(ollama + "/api/generate", {"model": m.get("name"), "keep_alive": 0}, timeout=60)
                except (OSError, ValueError, urllib.error.URLError):
                    pass

    graph = json.loads(json.dumps(w["graph"]))
    for key, value in values.items():
        node, field = (w.get("set") or {}).get(key, (None, None))
        if node in graph:
            graph[node]["inputs"][field] = value
    client_id = "leitstand-" + uuid.uuid4().hex
    progress = _Progress(comfy, client_id, w.get("progress"))
    try:
        pid = _json(comfy + "/prompt", {"prompt": graph, "client_id": client_id}, timeout=30)["prompt_id"]
    except urllib.error.HTTPError as e:
        progress.close()
        yield {"state": "error", "error": _error_text(e)}
        return
    except (OSError, ValueError, KeyError, urllib.error.URLError) as e:
        progress.close()
        yield {"state": "error", "error": f"ComfyUI nicht erreichbar: {e}"}
        return
    if share:
        _comfy_holds_vram = comfy

    finished = False
    try:
        last, last_sent = None, 0.0
        timeout = int(w.get("timeout", RUN_TIMEOUT))
        deadline = time.monotonic() + timeout
        while True:
            try:
                hist = _json(comfy + "/history/" + pid, timeout=10).get(pid)
            except (OSError, ValueError, urllib.error.URLError) as e:
                yield {"state": "error", "error": f"ComfyUI nicht erreichbar: {e}"}
                return
            if hist and ((hist.get("status") or {}).get("completed") is not None or hist.get("outputs")):
                finished = True
                break
            try:
                q = _json(comfy + "/queue", timeout=5)
            except (OSError, ValueError, urllib.error.URLError):
                q = {}
            if any(len(x) > 1 and x[1] == pid for x in q.get("queue_running", [])):
                now = {"state": "running"}
                if step := progress.steps.get(pid):
                    now["step"], now["steps"] = step
            else:
                ahead = [x for x in q.get("queue_pending", []) if len(x) > 1]
                pos = next((i for i, x in enumerate(sorted(ahead, key=lambda x: x[0])) if x[1] == pid), None)
                running = len(q.get("queue_running", []))
                now = {"state": "queued", "position": (pos or 0) + running}
            # Neues gleich, sonst alle paar Sekunden dasselbe: so merkt chat.py,
            # wenn die Seite weg ist, auch waehrend ComfyUI lange Modelle laedt.
            if now != last or time.monotonic() - last_sent > 5:
                yield now
                last, last_sent = now, time.monotonic()
            if time.monotonic() > deadline:
                yield {"state": "error", "error": f"ComfyUI hat nach {timeout // 60} Minuten nichts geliefert"}
                return
            time.sleep(0.4)
    finally:
        progress.close()
        if not finished:   # Fehler, Zeitlimit oder die Seite ist weg (GeneratorExit)
            _cancel(comfy, pid)

    status = hist.get("status") or {}
    if status.get("status_str") == "error":
        msg = "ComfyUI meldet einen Fehler"
        for kind, data in status.get("messages", []):
            if kind == "execution_error":
                msg = f"{data.get('node_type', '')}: {data.get('exception_message', '').strip()}"
        yield {"state": "error", "error": msg}
        return
    images_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    ext = ".mp4" if kind == "video" else ".png"
    for out in (hist.get("outputs") or {}).values():
        # SaveImage und SaveVideo melden beide unter "images" (Videos mit "animated").
        for im in out.get("images", []):
            if im.get("type") != "output" or not im.get("filename", "").lower().endswith(ext):
                continue
            q = urllib.parse.urlencode({"filename": im["filename"], "subfolder": im.get("subfolder", ""),
                                        "type": "output"})
            try:
                with urllib.request.urlopen(comfy + "/view?" + q, timeout=120) as r:
                    data = r.read()
            except (OSError, urllib.error.URLError) as e:
                yield {"state": "error", "error": f"{'Video' if kind == 'video' else 'Bild'} nicht abholbar: {e}"}
                return
            name = uuid.uuid4().hex + ext
            (images_dir / name).write_bytes(data)
            saved.append("images/" + name)
    if not saved:
        yield {"state": "error", "error": "Der Workflow hat kein Video gespeichert (fehlt ein SaveVideo-Knoten?)"
               if kind == "video" else "Der Workflow hat kein Bild gespeichert (fehlt ein SaveImage-Knoten?)"}
        return
    done = {"state": "done", "kind": kind, "images": saved, "prompt": prompt, "seed": values["seed"],
            "width": values.get("width"), "height": values.get("height"), "steps": values.get("steps"),
            "workflow": req.get("workflow") or "z-image-turbo", "workflow_name": w.get("name"),
            "server": server.get("name"),
            "seconds": round(time.monotonic() - started, 1)}
    if "frames" in values:
        done["duration"] = round(values["frames"] / (w.get("frames") or {}).get("fps", 24), 1)
    yield done
