"""Der Chat des Leitstands: ein Web-Chat mit Ollama unter /chat/.

Hervorgegangen aus ollama-chat (~/Documents/devel/ollama-chat): die Seite liegt
in static/chat.html, die Chats als eine JSON-Datei pro Chat in
~/.local/share/ollama-chat/ -- dieselbe Ablage wie vorher, alte Chats sind also
da. Der Server reicht /chat/api/* an Ollama weiter (streamend), speichert die
Chats und macht aus angehaengten PDFs Text (pdftotext). Bilder und Videos
erzeugt ComfyUI (imagegen.py); sie liegen in der Ablage unter images/. Im Internet
sucht und liest websearch.py (SearXNG). Dateien, die das Modell zum Herunterladen
anlegt, liegen unter downloads/ (downloads.py). Boersenkurse und Kerzen holt
markets.py (Yahoo Finance), Musik macht ACE-Step 1.5 als eigener Dienst (music.py).
"""

import ipaddress
import json
import os
import re
import socket
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import codeindex
import comfytemplates
import downloads
import folders
import imagegen
import markets
import music
import websearch

PAGE = Path(__file__).resolve().parent / "static" / "chat.html"
DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "ollama-chat"
CHAT_ID = re.compile(r"[0-9a-f]{32}")
CHAT_PATH = re.compile(r"/chat/chats/([0-9a-f]{32})")
IMAGE_PATH = re.compile(r"/chat/images/(" + imagegen.MEDIA_NAME.pattern + ")")
IMAGES = DATA / "images"
DOWNLOADS = DATA / "downloads"
codeindex.setup(DATA)
DOWNLOAD_PATH = re.compile(r"/chat/downloads/([0-9a-f]{32})/([^/]+)")
# KaTeX und highlight.js, je Version ein Verzeichnis: kein Teil beginnt mit einem Punkt
VENDOR = PAGE.parent / "vendor"
VENDOR_PATH = re.compile(r"/chat/vendor/((?:[\w-][\w.-]*/)*[\w-][\w.-]*\.(js|css|woff2))")
VENDOR_TYPES = {"js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8", "woff2": "font/woff2"}


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
            path = self.root / f"{chat_id}.json"
            # Die Bilder und Videos, die ComfyUI fuer diesen Chat gemacht hat, und die
            # Dateien, die das Modell angelegt hat, gehen mit.
            try:
                text = path.read_text("utf-8")
                for name in set(re.findall(r'"images/([0-9a-f]{32}\.(?:png|mp4|mp3))"', text)):
                    (IMAGES / name).unlink(missing_ok=True)
                downloads.remove(DOWNLOADS, set(re.findall(r'"downloads/([0-9a-f]{32})/', text)))
            except OSError:
                pass
            path.unlink(missing_ok=True)
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


def handle(handler, ollama, comfy):
    """Beantwortet eine Anfrage unter /chat; False, wenn der Pfad nicht dem Chat gehoert.
    `comfy` ist der gewaehlte ComfyUI-Server (comfyservers.py): name, url, local."""
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
        if path == "/chat/api/chat":
            imagegen.release_for_chat()   # das Bildmodell raeumt die Karte fuer den Chat
        proxy(handler, ollama + handler.path[len("/chat"):])   # Pfad und Query, ohne /chat
    elif path == "/chat/image/workflows" and method == "GET":
        reply_json(handler, {"workflows": imagegen.workflows(), "server": comfy})
    elif path == "/chat/image" and method == "POST":
        image(handler, ollama, comfy)
    elif path == "/chat/web/status" and method == "GET":
        reply_json(handler, {"available": websearch.available()})
    elif path in ("/chat/web/search", "/chat/web/fetch") and method == "POST":
        web(handler, path.rsplit("/", 1)[1])
    elif path in ("/chat/files/browse", "/chat/files/list", "/chat/files/read", "/chat/files/search",
                  "/chat/files/map", "/chat/files/symbol", "/chat/files/code", "/chat/files/note",
                  "/chat/files/status") and method == "POST":
        files(handler, path.rsplit("/", 1)[1])
    elif path == "/chat/music" and method == "POST":
        compose(handler, ollama)
    elif path == "/chat/music/status" and method == "GET":
        reply_json(handler, music.status())
    elif path in ("/chat/music/start", "/chat/music/stop") and method == "POST":
        try:
            reply_json(handler, music.start(ollama) if path.endswith("start") else music.stop())
        except RuntimeError as e:
            reply_json(handler, {"error": str(e)}, 409)
    elif path in ("/chat/markets/search", "/chat/markets/quote", "/chat/markets/chart",
                  "/chat/markets/resolve") and method == "POST":
        market(handler, path.rsplit("/", 1)[1])
    elif path == "/chat/downloads" and method == "POST":
        make_file(handler, comfy)
    elif (m := DOWNLOAD_PATH.fullmatch(path)) and method == "GET":
        download(handler, m[1], unquote(m[2]))
    elif path in ("/chat/comfy/nodes", "/chat/comfy/templates") and method == "POST":
        comfy_lookup(handler, comfy, path.rsplit("/", 1)[1])
    elif (m := IMAGE_PATH.fullmatch(path)) and method == "GET":
        media(handler, IMAGES / m[1])
    elif (m := VENDOR_PATH.fullmatch(path)) and method == "GET":
        vendor(handler, VENDOR / m[1], VENDOR_TYPES[m[2]])
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


def image(handler, ollama, comfy):
    """Ein Bild oder Video von ComfyUI, als NDJSON-Strom der Zustaende (siehe imagegen.run)."""
    try:
        req = json.loads(body(handler) or b"{}")
    except ValueError:
        return reply_json(handler, {"error": "Ungültiges JSON"}, 400)
    handler.send_response(200)
    handler.send_header("Content-Type", "application/x-ndjson")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    states = imagegen.run(req, ollama, comfy, IMAGES)
    try:
        for state in states:
            handler.wfile.write((json.dumps(state, ensure_ascii=False) + "\n").encode())
            handler.wfile.flush()
    except ConnectionError:
        pass  # Stopp oder Seite zu
    finally:
        states.close()   # bricht den Auftrag in ComfyUI ab, falls er noch laeuft


def compose(handler, ollama):
    """Ein Stueck Musik von ACE-Step (music.py), als NDJSON-Strom der Zustaende."""
    try:
        req = json.loads(body(handler) or b"{}")
    except ValueError:
        return reply_json(handler, {"error": "Ungültiges JSON"}, 400)
    handler.send_response(200)
    handler.send_header("Content-Type", "application/x-ndjson")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    states = music.run(req, IMAGES, ollama)
    try:
        for state in states:
            handler.wfile.write((json.dumps(state, ensure_ascii=False) + "\n").encode())
            handler.wfile.flush()
    except ConnectionError:
        pass
    finally:
        states.close()


def market(handler, what):
    """Boersenkurse (markets.py): suchen, Kurse, Kerzen und die Eingabe des Schnellwegs."""
    try:
        req = json.loads(body(handler) or b"{}")
        result = (markets.search(req.get("query")) if what == "search"
                  else markets.quotes(req.get("symbols")) if what == "quote"
                  else markets.candles(req.get("symbol"), req.get("range"), req.get("interval")) if what == "chart"
                  else {"items": markets.resolve(req.get("text"))})
    except ValueError as e:
        return reply_json(handler, {"error": str(e)}, 400)
    except RuntimeError as e:
        return reply_json(handler, {"error": str(e)}, 502)
    reply_json(handler, result)


def make_file(handler, comfy):
    """Eine Datei, die das Modell anlegt (create_file): {name, content}."""
    try:
        req = json.loads(body(handler) or b"{}")
        result = downloads.save(DOWNLOADS, req.get("name"), req.get("content"), comfy)
    except ValueError as e:
        return reply_json(handler, {"error": str(e)}, 400)
    reply_json(handler, result)


def download(handler, file_id, name):
    """Immer als Anhang und in einer Sandbox: eine HTML-Datei des Modells laeuft
    nicht im Ursprung des Leitstands."""
    path = downloads.path_of(DOWNLOADS, file_id, name)
    if path is None:
        return reply_json(handler, {"error": "Datei nicht gefunden"}, 404)
    data = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", downloads.content_type(name))
    handler.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
    handler.send_header("Content-Security-Policy", "sandbox")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Cache-Control", "private, max-age=86400")   # die id ist eindeutig
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def comfy_lookup(handler, comfy, what):
    """Fuer die Werkzeuge comfy_nodes und comfy_templates: Knoten des ComfyUI-Servers
    beschreiben, seine Vorlagen suchen oder eine im API-Format holen."""
    try:
        req = json.loads(body(handler) or b"{}")
        if what == "nodes":
            result = downloads.nodes(DOWNLOADS, comfy, req.get("search"), req.get("names"))
        elif req.get("name"):
            result = comfytemplates.get(DOWNLOADS, comfy, req["name"])
        else:
            result = comfytemplates.search(comfy, req.get("search"))
    except ValueError as e:
        return reply_json(handler, {"error": str(e)}, 400)
    except RuntimeError as e:
        return reply_json(handler, {"error": str(e)}, 502)
    reply_json(handler, result)


def vendor(handler, path, ctype):
    try:
        data = path.read_bytes()
    except OSError:
        return reply_json(handler, {"error": "Datei nicht gefunden"}, 404)
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Cache-Control", "public, max-age=31536000, immutable")   # die Version steht im Pfad
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def media(handler, path):
    """Ein Bild oder Video aus der Ablage. Videos mit Range: ohne kann der
    Browser darin nicht springen."""
    try:
        data = path.read_bytes()
    except OSError:
        return reply_json(handler, {"error": "Datei nicht gefunden"}, 404)
    ctype = {".mp4": "video/mp4", ".mp3": "audio/mpeg"}.get(path.suffix, "image/png")
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", handler.headers.get("Range", "").strip())
    if not m or not (m[1] or m[2]):
        handler.send_response(200)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "private, max-age=86400")   # der Name ist eindeutig
        handler.end_headers()
        handler.wfile.write(data)
        return
    size = len(data)
    start, end = (int(m[1]), int(m[2]) if m[2] else size - 1) if m[1] else (max(0, size - int(m[2])), size - 1)
    if start >= size or end < start:
        handler.send_response(416)
        handler.send_header("Content-Range", f"bytes */{size}")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    end = min(end, size - 1)
    handler.send_response(206)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.send_header("Content-Length", str(end - start + 1))
    handler.send_header("Cache-Control", "private, max-age=86400")
    handler.end_headers()
    handler.wfile.write(data[start:end + 1])


def web(handler, what):
    """Eine Suche oder eine gelesene Seite fuer die Recherche des Chat-Modells."""
    try:
        req = json.loads(body(handler) or b"{}")
        result = (websearch.search(req.get("query"), req.get("language") or "de") if what == "search"
                  else websearch.fetch(req.get("url")))
    except ValueError as e:
        return reply_json(handler, {"error": str(e)}, 400)
    except RuntimeError as e:
        return reply_json(handler, {"error": str(e)}, 502)
    reply_json(handler, result)


def files(handler, what):
    """Die freigegebenen Ordner eines Chats (folders.py): die Auswahl und die Werkzeuge des
    Modells, dazu das Projektgedaechtnis (codeindex.py): Karte, Symbole, Suche, Notizen."""
    try:
        req = json.loads(body(handler) or b"{}")
        roots = req.get("roots")
        result = (folders.browse(req.get("path")) if what == "browse"
                  else folders.listing(roots, req.get("path"), req.get("pattern") or "") if what == "list"
                  else folders.read(roots, req.get("path"), req.get("start_line") or 1) if what == "read"
                  else folders.search(roots, req.get("query"), req.get("path")) if what == "search"
                  else codeindex.project_map(roots, req.get("path") or "") if what == "map"
                  else codeindex.find_symbol(roots, req.get("name")) if what == "symbol"
                  else codeindex.search_code(roots, req.get("query"), req.get("path") or "") if what == "code"
                  else codeindex.save_note(roots, req.get("path") or "", req.get("note")) if what == "note"
                  else {"folders": codeindex.status(roots)})
        if what == "read" and "text" in result:
            note = codeindex.note_for(roots, req.get("path"))
            if note:
                result["your_note"] = note   # was das Modell frueher ueber die Datei notiert hat
            else:
                result["hint"] = ("Zu dieser Datei hast du noch keine Notiz. Ist sie wichtig, halte das Wesentliche "
                                  "mit save_note fest, bevor du antwortest -- der Inhalt fällt später aus dem Gespräch.")
    except ValueError as e:
        return reply_json(handler, {"error": str(e)}, 400)
    except OSError as e:
        return reply_json(handler, {"error": f"Nicht lesbar: {e.strerror or e}"}, 400)
    except sqlite3.Error as e:
        return reply_json(handler, {"error": f"Projektindex: {e}"}, 500)
    reply_json(handler, result)


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
