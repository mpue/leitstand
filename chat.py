"""Der Chat des Leitstands: ein Web-Chat mit Ollama unter /chat/.

Hervorgegangen aus ollama-chat (~/Documents/devel/ollama-chat): die Seite liegt
in static/chat.html, die Chats als eine JSON-Datei pro Chat in
~/.local/share/ollama-chat/ -- dieselbe Ablage wie vorher, alte Chats sind also
da. Der Server reicht /chat/api/* an Ollama weiter (streamend), speichert die
Chats und macht aus angehaengten PDFs Text (pdftotext).
"""

import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

PAGE = Path(__file__).resolve().parent / "static" / "chat.html"
DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "ollama-chat"
CHAT_ID = re.compile(r"[0-9a-f]{32}")
CHAT_PATH = re.compile(r"/chat/chats/([0-9a-f]{32})")


class ChatStore:
    """Eine JSON-Datei pro Chat; Titel und Zeitstempel fuer die Seitenleiste im Speicher."""

    def __init__(self, root):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.index = {}
        for f in root.glob("*.json"):
            if not CHAT_ID.fullmatch(f.stem):
                continue
            try:
                chat = json.loads(f.read_text("utf-8"))
            except (OSError, ValueError) as e:
                print(f"chat: ueberspringe {f}: {e}", flush=True)
                continue
            self.index[f.stem] = self.meta(f.stem, chat)

    @staticmethod
    def meta(chat_id, chat):
        return {"id": chat_id, "title": chat.get("title", ""), "updated": chat.get("updated", 0)}

    def list(self):
        return sorted(self.index.values(), key=lambda c: c["updated"], reverse=True)

    def get(self, chat_id):
        path = self.root / f"{chat_id}.json"
        return path.read_bytes() if path.exists() else None

    def put(self, chat_id, data):
        chat = json.loads(data)
        with self.lock:
            tmp = self.root / f"{chat_id}.json.tmp"
            tmp.write_bytes(data)
            tmp.replace(self.root / f"{chat_id}.json")
            self.index[chat_id] = self.meta(chat_id, chat)

    def delete(self, chat_id):
        with self.lock:
            (self.root / f"{chat_id}.json").unlink(missing_ok=True)
            self.index.pop(chat_id, None)


_store = None
_store_lock = threading.Lock()


def store():
    # Erst beim ersten Abruf: ein Leitstand, dessen Chat nie geoeffnet wird,
    # legt auch keinen Ordner an.
    global _store
    with _store_lock:
        if _store is None:
            _store = ChatStore(DATA)
        return _store


def trusted(handler):
    """Der Proxy verbirgt Ollama den Origin des Browsers, also pruefen wir hier:
    angesprochen per IP, localhost oder den eigenen Rechnernamen (kein DNS-Rebinding),
    und -- falls der Browser einen Origin schickt -- von dieser Seite aus."""
    host = handler.headers.get("Host", "")
    name = (urlsplit("//" + host).hostname or "").lower()
    own = socket.gethostname().lower()
    if name not in ("localhost", own) and not name.startswith(own + "."):
        try:
            ipaddress.ip_address(name)
        except ValueError:
            return False
    origin = handler.headers.get("Origin")
    return origin is None or urlsplit(origin).netloc == host


def handle(handler, ollama):
    """Beantwortet eine Anfrage unter /chat; False, wenn der Pfad nicht dem Chat gehoert."""
    path = urlsplit(handler.path).path
    if path == "/chat":
        handler.send_response(301)
        handler.send_header("Location", "/chat/")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True
    if not path.startswith("/chat/"):
        return False
    if not trusted(handler):
        reply_json(handler, {"error": "Fremder Ursprung abgelehnt"}, 403)
        return True
    # Schreibendes nur mit dem Header des Leitstands (siehe server.py, do_POST).
    if handler.command != "GET" and handler.headers.get("X-Leitstand") != "1":
        reply_json(handler, {"error": "X-Leitstand-Header fehlt"}, 403)
        return True
    method = handler.command
    if path.startswith("/chat/api/"):
        proxy(handler, ollama + handler.path[len("/chat"):])   # Pfad und Query, ohne /chat
    elif path == "/chat/" and method == "GET":
        try:
            reply(handler, 200, PAGE.read_bytes(), "text/html; charset=utf-8")
        except OSError:
            reply_json(handler, {"error": "static/chat.html fehlt"}, 500)
    elif path == "/chat/chats" and method == "GET":
        reply_json(handler, store().list())
    elif path == "/chat/pdf-text" and method == "POST":
        pdf_text(handler)
    elif m := CHAT_PATH.fullmatch(path):
        chat(handler, m[1])
    else:
        reply_json(handler, {"error": "Nicht gefunden"}, 404)
    return True


def chat(handler, chat_id):
    if handler.command == "GET":
        data = store().get(chat_id)
        if data is None:
            return reply_json(handler, {"error": "Chat nicht gefunden"}, 404)
        reply(handler, 200, data, "application/json")
    elif handler.command == "PUT":
        try:
            store().put(chat_id, body(handler))
        except ValueError:
            return reply_json(handler, {"error": "Ungültiges JSON"}, 400)
        reply(handler, 204)
    elif handler.command == "DELETE":
        store().delete(chat_id)
        reply(handler, 204)
    else:
        reply_json(handler, {"error": "Methode nicht erlaubt"}, 405)


def pdf_text(handler):
    try:
        out = subprocess.run(["pdftotext", "-", "-"], input=body(handler), capture_output=True)
    except FileNotFoundError:
        return reply_json(handler, {"error": "pdftotext fehlt (Paket poppler-utils installieren)"}, 501)
    if out.returncode:
        reason = (out.stderr.decode(errors="replace").strip().splitlines() or ["unbekannter Fehler"])[-1]
        return reply_json(handler, {"error": f"PDF nicht lesbar: {reason}"}, 422)
    pages = out.stdout.decode(errors="replace").rstrip("\f").split("\f")
    text = "\n\n".join(f"--- Seite {i} ---\n{page.strip()}" for i, page in enumerate(pages, 1) if page.strip())
    reply_json(handler, {"text": text})


def proxy(handler, url):
    req = urllib.request.Request(url, data=body(handler) or None, method=handler.command,
                                 headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        return reply(handler, e.code, e.read(), e.headers.get("Content-Type", "application/json"))
    except urllib.error.URLError as e:
        return reply_json(handler, {"error": f"Ollama unter {url.split('/api/')[0]} nicht erreichbar: {e.reason}"}, 502)
    with resp:
        handler.send_response(resp.status)
        handler.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        try:
            while chunk := resp.read1(65536):
                handler.wfile.write(chunk)
                handler.wfile.flush()
        except ConnectionError:
            pass  # Browser hat abgebrochen; die geschlossene Verbindung beendet die Antwort in Ollama


def body(handler):
    return handler.rfile.read(int(handler.headers.get("Content-Length") or 0))


def reply(handler, status, data=b"", content_type=None):
    handler.send_response(status)
    if content_type:
        handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def reply_json(handler, data, status=200):
    reply(handler, status, json.dumps(data, ensure_ascii=False).encode(), "application/json")
