"""Freigegebene Ordner fuer das Chat-Modell: auflisten, lesen, durchsuchen -- nur lesend.

Ein Chat nennt seine Ordner bei jeder Anfrage selbst (`roots`); hier wird nur
geprueft, dass sie im eigenen Home liegen und nichts Verstecktes darin sind.
Versteckt heisst: ein Teil des Pfads beginnt mit einem Punkt. Das haelt
~/.ssh, ~/.config, .git und .env-Dateien heraus, auch ueber Symlinks, denn
geprueft wird der aufgeloeste Pfad.
"""
import base64
import fnmatch
import os
import subprocess
from pathlib import Path

HOME = Path.home().resolve()
SKIP_DIRS = {"node_modules", "__pycache__", "venv", "build", "dist", "target"}   # beim Durchgehen, nicht beim Lesen
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
               ".gif": "image/gif", ".bmp": "image/bmp"}
MAX_ENTRIES = 400       # list_files
MAX_LINES = 800         # read_file, je Aufruf
MAX_CHARS = 40000
MAX_MATCHES = 60        # search_files
MAX_SEARCH_BYTES = 2_000_000
MAX_IMAGE_BYTES = 20_000_000


def _hidden(path, base):
    return any(part.startswith(".") for part in path.relative_to(base).parts)


def root(path):
    """Ein freigegebener Ordner, aufgeloest; ValueError, wenn er nicht taugt."""
    p = Path(str(path or "")).expanduser()
    if not p.is_absolute():
        raise ValueError(f"Kein absoluter Pfad: {path}")
    try:
        p = p.resolve(strict=True)
    except OSError:
        raise ValueError(f"Ordner nicht gefunden: {path}")
    if not p.is_dir():
        raise ValueError(f"Kein Ordner: {path}")
    if p != HOME and HOME not in p.parents:
        raise ValueError(f"Nur Ordner unter {HOME} lassen sich freigeben")
    if _hidden(p, HOME):
        raise ValueError("Versteckte Ordner (mit Punkt) lassen sich nicht freigeben")
    return p


def target(roots, path):
    """(Ordner, Pfad) fuer einen Pfad des Modells: absolut in einem der Ordner
    oder relativ zum ersten."""
    roots = [root(r) for r in roots or []]
    if not roots:
        raise ValueError("Dem Chat ist kein Ordner freigegeben")
    p = Path(str(path or "").strip() or ".").expanduser()
    # Das Modell sieht Pfade mit dem Namen des Ordners davor (siehe _shown) und gibt sie so zurueck.
    tries = [p] if p.is_absolute() else [roots[0] / p] + [r / Path(*p.parts[1:]) for r in roots if p.parts[:1] == (r.name,)]
    for p in tries:
        try:
            p = p.resolve(strict=True)
            break
        except OSError:
            pass
    else:
        raise ValueError(f"Nicht gefunden: {path}")
    for r in roots:
        if p == r or r in p.parents:
            if _hidden(p, r):
                raise ValueError("Versteckte Dateien und Ordner (mit Punkt) sind nicht freigegeben")
            return r, p
    raise ValueError(f"{path} liegt nicht in einem freigegebenen Ordner")


def _shown(r, p):
    """Wie das Modell den Pfad sieht: relativ zum Ordner, mit dessen Namen davor."""
    rel = p.relative_to(r)
    return r.name + ("/" + rel.as_posix() if rel.parts else "")


def _size(n):
    return f"{n} B" if n < 1024 else f"{n / 1024:.0f} KB" if n < 1024 ** 2 else f"{n / 1024 ** 2:.1f} MB"


def _walk(top):
    """Dateien und Ordner unter top, ohne Verstecktes und ohne SKIP_DIRS, sortiert."""
    for d, dirs, files in os.walk(top):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in SKIP_DIRS)
        yield Path(d), dirs, sorted(f for f in files if not f.startswith("."))


def browse(path=None):
    """Unterordner fuer die Auswahl im Chat."""
    p = root(path or HOME)
    try:
        dirs = sorted((x.name for x in p.iterdir() if x.is_dir() and not x.name.startswith(".")), key=str.lower)
    except OSError as e:
        raise ValueError(f"Nicht lesbar: {e.strerror}")
    return {"path": str(p), "parent": str(p.parent) if p != HOME else None, "dirs": dirs}


def listing(roots, path="", pattern=""):
    """Der Inhalt eines Ordners; mit pattern (z. B. *.py) alle passenden Dateien darunter."""
    r, p = target(roots, path)
    if not p.is_dir():
        raise ValueError(f"Kein Ordner: {path}")
    lines, more = [], False
    if pattern:
        for d, _, files in _walk(p):
            for f in files:
                if fnmatch.fnmatch(f.lower(), pattern.lower()):
                    if len(lines) == MAX_ENTRIES:
                        more = True
                        break
                    lines.append(f"{(d / f).relative_to(p).as_posix()}  {_size((d / f).stat().st_size)}")
            if more:
                break
    else:
        for x in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if x.name.startswith("."):
                continue
            if len(lines) == MAX_ENTRIES:
                more = True
                break
            try:
                lines.append(x.name + "/" if x.is_dir() else f"{x.name}  {_size(x.stat().st_size)}")
            except OSError:
                continue
    out = {"folder": _shown(r, p), "entries": "\n".join(lines) or "(leer)"}
    if more:
        out["note"] = f"nur die ersten {MAX_ENTRIES} Eintraege" + ("" if pattern else "; mit pattern gezielter suchen")
    return out


def read(roots, path, start=1):
    """Eine Datei: Text mit Zeilennummern (seitenweise ab start), PDFs als Text, Bilder als Base64."""
    r, p = target(roots, path)
    if p.is_dir():
        raise ValueError(f"{path} ist ein Ordner: list_files nehmen")
    size = p.stat().st_size
    shown = _shown(r, p)
    if p.suffix.lower() in IMAGE_TYPES:
        if size > MAX_IMAGE_BYTES:
            raise ValueError(f"Bild zu gross ({_size(size)})")
        return {"file": shown, "size": _size(size), "mime": IMAGE_TYPES[p.suffix.lower()],
                "image": base64.b64encode(p.read_bytes()).decode()}
    if p.suffix.lower() == ".pdf":
        try:
            out = subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True, timeout=60)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            raise ValueError("PDF nicht lesbar (pdftotext)")
        text = out.stdout.decode(errors="replace")
    else:
        with open(p, "rb") as f:
            head = f.read(8192)
            if b"\0" in head:
                raise ValueError(f"Binaerdatei ({_size(size)}), kein Text")
            text = (head + f.read()).decode(errors="replace")
    lines = text.splitlines()
    first = max(1, int(start or 1))
    chunk, chars = [], 0
    for n in range(first, len(lines) + 1):
        line = f"{n:>5}  {lines[n - 1]}"
        if chunk and (len(chunk) == MAX_LINES or chars + len(line) > MAX_CHARS):
            break
        chunk.append(line[:MAX_CHARS])   # eine einzelne Riesenzeile (minifiziert) gekuerzt
        chars += len(line) + 1
    last = first + len(chunk) - 1
    out = {"file": shown, "size": _size(size), "lines": len(lines), "text": "\n".join(chunk)}
    if lines and first > len(lines):
        out["note"] = f"Die Datei hat nur {len(lines)} Zeilen"
    elif last < len(lines):
        out["note"] = f"Zeilen {first}-{last} von {len(lines)}; weiter mit start_line={last + 1}"
    return out


def search(roots, query, path=""):
    """Textstellen (ohne Gross-/Kleinschreibung) in den Dateien unter path."""
    query = str(query or "").strip()
    if not query:
        raise ValueError("Suchtext fehlt")
    r, p = target(roots, path)
    needle, hits, files, more = query.lower(), [], 0, False
    for d, _, names in ([(p.parent, [], [p.name])] if p.is_file() else _walk(p)):
        for name in names:
            f = d / name
            try:
                real = f.resolve()
                if real != f and (r not in real.parents or _hidden(real, r)):
                    continue   # ein Symlink nach draussen oder auf Verstecktes
                if f.stat().st_size > MAX_SEARCH_BYTES or f.suffix.lower() in IMAGE_TYPES:
                    continue
                data = f.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            files += 1
            for n, line in enumerate(data.decode(errors="replace").splitlines(), 1):
                if needle in line.lower():
                    if len(hits) == MAX_MATCHES:
                        more = True
                        break
                    hits.append(f"{_shown(r, f)}:{n}: {line.strip()[:200]}")
            if more:
                break
        if more:
            break
    out = {"query": query, "files_searched": files, "count": len(hits), "matches": "\n".join(hits) or "(keine Treffer)"}
    if more:
        out["note"] = f"nur die ersten {MAX_MATCHES} Treffer; genauer suchen oder path einschraenken"
    return out
