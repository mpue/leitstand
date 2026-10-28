"""Die Internet-Recherche des Chats: suchen ueber SearXNG, Seiten als Text lesen.

Suchen fragt das eigene SearXNG (searxng.sh: ein Docker-Container auf
127.0.0.1:8888, LEITSTAND_SEARXNG). Lesen holt eine Seite selbst und macht
daraus lesbaren Text -- ohne Skripte, Stile, Navigation und Fusszeilen, mit
Ueberschriften und Absaetzen; PDFs ueber pdftotext.

Gelesen wird nur das oeffentliche Internet: Adressen, die auf dieses Netz
zeigen (localhost, 192.168.*, 10.*, ...), holt der Leitstand nicht. Was das
Modell liest, kommt von fremden Seiten, und eine Seite koennte es sonst
anweisen, den Router oder den Leitstand selbst abzurufen.
"""

import ipaddress
import json
import os
import re
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser

SEARXNG = os.environ.get("LEITSTAND_SEARXNG", "http://127.0.0.1:8888")
AGENT = "Mozilla/5.0 (X11; Linux x86_64) Leitstand-Recherche"
MAX_BYTES = 4 * 1024 * 1024
MAX_TEXT = 12000          # was eine Seite dem Modell an Text gibt


def available():
    try:
        with urllib.request.urlopen(SEARXNG + "/healthz", timeout=2) as r:
            return r.status == 200
    except (OSError, urllib.error.URLError):
        return False


def search(query, language="de", n=8):
    query = str(query or "").strip()
    if not query:
        raise ValueError("Suchbegriff fehlt")
    q = urllib.parse.urlencode({"q": query, "format": "json", "language": language or "de"})
    try:
        with urllib.request.urlopen(SEARXNG + "/search?" + q, timeout=20) as r:
            data = json.loads(r.read())
    except (OSError, ValueError, urllib.error.URLError) as e:
        raise RuntimeError(f"SearXNG antwortet nicht ({e}) -- sh searxng.sh") from None
    out, seen = [], set()
    for res in data.get("results", []):
        url = res.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"title": res.get("title", "").strip(), "url": url,
                    "snippet": re.sub(r"\s+", " ", res.get("content") or "").strip()[:300],
                    "date": res.get("publishedDate")})
        if len(out) >= n:
            break
    answers = [a if isinstance(a, str) else a.get("answer", "") for a in data.get("answers", [])]
    return {"query": query, "results": out, "answers": [a for a in answers if a][:3]}


# --- Lesen -----------------------------------------------------------------

def _public(host):
    """Zeigt der Name nur auf oeffentliche Adressen?"""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise ValueError(f"{host} ist unbekannt")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return False
    return True


class _NoInternalRedirect(urllib.request.HTTPRedirectHandler):
    # Eine Weiterleitung ins eigene Netz ist dieselbe Gefahr wie ein Link dorthin.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlsplit(newurl).hostname or ""
        if not _public(host):
            raise ValueError(f"Weiterleitung nach {host} (eigenes Netz) nicht erlaubt")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_NoInternalRedirect)


class _Text(HTMLParser):
    """HTML -> lesbarer Text: Ueberschriften mit #, Absaetze, Listen, ohne Beiwerk."""

    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form",
            "template", "iframe", "button", "select"}
    BLOCK = {"p", "div", "section", "article", "main", "br", "li", "tr", "table", "ul", "ol",
             "blockquote", "pre", "dd", "dt", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.title, self.in_title = [], 0, "", False
        # Der Hauptteil (<main>/<article>) extra: ist er da, ist er die Seite.
        self.core, self.in_core = [], 0

    def _emit(self, s):
        self.out.append(s)
        if self.in_core:
            self.core.append(s)

    def handle_starttag(self, tag, attrs):
        if tag in ("main", "article"):
            self.in_core += 1
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self.in_title = True
        elif not self.skip and tag in self.BLOCK:
            self._emit("\n")
            if tag[0] == "h" and tag[1:].isdigit():
                self._emit("#" * int(tag[1]) + " ")
            elif tag == "li":
                self._emit("- ")

    def handle_endtag(self, tag):
        if tag in ("main", "article") and self.in_core:
            self.in_core -= 1
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self.in_title = False
        elif not self.skip and tag in self.BLOCK:
            self._emit("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.skip:
            self._emit(data)

    @staticmethod
    def _clean(parts):
        t = re.sub(r"[ \t\r\f\v]+", " ", "".join(parts))
        t = re.sub(r" *\n *", "\n", t)
        t = re.sub(r"^(?:[-#]+ ?)?$\n?", "", t, flags=re.M)   # leere Punkte und Ueberschriften
        return re.sub(r"\n{3,}", "\n\n", t).strip()

    def text(self):
        core = self._clean(self.core)
        return core if len(core) > 400 else self._clean(self.out)


def fetch(url):
    url = str(url or "").strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("nur http- und https-Adressen")
    if not _public(parts.hostname):
        raise ValueError(f"{parts.hostname} liegt im eigenen Netz -- das liest die Recherche nicht")
    # gzip ausdruecklich: manche Server packen sonst trotzdem, und brotli kann
    # die Standardbibliothek nicht.
    req = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept-Language": "de,en;q=0.8",
                                               "Accept-Encoding": "gzip, deflate"})
    try:
        with _opener.open(req, timeout=20) as r:
            ctype = r.headers.get("Content-Type", "")
            charset = r.headers.get_content_charset()
            final = r.geturl()
            encoding = (r.headers.get("Content-Encoding") or "").lower()
            raw = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"die Seite antwortet mit HTTP {e.code}") from None
    except (OSError, urllib.error.URLError) as e:
        raise RuntimeError(f"nicht abrufbar: {getattr(e, 'reason', e)}") from None
    raw = raw[:MAX_BYTES]
    if encoding in ("gzip", "deflate", "x-gzip"):
        try:   # wbits 47: gzip oder zlib, was immer kommt
            raw = zlib.decompressobj(47 if encoding != "deflate" else 15).decompress(raw, MAX_BYTES * 4)
        except zlib.error:
            raise RuntimeError("die Seite kam kaputt gepackt") from None
    elif encoding not in ("", "identity"):
        raise RuntimeError(f"die Seite kam als {encoding} gepackt") from None
    if "pdf" in ctype or final.lower().endswith(".pdf"):
        try:
            p = subprocess.run(["pdftotext", "-layout", "-", "-"], input=raw, capture_output=True, timeout=60)
        except FileNotFoundError:
            raise RuntimeError("pdftotext fehlt (Paket poppler-utils)") from None
        title, text = os.path.basename(urllib.parse.urlsplit(final).path), p.stdout.decode(errors="replace")
    elif "html" in ctype or raw.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        if not charset:
            m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', raw[:4096], re.I)
            charset = m.group(1).decode() if m else "utf-8"
        parser = _Text()
        try:
            parser.feed(raw.decode(charset, errors="replace"))
        except LookupError:
            parser.feed(raw.decode("utf-8", errors="replace"))
        title, text = parser.title.strip(), parser.text()
    elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
        title, text = "", raw.decode(charset or "utf-8", errors="replace")
    else:
        raise RuntimeError(f"kein Text, sondern {ctype or 'unbekannter Inhalt'}")
    cut = len(text) > MAX_TEXT
    return {"url": final, "title": re.sub(r"\s+", " ", title), "text": text[:MAX_TEXT],
            "truncated": cut, "length": len(text)}
