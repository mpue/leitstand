"""Das Projektgedaechtnis des Chats: ein Index je freigegebenem Ordner, damit ein
Modell mit kleinem Kontext auch grosse Code-Projekte durchschaut.

Je Ordner eine SQLite-Datei in der Ablage (index/<hash>.sqlite) mit
- den Dateien: Zeilen, Sprache, Pruefsumme und einer Kurzfassung ihrer Symbole,
- den Symbolen: Klassen, Funktionen, Ueberschriften mit Zeile und Signatur,
- dem Volltext in Abschnitten (FTS5), samt den Teilwoertern der Bezeichner, so
  dass "shadow" auch renderShadowMap findet,
- den Notizen des Modells: was es ueber eine Datei, einen Ordner oder das
  Projekt herausgefunden hat. Sie gelten fuer alle Chats mit diesem Ordner; hat
  sich eine Datei seit ihrer Notiz geaendert, steht die Notiz als veraltet da.

Der Index haelt sich selbst aktuell: vor jeder Abfrage (hoechstens alle paar
Sekunden) vergleicht er Groesse und Aenderungszeit und liest nur Geaendertes neu.
Welche Dateien dazugehoeren, sagt git (versionierte und nicht ignorierte), sonst
dieselben Regeln wie folders.py: nichts Verstecktes, keine Build-Ordner.
"""

import ast
import hashlib
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import folders

VERSION = 2
MAX_FILE_BYTES = 1_000_000
CHUNK_LINES = 40
REFRESH_EVERY = 5          # Sekunden zwischen zwei Abgleichen eines Ordners
MAP_CHARS = 12000          # so lang wird project_map hoechstens
MAX_NOTE = 2000
TEXT_EXT = {
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".inl", ".ipp", ".m", ".mm",
    ".cs", ".java", ".kt", ".kts", ".scala", ".go", ".rs", ".swift", ".py", ".pyi", ".rb", ".php", ".pl",
    ".lua", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".html", ".htm", ".css", ".scss",
    ".glsl", ".vert", ".frag", ".geom", ".comp", ".tesc", ".tese", ".hlsl", ".fx", ".wgsl", ".metal", ".shader",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmake", ".mk", ".make", ".gradle",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".proto", ".graphql", ".sql",
    ".md", ".rst", ".txt", ".tex", ".csv", ".el", ".clj", ".hs", ".ml", ".ex", ".exs", ".erl", ".dart", ".zig", ".nim",
}
TEXT_NAMES = {"makefile", "cmakelists.txt", "dockerfile", "readme", "license", "meson.build", "build", "sconstruct"}
LANG = {".py": "python", ".pyi": "python", ".md": "markdown", ".rst": "markdown", ".lua": "lua", ".go": "go", ".rs": "rust",
        ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".vue": "js", ".svelte": "js",
        ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".cmake": "cmake"}
C_LIKE = {".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".inl", ".ipp", ".m", ".mm", ".cs", ".java", ".kt",
          ".scala", ".swift", ".php", ".glsl", ".vert", ".frag", ".geom", ".comp", ".tesc", ".tese", ".hlsl", ".fx",
          ".wgsl", ".metal", ".shader", ".dart", ".zig"}
HEADERS = {".h", ".hh", ".hpp", ".hxx", ".inl"}

_locks = {}
_locks_guard = threading.Lock()
_checked = {}              # Ordner -> Zeit des letzten Abgleichs
_root_dir = None           # setzt chat.py: wo die Index-Dateien liegen


def setup(data_dir):
    global _root_dir
    _root_dir = Path(data_dir)


# --- Symbole ----------------------------------------------------------------

TYPE_RE = re.compile(r"^\s*(?:template\s*<[^>]*>\s*)?(?:(?:public|private|protected|internal|static|final|abstract|sealed|"
                     r"partial|export|inline)\s+)*(class|struct|union|enum(?:\s+class|\s+struct)?|namespace|interface|record|"
                     r"trait|object)\s+(?:\[\[[^\]]*\]\]\s*)?(?:alignas\([^)]*\)\s*)?([A-Za-z_][\w:]*)")
FUNC_RE = re.compile(r"^(\s*)((?:[\w:<>,\*&~\[\]]+\s+)+)[\*&]*\s*((?:[A-Za-z_]\w*::)*~?[A-Za-z_]\w*|operator\s*\S+?)\s*\(")
NOT_TYPES = {"return", "else", "if", "for", "while", "switch", "case", "delete", "new", "throw", "goto", "using",
             "typedef", "sizeof", "co_return", "co_await", "co_yield", "do", "and", "or", "not", "await", "yield"}
NOT_NAMES = {"if", "for", "while", "switch", "return", "sizeof", "catch", "defined", "decltype", "static_assert", "alignof"}
OTHER_RES = {
    "lua": [(re.compile(r"^\s*(?:local\s+)?function\s+([\w.:]+)\s*\("), "function")],
    "go": [(re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)\s*\("), "function"),
           (re.compile(r"^type\s+(\w+)\s+(?:struct|interface)"), "type")],
    "rust": [(re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+(\w+)"), "function"),
             (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union|mod)\s+(\w+)"), "type"),
             (re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>]+\s+for\s+)?([\w:]+)"), "impl")],
    "js": [(re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)"), "function"),
           (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)"), "class"),
           (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|\w+\s*=>)"), "function"),
           (re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+(\w+)"), "type")],
    "shell": [(re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{?"), "function")],
    "cmake": [(re.compile(r"^\s*(?:function|macro)\s*\(\s*(\w+)", re.I), "function"),
              (re.compile(r"^\s*add_(?:executable|library)\s*\(\s*(\w+)", re.I), "target")],
    "markdown": [(re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$"), "heading")],
}


def _python_symbols(text):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    lines = text.splitlines()
    out = []

    def visit(body, owner=""):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{owner}{node.name}"
                kind = "class" if isinstance(node, ast.ClassDef) else "method" if owner else "function"
                sig = lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
                out.append((name, kind, node.lineno, sig[:160]))
                if isinstance(node, ast.ClassDef):
                    visit(node.body, name + ".")
    visit(tree.body)
    return out


def _c_symbols(text, header):
    """Typen und Funktionsdefinitionen in C, C++, C#, Java, Shadern …, nach Zeilenmustern.
    In Headern auch Deklarationen (bis zwei Ebenen eingerueckt)."""
    out, lines = [], text.splitlines()
    for n, line in enumerate(lines, 1):
        if len(line) > 300 or line.lstrip().startswith(("//", "/*", "*", "#")):
            continue
        m = TYPE_RE.match(line)
        if m:
            rest = line[m.end():].strip()
            if not rest.startswith((";", "=")):   # keine Vorwaertsdeklaration, kein Alias (namespace fs = …)
                out.append((m.group(2), m.group(1).split()[0], n, line.strip()[:160]))
            continue
        m = FUNC_RE.match(line)
        if not m:
            continue
        indent, types, name = len(m.group(1).expandtabs(4)), m.group(2).split(), m.group(3)
        if indent > 8 or name in NOT_NAMES or types[0] in NOT_TYPES or "=" in line[:m.start(3)]:
            continue
        tail = line[m.end():]
        nxt = next((l.strip() for l in lines[n:n + 3] if l.strip()), "")
        body = "{" in tail or (not tail.rstrip().endswith(";") and (nxt.startswith("{") or nxt.startswith(":")))
        decl = header and tail.rstrip().endswith(";") and indent <= 8
        if body or decl:
            out.append((name, "function" if body else "declaration", n, line.strip()[:160]))
    return out


def symbols(path, text):
    ext = path.suffix.lower()
    lang = LANG.get(ext)
    if lang == "python":
        return _python_symbols(text)
    if ext in C_LIKE:
        return _c_symbols(text, ext in HEADERS)
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for regex, kind in OTHER_RES.get(lang, []):
            m = regex.match(line)
            if m:
                name = m.group(m.lastindex)
                out.append((name, kind if kind != "heading" else f"h{len(m.group(1))}", n, line.strip()[:160]))
                break
    return out


def _parts(text):
    """Die Teilwoerter der Bezeichner (renderShadowMap -> render shadow map), fuer die Suche."""
    words = set()
    for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text):
        pieces = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", ident)
        if len(pieces) > 1:
            words.update(p.lower() for p in pieces if len(p) > 2)
    return " ".join(sorted(words))


# --- Index ------------------------------------------------------------------

def _lock(root):
    with _locks_guard:
        return _locks.setdefault(str(root), threading.Lock())


def _db(root):
    _root_dir.joinpath("index").mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(str(root).encode()).hexdigest()[:16]
    db = sqlite3.connect(_root_dir / "index" / f"{key}.sqlite", timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER, lines INTEGER,
                                          digest TEXT, outline TEXT);
        CREATE TABLE IF NOT EXISTS symbols (path TEXT, name TEXT, kind TEXT, line INTEGER, signature TEXT);
        CREATE INDEX IF NOT EXISTS symbols_name ON symbols (name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS symbols_path ON symbols (path);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(text, words, path UNINDEXED, start UNINDEXED,
                                                             tokenize="unicode61 tokenchars '_'");
        CREATE TABLE IF NOT EXISTS notes (path TEXT PRIMARY KEY, text TEXT, digest TEXT, updated REAL);
    """)
    if db.execute("SELECT value FROM meta WHERE key='version'").fetchone() != (str(VERSION),):
        db.executescript("DELETE FROM files; DELETE FROM symbols; DELETE FROM chunks;")   # Notizen bleiben
        db.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?), ('root', ?)", (str(VERSION), str(root)))
        db.commit()
    return db


def _candidates(root):
    """Die Dateien des Projekts, relativ: was git kennt (ohne Ignoriertes), sonst alles Sichtbare."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, timeout=30)
        names = out.stdout.decode(errors="replace").split("\0") if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        names = None
    if names is None:
        names = [(d / f).relative_to(root).as_posix() for d, _, files in folders._walk(root) for f in files]
    for rel in filter(None, names):
        parts = rel.split("/")
        if any(p.startswith(".") for p in parts) or any(p in folders.SKIP_DIRS for p in parts[:-1]):
            continue
        p = Path(rel)
        if p.suffix.lower() in TEXT_EXT or p.name.lower() in TEXT_NAMES:
            yield rel


def refresh(root, force=False):
    """Gleicht den Index mit dem Ordner ab; liest nur, was sich geaendert hat."""
    root = folders.root(root)
    with _lock(root):
        if not force and time.time() - _checked.get(str(root), 0) < REFRESH_EVERY:
            return {"changed": 0}
        db = _db(root)
        try:
            known = {p: (m, s) for p, m, s in db.execute("SELECT path, mtime, size FROM files")}
            seen, changed = set(), 0
            for rel in _candidates(root):
                f = root / rel
                try:
                    real = f.resolve()
                    if real != f and (root not in real.parents or folders._hidden(real, root)):
                        continue
                    st = f.stat()
                except OSError:
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    continue
                seen.add(rel)
                if known.get(rel) == (st.st_mtime, st.st_size):
                    continue
                try:
                    data = f.read_bytes()
                except OSError:
                    continue
                if b"\0" in data[:8192]:
                    continue
                _index_file(db, rel, Path(rel), data, st)
                changed += 1
            for rel in set(known) - seen:
                db.execute("DELETE FROM files WHERE path=?", (rel,))
                db.execute("DELETE FROM symbols WHERE path=?", (rel,))
                db.execute("DELETE FROM chunks WHERE path=?", (rel,))
                changed += 1
            db.commit()
        finally:
            db.close()
        _checked[str(root)] = time.time()
        return {"changed": changed}


def _index_file(db, rel, path, data, st):
    text = data.decode(errors="replace")
    lines = text.splitlines()
    syms = symbols(path, text)
    outline = ", ".join(dict.fromkeys(name for name, kind, _, _ in syms if kind not in ("declaration", "namespace")))[:300]
    db.execute("DELETE FROM symbols WHERE path=?", (rel,))
    db.execute("DELETE FROM chunks WHERE path=?", (rel,))
    db.executemany("INSERT INTO symbols VALUES (?, ?, ?, ?, ?)", [(rel, n, k, l, s) for n, k, l, s in syms])
    for start in range(0, len(lines), CHUNK_LINES):
        chunk = "\n".join(lines[start:start + CHUNK_LINES])
        if chunk.strip():
            db.execute("INSERT INTO chunks VALUES (?, ?, ?, ?)", (chunk, _parts(chunk) + " " + path.stem, rel, start + 1))
    db.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?)",
               (rel, st.st_mtime, st.st_size, len(lines), hashlib.sha1(data).hexdigest(), outline))


# --- Abfragen fuer das Modell -------------------------------------------------

def _n(x):
    return f"{x:,}".replace(",", ".")


def _resolve(roots, path):
    """(Ordner, relativer Pfad, angezeigter Pfad) fuer einen Pfad des Modells."""
    r, p = folders.target(roots, path)
    rel = p.relative_to(r).as_posix()
    return r, "" if rel == "." else rel, folders._shown(r, p)


def _note_line(row, digest):
    """Eine Notiz und ob sie noch zur Datei passt."""
    text, noted = row
    return text + ("  [Notiz veraltet: die Datei hat sich seitdem geändert]" if digest and noted and noted != digest else "")


def project_map(roots, path=""):
    """Die komprimierte Karte eines Ordners: Unterordner mit Umfang und Notiz, Dateien
    mit Zeilen und Notiz oder ihren Symbolen; Unterordner aufgeklappt, solange Platz ist."""
    r, rel, shown = _resolve(roots, path)
    refresh(r)
    db = _db(r)
    try:
        prefix = rel + "/" if rel else ""
        files = db.execute("SELECT path, lines, digest, outline FROM files WHERE path LIKE ? ESCAPE '\\' ORDER BY path",
                           (prefix.replace("_", "\\_").replace("%", "\\%") + "%",)).fetchall()
        notes = dict((p, (t, d)) for p, t, d in db.execute("SELECT path, text, digest FROM notes"))
    finally:
        db.close()
    if not files and rel and not (r / rel).is_dir():
        raise ValueError(f"{path} ist eine Datei: read_file nehmen")
    tree = {}
    for p, lines, digest, outline in files:
        node = tree
        for part in p[len(prefix):].split("/")[:-1]:
            node = node.setdefault(part + "/", {})
        node[p.rsplit("/", 1)[-1]] = (p, lines, digest, outline)

    def totals(node):
        n = lines = 0
        for v in node.values():
            if isinstance(v, dict):
                a, b = totals(v)
                n, lines = n + a, lines + b
            else:
                n, lines = n + 1, lines + (v[1] or 0)
        return n, lines

    count, total = totals(tree)
    head = [f"{shown}/  ({count} Dateien, {_n(total)} Zeilen)"]
    own = notes.get(prefix if rel else "")
    if own:
        head.append(f"Notiz: {own[0]}")
    # Breite zuerst: erst eine Ebene ganz, dann die naechste, solange Platz ist;
    # ein Ordner, der nicht mehr passt, bleibt zu (▸) und laesst sich mit path oeffnen.
    size, blocks, queue = sum(len(h) + 1 for h in head), {}, [(tree, prefix, 0)]
    while queue:
        node, at, depth = queue.pop(0)

        def level(with_outline):
            rows, pad = [], "  " * depth
            for name in sorted(node, key=lambda k: (not k.endswith("/"), k.lower())):
                v = node[name]
                if isinstance(v, dict):
                    n, lines = totals(v)
                    note = notes.get(at + name)
                    rows.append((at + name, f"{pad}{name}  ({n} Dateien, {_n(lines)} Z.)" +
                                 (f"  — {note[0]}" if note else ""), bool(note)))
                else:
                    p, lines, digest, outline = v
                    note = notes.get(p)
                    desc = (f"  — {_note_line(note, digest)}" if note else
                            f"  · {outline[:120]}" if outline and with_outline else "")
                    rows.append((None, f"{pad}{name}  ({lines} Z.){desc}", bool(note)))
            return rows

        rows = level(True)
        cost = sum(len(t) + 1 for _, t, _ in rows)
        if blocks and size + cost > MAP_CHARS:
            continue
        if not blocks and size + cost > MAP_CHARS:   # schon die oberste Ebene ist zu lang
            # erst ohne Symbole; reicht das nicht, zuerst Ordner und Notiertes, dann der Rest
            rows = level(False)
            if size + sum(len(t) + 1 for _, t, _ in rows) > MAP_CHARS:
                ranked = sorted(range(len(rows)), key=lambda i: (rows[i][0] is None and not rows[i][2], i))
                keep, used = set(), size
                for i in ranked:
                    if used + len(rows[i][1]) + 1 > MAP_CHARS - 100:
                        break
                    keep.add(i)
                    used += len(rows[i][1]) + 1
                hidden = len(rows) - len(keep)
                rows = [r for i, r in enumerate(rows) if i in keep]
                rows.append((None, f"{'  ' * depth}… und {hidden} weitere Dateien ohne Notiz: "
                                   f"find_symbol, search_code oder list_files mit pattern", False))
            cost = sum(len(t) + 1 for _, t, _ in rows)
        size += cost
        blocks[at] = rows
        queue += [(node[key[len(at):]], key, depth + 1) for key, _, _ in rows if key]

    lines_out = []

    def emit(at):
        for key, text, _ in blocks.get(at, []):
            lines_out.append(text + (" ▸" if key and key not in blocks else ""))
            if key in blocks:
                emit(key)
    emit(prefix)
    return {"map": "\n".join(head + lines_out),
            "hint": "Zeilen mit „—“ sind deine Notizen, mit „·“ die Symbole einer Datei. Mit path tiefer gehen; "
                    "find_symbol und search_code finden Stellen, read_file liest sie. Was du gelernt hast: save_note."}


def find_symbol(roots, name):
    name = str(name or "").strip()
    if not name:
        raise ValueError("name fehlt")
    out = []
    for r in [folders.root(x) for x in roots or []]:
        refresh(r)
        db = _db(r)
        try:
            short = name.split("::")[-1].split(".")[-1]
            rows = db.execute("""SELECT path, name, kind, line, signature FROM symbols
                                 WHERE name = ? COLLATE NOCASE OR name LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\'
                                 ORDER BY (name = ? COLLATE NOCASE) DESC, kind = 'declaration', path LIMIT 60""",
                              (name, "%::" + short.replace("_", "\\_"), "%." + short.replace("_", "\\_"), name)).fetchall()
            if not rows:
                rows = db.execute("SELECT path, name, kind, line, signature FROM symbols WHERE name LIKE ? ESCAPE '\\' "
                                  "ORDER BY length(name), path LIMIT 40", ("%" + short.replace("_", "\\_") + "%",)).fetchall()
        finally:
            db.close()
        out += [f"{r.name}/{p}:{line}  {kind} {n}  —  {sig}" for p, n, kind, line, sig in rows]
    return {"name": name, "count": len(out), "definitions": "\n".join(out[:60]) or "(nichts gefunden; search_code versuchen)"}


def search_code(roots, query, path=""):
    words = re.findall(r"[\w]+", str(query or ""), re.UNICODE)
    if not words:
        raise ValueError("Suchwörter fehlen")
    r, rel, _ = _resolve(roots, path)
    refresh(r)
    db = _db(r)
    try:
        hits = []
        for joiner in (" AND ", " OR "):   # erst alle Woerter, sonst irgendeins
            fts = joiner.join(f'"{w}"' for w in words)
            hits = db.execute("""SELECT path, start, text FROM chunks WHERE chunks MATCH ? AND path LIKE ? ESCAPE '\\'
                                 ORDER BY bm25(chunks, 1.0, 0.6) LIMIT 15""",
                              (fts, (rel.replace("_", "\\_") + "%") if rel else "%")).fetchall()
            if hits:
                break
    finally:
        db.close()
    needles = [w.lower() for w in words]
    out = []
    for p, start, text in hits:
        lines = text.splitlines()
        best = [(i, l) for i, l in enumerate(lines) if any(n in l.lower() for n in needles)][:3] or [(0, lines[0] if lines else "")]
        shown = "\n".join(f"  {start + i}: {l.strip()[:160]}" for i, l in best)
        out.append(f"{r.name}/{p}:{start}-{start + len(lines) - 1}\n{shown}")
    return {"query": " ".join(words), "count": len(out), "results": "\n".join(out) or "(keine Treffer)",
            "hint": "Nach Relevanz sortiert; die Zeilen zeigen die Treffer im Abschnitt. Ganz lesen mit read_file und start_line."}


def save_note(roots, path, text):
    text = str(text or "").strip()
    if not text:
        raise ValueError("Die Notiz ist leer")
    if len(text) > MAX_NOTE:
        raise ValueError(f"Zu lang ({len(text)} Zeichen, höchstens {MAX_NOTE}): kürzer zusammenfassen")
    r, rel, shown = _resolve(roots, path)
    key = rel + "/" if rel and (r / rel).is_dir() else rel
    refresh(r)
    db = _db(r)
    try:
        digest = db.execute("SELECT digest FROM files WHERE path=?", (rel,)).fetchone() if rel else None
        db.execute("INSERT OR REPLACE INTO notes VALUES (?, ?, ?, ?)", (key, text, digest[0] if digest else None, time.time()))
        db.commit()
    finally:
        db.close()
    return {"ok": True, "path": shown + ("/" if key.endswith("/") else ""), "chars": len(text)}


def note_for(roots, path):
    """Die Notiz zu einer Datei, fuer read_file; None ohne."""
    try:
        r, rel, _ = _resolve(roots, path)
    except ValueError:
        return None
    db = _db(r)
    try:
        row = db.execute("SELECT text, digest FROM notes WHERE path=?", (rel,)).fetchone()
        now = db.execute("SELECT digest FROM files WHERE path=?", (rel,)).fetchone()
    finally:
        db.close()
    return _note_line(row, now[0] if now else None) if row else None


def status(roots):
    """Fuer die Anzeige im Chat: je Ordner Dateien und Notizen."""
    out = []
    for x in roots or []:
        try:
            r = folders.root(x)
        except ValueError:
            continue
        db = _db(r)
        try:
            files = db.execute("SELECT count(*), coalesce(sum(lines), 0) FROM files").fetchone()
            notes = db.execute("SELECT count(*) FROM notes").fetchone()[0]
        finally:
            db.close()
        out.append({"root": str(r), "files": files[0], "lines": files[1], "notes": notes})
    return out
