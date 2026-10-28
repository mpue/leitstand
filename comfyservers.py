"""Die ComfyUI-Server des Leitstands und welcher gerade gilt.

Der eigene ist immer da: ComfyUI als Dienst comfyui.service auf diesem
Rechner (LEITSTAND_COMFY). Weitere im Netz kommen per Name und Adresse dazu.
Der gewaehlte gilt fuer die ComfyUI-Karte und fuer die Bilder im Chat; bei
einem entfernten gibt es keine Dienst-Knoepfe (die steuern nur den eigenen),
und der Chat raeumt fuer ihn keinen Grafikspeicher (er rechnet auf einer
anderen Karte).

Gespeichert in ~/.config/leitstand/settings.json unter "comfy".
"""

import json
import os
import re
import socket
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "leitstand" / "settings.json"
SERVER_ID = re.compile(r"[0-9a-f]{8}|local")


def normalize_url(text):
    """"192.168.178.77:8188" -> "http://192.168.178.77:8188"; ValueError, wenn es keine Adresse ist."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Adresse fehlt")
    parts = urlsplit(text if "://" in text else "http://" + text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"keine http-Adresse: {text}")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}:{parts.port or 8188}"


class Servers:
    def __init__(self, local_url):
        self.local = {"id": "local", "name": f"{socket.gethostname()} (lokal)",
                      "url": local_url.rstrip("/"), "local": True}
        self.lock = threading.Lock()
        self.others = []
        self.active_id = "local"
        try:
            j = json.loads(CONFIG.read_text("utf-8")).get("comfy", {})
            for s in j.get("servers", []):
                if SERVER_ID.fullmatch(str(s.get("id", ""))) and s.get("id") != "local":
                    self.others.append({"id": s["id"], "name": str(s.get("name") or s["url"]),
                                        "url": normalize_url(s["url"]), "local": False})
            self.active_id = j.get("active", "local")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def _save(self):
        try:
            data = json.loads(CONFIG.read_text("utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data["comfy"] = {"active": self.active_id,
                         "servers": [{"id": s["id"], "name": s["name"], "url": s["url"]} for s in self.others]}
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
        tmp.replace(CONFIG)

    def list(self):
        with self.lock:
            return [dict(self.local), *(dict(s) for s in self.others)]

    def active(self):
        with self.lock:
            for s in [self.local, *self.others]:
                if s["id"] == self.active_id:
                    return dict(s)
            return dict(self.local)

    def add(self, name, url):
        url = normalize_url(url)
        with self.lock:
            if url == self.local["url"] or any(s["url"] == url for s in self.others):
                raise ValueError(f"{url} steht schon in der Liste")
            s = {"id": uuid.uuid4().hex[:8], "name": (name or "").strip() or urlsplit(url).hostname,
                 "url": url, "local": False}
            self.others.append(s)
            self._save()
            return dict(s)

    def remove(self, server_id):
        with self.lock:
            if server_id == "local":
                raise ValueError("Der eigene Server bleibt")
            if not any(s["id"] == server_id for s in self.others):
                raise ValueError("unbekannter Server")
            self.others = [s for s in self.others if s["id"] != server_id]
            if self.active_id == server_id:
                self.active_id = "local"
            self._save()

    def select(self, server_id):
        with self.lock:
            if server_id != "local" and not any(s["id"] == server_id for s in self.others):
                raise ValueError("unbekannter Server")
            self.active_id = server_id
            self._save()
