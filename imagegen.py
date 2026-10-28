"""Bilder fuer den Chat: ein Workflow aus workflows/ an ComfyUI, das Bild zurueck.

Ein Workflow ist ComfyUIs API-Format ("Export (API)") mit einem kleinen Kopf
darum: welcher Knoten den Prompt, die Groesse, den Seed und die Schritte
bekommt, und mit welchen Werten er sonst laeuft (siehe workflows/*.json).

Der Ablauf meldet sich als Folge von Zustaenden, die chat.py als NDJSON an die
Seite streamt: ComfyUI startet, Ollama gibt den Grafikspeicher frei, wartet in
der Schlange, rechnet, fertig (mit dem Bild) -- oder ein Fehler.

Grafikspeicher: Ollamas grosse Modelle und ComfyUIs Bildmodell passen nicht
zusammen auf die Karte. Mit `free_vram` entlaedt ein Bild vorher die
Ollama-Modelle, und ComfyUI gibt seins erst wieder her, wenn der Chat das
naechste Mal Ollama fragt (release_for_chat) -- mehrere Bilder hintereinander
laden das Bildmodell so nur einmal. Nur fuer den eigenen Server: ein entfernter
rechnet auf einer anderen Karte, und starten kann der Leitstand ihn auch nicht.
"""

import json
import random
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent / "workflows"
WORKFLOW_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
IMAGE_NAME = re.compile(r"[0-9a-f]{32}\.png")
START_TIMEOUT = 120      # ComfyUI braucht beim ersten Start eine Weile
RUN_TIMEOUT = 600

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
                    "defaults": w.get("defaults", {}), "sets": sorted(w.get("set", {}))})
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


def release_for_chat():
    """Vor einer Chat-Antwort: gibt ComfyUI den Grafikspeicher her, den ein Bild belegt hat."""
    global _comfy_holds_vram
    url, _comfy_holds_vram = _comfy_holds_vram, ""
    if not url:
        return
    try:
        _json(url + "/free", {"unload_models": True, "free_memory": True}, timeout=30)
    except (OSError, ValueError, urllib.error.URLError):
        pass


def run(req, ollama, server, images_dir):
    """Erzeugt ein Bild auf `server` (comfyservers.py: name, url, local) und liefert
    unterwegs Zustaende (dicts); der letzte ist {"state": "done", ...} oder
    {"state": "error", "error": ...}."""
    global _comfy_holds_vram
    started = time.monotonic()
    comfy, local = server["url"], server.get("local", False)
    share = local and req.get("free_vram", True)
    try:
        w = _load(req.get("workflow") or "z-image-turbo")
    except ValueError as e:
        yield {"state": "error", "error": str(e)}
        return
    prompt = (req.get("prompt") or "").strip()
    if not prompt:
        yield {"state": "error", "error": "Keine Bildbeschreibung"}
        return
    d = w.get("defaults", {})
    values = {"prompt": prompt}
    for key, lo, hi in (("width", 256, 2048), ("height", 256, 2048), ("steps", 1, 100)):
        v = req.get(key) or d.get(key)
        if v is not None:
            v = max(lo, min(hi, int(v)))
            values[key] = v - v % 16 if key != "steps" else v   # Latents wollen Vielfache von 16
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
    try:
        pid = _json(comfy + "/prompt", {"prompt": graph, "client_id": "leitstand-" + uuid.uuid4().hex},
                    timeout=30)["prompt_id"]
    except urllib.error.HTTPError as e:
        yield {"state": "error", "error": _error_text(e)}
        return
    except (OSError, ValueError, KeyError, urllib.error.URLError) as e:
        yield {"state": "error", "error": f"ComfyUI nicht erreichbar: {e}"}
        return
    if share:
        _comfy_holds_vram = comfy

    last = None
    deadline = time.monotonic() + RUN_TIMEOUT
    while True:
        try:
            hist = _json(comfy + "/history/" + pid, timeout=10).get(pid)
        except (OSError, ValueError, urllib.error.URLError) as e:
            yield {"state": "error", "error": f"ComfyUI nicht erreichbar: {e}"}
            return
        if hist and ((hist.get("status") or {}).get("completed") is not None or hist.get("outputs")):
            break
        try:
            q = _json(comfy + "/queue", timeout=5)
        except (OSError, ValueError, urllib.error.URLError):
            q = {}
        if any(len(x) > 1 and x[1] == pid for x in q.get("queue_running", [])):
            now = {"state": "running"}
        else:
            ahead = [x for x in q.get("queue_pending", []) if len(x) > 1]
            pos = next((i for i, x in enumerate(sorted(ahead, key=lambda x: x[0])) if x[1] == pid), None)
            running = len(q.get("queue_running", []))
            now = {"state": "queued", "position": (pos or 0) + running}
        if now != last:
            yield now
            last = now
        if time.monotonic() > deadline:
            yield {"state": "error", "error": "ComfyUI hat nach 10 Minuten kein Bild geliefert"}
            return
        time.sleep(0.4)

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
    for out in (hist.get("outputs") or {}).values():
        for im in out.get("images", []):
            if im.get("type") != "output":
                continue
            q = urllib.parse.urlencode({"filename": im["filename"], "subfolder": im.get("subfolder", ""),
                                        "type": "output"})
            try:
                with urllib.request.urlopen(comfy + "/view?" + q, timeout=60) as r:
                    data = r.read()
            except (OSError, urllib.error.URLError) as e:
                yield {"state": "error", "error": f"Bild nicht abholbar: {e}"}
                return
            name = uuid.uuid4().hex + ".png"
            (images_dir / name).write_bytes(data)
            saved.append("images/" + name)
    if not saved:
        yield {"state": "error", "error": "Der Workflow hat kein Bild gespeichert (fehlt ein SaveImage-Knoten?)"}
        return
    yield {"state": "done", "images": saved, "prompt": prompt, "seed": values["seed"],
           "width": values.get("width"), "height": values.get("height"), "steps": values.get("steps"),
           "workflow": req.get("workflow") or "z-image-turbo", "workflow_name": w.get("name"),
           "server": server.get("name"),
           "seconds": round(time.monotonic() - started, 1)}
