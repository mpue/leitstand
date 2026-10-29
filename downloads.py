"""Dateien, die das Chat-Modell anlegt (Werkzeug create_file), zum Herunterladen.

Jede Datei liegt in der Ablage des Chats unter downloads/<id>/<name>; die
Nachricht merkt sich {id, name, url, size}, und chat.py nimmt die Datei mit,
wenn der Chat geloescht wird. JSON muss gueltig sein. Sieht es nach einem
ComfyUI-Workflow aus, prueft check() es gegen die Knoten des gewaehlten
ComfyUI-Servers (/object_info): Knotentypen, Pflichteingaenge, Verbindungen
und Auswahlwerte wie Modelldateien oder Sampler. Die Knotenliste merkt sich
der Leitstand je Server auf der Platte -- so prueft und sucht er auch, wenn
ComfyUI gerade nicht laeuft.
"""

import difflib
import json
import mimetypes
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

MAX_BYTES = 2 * 1024 * 1024
ID = re.compile(r"[0-9a-f]{32}")
INFO_TTL = 300                     # so lange gilt die Knotenliste im Speicher
MAX_PROBLEMS = 25
MAX_OPTIONS = 40
# Knoten, die nur die Oberflaeche von ComfyUI kennt, nicht der Server
FRONTEND_ONLY = {"Note", "MarkdownNote", "Reroute", "PrimitiveNode"}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_info = {}                         # Server-URL -> (Zeit, object_info)
_info_lock = threading.Lock()


def clean_name(name):
    """Ein Dateiname ohne Pfad, ohne Punkt vorn und ohne Zeichen, die stoeren."""
    name = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^\w .,()+-]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")[:120].strip(" .")
    if not name:
        raise ValueError("Dateiname fehlt")
    return name


def save(root, name, content, comfy):
    """Legt die Datei an; ValueError, wenn sie nicht taugt. `comfy` ist der gewaehlte Server."""
    name = clean_name(name)
    if not isinstance(content, str):
        raise ValueError("content muss Text sein")
    data = content.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError(f"Zu groß: {len(data) // 1024} KB, höchstens {MAX_BYTES // 1024 // 1024} MB")
    result = {"name": name, "size": len(data)}
    if name.lower().endswith(".json"):
        try:
            parsed = json.loads(content)
        except ValueError as e:
            raise ValueError(f"Kein gültiges JSON: {e.msg} in Zeile {e.lineno}, Spalte {e.colno}. "
                             "Nichts angelegt -- bitte korrigieren und neu anlegen") from None
        check_result = check(parsed, comfy, root)
        if check_result:
            result["comfyui"] = check_result
        # eingerueckt, wie ComfyUI es selbst speichert: Modelle schreiben es gern in eine Zeile
        data = (json.dumps(parsed, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        result["size"] = len(data)
    file_id = secrets.token_hex(16)
    folder = root / file_id
    folder.mkdir(parents=True)
    (folder / name).write_bytes(data)
    result.update(id=file_id, url=f"downloads/{file_id}/{quote(name)}")
    return result


def path_of(root, file_id, name):
    """Der Pfad einer angelegten Datei oder None."""
    try:
        if not ID.fullmatch(file_id) or clean_name(name) != name:
            return None
    except ValueError:
        return None
    path = root / file_id / name
    return path if path.is_file() else None


def content_type(name):
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/json", "application/xml", "application/javascript"):
        ctype += "; charset=utf-8"
    return ctype


def remove(root, file_ids):
    for file_id in file_ids:
        if ID.fullmatch(file_id):
            shutil.rmtree(root / file_id, ignore_errors=True)


# --- ComfyUI ---------------------------------------------------------------

def _cache_file(root, url):
    host = re.sub(r"[^\w.-]+", "_", urlsplit(url).netloc or url)
    return root.parent / "comfy-nodes" / f"{host}.json"


def object_info(root, comfy):
    """Die Knoten des Servers: (object_info, Stand als Unix-Zeit, live?) oder (None, None, False)."""
    url = comfy["url"]
    with _info_lock:
        hit = _info.get(url)
        if hit and time.time() - hit[0] < INFO_TTL:
            return hit[1], hit[0], True
        cache = _cache_file(root, url)
        try:
            with urllib.request.urlopen(url + "/object_info", timeout=20) as r:
                info = json.loads(r.read())
        except (OSError, ValueError, urllib.error.URLError):
            try:
                stored = json.loads(cache.read_text("utf-8"))
                return stored["info"], stored["time"], False
            except (OSError, ValueError, KeyError):
                return None, None, False
        now = time.time()
        _info[url] = (now, info)
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps({"time": now, "info": info}), "utf-8")
        tmp.replace(cache)
        return info, now, True


def _graph(data):
    """Das API-Format darin: der Graph selbst, {"prompt": …} wie beim Abschicken oder
    der Graph eines Leitstand-Workflows ({"graph": …}). None, wenn keins."""
    for g in (data, data.get("prompt") if isinstance(data, dict) else None,
              data.get("graph") if isinstance(data, dict) else None):
        if isinstance(g, dict) and g and all(isinstance(v, dict) and "class_type" in v for v in g.values()):
            return g
    return None


def _options(decl):
    """Die erlaubten Werte eines Auswahleingangs, sonst None."""
    if not isinstance(decl, list) or not decl:
        return None
    if isinstance(decl[0], list):
        return decl[0]
    if decl[0] == "COMBO" and len(decl) > 1 and isinstance(decl[1], dict):
        return decl[1].get("options")
    return None


def _v3(kind):
    return isinstance(kind, str) and kind.startswith("COMFY_") and kind.endswith("_V3")


def _fits(want, got):
    if not isinstance(want, str) or not isinstance(got, str) or _v3(want) or _v3(got):
        return True   # MatchType und Co. nehmen, was kommt
    return want == got or "*" in (want, got) or got in want.split(",") or want in got.split(",")


def _flat(declared, values, prefix=""):
    """(Name, Deklaration, Pflicht?) der Eingaenge eines Knotens, mit den V3-Eingaengen
    so, wie ComfyUI sie erwartet: eine DynamicCombo als Auswahl ihrer Schluessel plus die
    Eingaenge der gewaehlten Option ("sampling_mode.temperature"); Autogrow und
    DynamicGroup haben nur Untereintraege ("images.image_1") und bleiben ungeprueft."""
    for part in ("required", "optional"):
        for name, decl in (declared.get(part) or {}).items():
            full = prefix + name
            kind = decl[0] if isinstance(decl, list) and decl else None
            opts = decl[1] if isinstance(decl, list) and len(decl) > 1 and isinstance(decl[1], dict) else {}
            if kind == "COMFY_DYNAMICCOMBO_V3":
                choices = opts.get("options") or []
                yield full, ["COMBO", {"options": [c.get("key") for c in choices]}], part == "required"
                chosen = next((c for c in choices if c.get("key") == values.get(full)), None)
                if chosen:
                    yield from _flat(chosen.get("inputs") or {}, values, full + ".")
            elif _v3(kind) and kind not in ("COMFY_MATCHTYPE_V3", "COMFY_MULTITYPED_V3"):
                continue
            else:
                yield full, decl, part == "required" and not opts.get("socketless")


def _suggest(cls, info):
    close = difflib.get_close_matches(str(cls), list(info), n=3, cutoff=0.6)
    return f" -- gemeint vielleicht {', '.join(close)}?" if close else " -- mit comfy_nodes nachschlagen"


def _needed(graph, info):
    """Die Knoten, die ComfyUI ausfuehrt: die Ausgabeknoten und alles, wovon sie abhaengen.
    Nur deren Eingaenge prueft ComfyUI; ein loser Knoten daneben stoert nicht."""
    todo = [nid for nid, n in graph.items() if (info.get(n.get("class_type")) or {}).get("output_node")]
    seen = set()
    while todo:
        nid = todo.pop()
        if nid in seen or nid not in graph:
            continue
        seen.add(nid)
        for value in (graph[nid].get("inputs") or {}).values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
                todo.append(str(value[0]))
    return seen


def _check_api(graph, info):
    problems = []
    runs = _needed(graph, info)
    for nid, node in graph.items():
        cls = node.get("class_type")
        spec = info.get(cls)
        where = f"Knoten {nid} ({cls})"
        if spec is None:   # das prueft ComfyUI fuer jeden Knoten
            problems.append(f"{where}: diesen Knotentyp gibt es auf dem Server nicht{_suggest(cls, info)}")
            continue
        if nid not in runs:
            continue
        inputs = node.get("inputs") or {}
        decls = {name: decl for name, decl, _ in _flat(spec.get("input") or {}, inputs)}
        for name, _, needed in _flat(spec.get("input") or {}, inputs):
            if needed and name not in inputs:
                problems.append(f"{where}: Pflichteingang {name} fehlt")
        for key, value in inputs.items():
            decl = decls.get(key)
            if decl is None:
                continue   # verborgene oder dynamische Eingaenge
            if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
                src = graph.get(str(value[0]))
                if src is None:
                    problems.append(f"{where}: {key} verweist auf Knoten {value[0]}, den es im Workflow nicht gibt")
                    continue
                outputs = (info.get(src.get("class_type")) or {}).get("output")
                if outputs is None:
                    continue
                if value[1] >= len(outputs):
                    problems.append(f"{where}: {key} will Ausgang {value[1]} von Knoten {value[0]}, "
                                    f"der hat nur {len(outputs)} ({', '.join(map(str, outputs))})")
                elif _options(decl) is None and not _fits(decl[0], outputs[value[1]]):
                    problems.append(f"{where}: {key} erwartet {decl[0]}, Knoten {value[0]} liefert dort {outputs[value[1]]}")
                continue
            options = _options(decl)
            if options and value not in options:   # leer: die Oberflaeche legt sie fest (CustomCombo)
                shown = ", ".join(map(str, options[:MAX_OPTIONS])) + (" …" if len(options) > MAX_OPTIONS else "")
                problems.append(f"{where}: {key} = {json.dumps(value, ensure_ascii=False)} gibt es nicht; möglich: {shown or '(keine)'}")
    if not any((info.get(n.get("class_type")) or {}).get("output_node") for n in graph.values()):
        problems.append("Kein Ausgabeknoten (etwa SaveImage oder PreviewImage): so führt ComfyUI den Workflow nicht aus")
    return problems


def _check_ui(data, info):
    subgraphs = (data.get("definitions") or {}).get("subgraphs") or []
    inner = {s.get("id") for s in subgraphs if isinstance(s, dict)}
    problems = []
    for owner in [data, *[s for s in subgraphs if isinstance(s, dict)]]:
        for node in owner.get("nodes") or []:
            cls = node.get("type") if isinstance(node, dict) else None
            if not cls or cls in FRONTEND_ONLY or cls in inner or UUID.fullmatch(str(cls)) or cls in info:
                continue
            problems.append(f"Knoten {node.get('id')} ({cls}): diesen Knotentyp gibt es auf dem Server nicht{_suggest(cls, info)}")
    return problems


def check(data, comfy, root):
    """Prueft einen ComfyUI-Workflow; None, wenn es keiner ist."""
    graph = _graph(data)
    ui = graph is None and isinstance(data, dict) and isinstance(data.get("nodes"), list) and "links" in data
    if graph is None and not ui:
        return None
    result = {"format": "API" if graph is not None else "UI",
              "nodes": len(graph) if graph is not None else len(data["nodes"]), "server": comfy.get("name")}
    info, stamp, live = object_info(root, comfy)
    if info is None:
        result.update(checked=False, note="Nicht geprüft: der ComfyUI-Server antwortet nicht, und es gibt noch keine gemerkte Knotenliste")
        return result
    problems = _check_api(graph, info) if graph is not None else _check_ui(data, info)
    result.update(checked=True, problems=problems[:MAX_PROBLEMS])
    if len(problems) > MAX_PROBLEMS:
        result["more"] = len(problems) - MAX_PROBLEMS
    if not live:
        result["note"] = "Geprüft gegen die gemerkte Knotenliste vom " + time.strftime("%d.%m.%Y %H:%M", time.localtime(stamp))
    return result


def nodes(root, comfy, search=None, names=None):
    """Fuer das Werkzeug comfy_nodes: Knoten suchen oder ihre Eingaenge und Ausgaenge."""
    info, stamp, live = object_info(root, comfy)
    if info is None:
        raise RuntimeError("Der ComfyUI-Server antwortet nicht, und es gibt noch keine gemerkte Knotenliste")
    out = {"server": comfy.get("name")}
    if not live:
        out["note"] = "Gemerkte Knotenliste vom " + time.strftime("%d.%m.%Y %H:%M", time.localtime(stamp))
    if names:
        found = {}
        for name in names if isinstance(names, list) else [names]:
            spec = info.get(name)
            if spec is None:
                found[name] = {"error": "unbekannt" + _suggest(name, info)}
                continue
            found[name] = _describe(spec)
        out["nodes"] = found
    if search:
        words = str(search).lower().split()
        hits = [(n, s) for n, s in info.items()
                if all(w in f"{n} {s.get('display_name', '')} {s.get('category', '')}".lower() for w in words)]
        out["matches"] = [{"name": n, "display_name": s.get("display_name"), "category": s.get("category")}
                          for n, s in sorted(hits)[:40]]
        out["count"] = len(hits)
    if not names and not search:
        raise ValueError("search oder names angeben")
    return out


def _describe(spec):
    def one(decl):
        options = _options(decl)
        extra = decl[1] if isinstance(decl, list) and len(decl) > 1 and isinstance(decl[1], dict) else {}
        if options is not None:
            d = {"type": "COMBO", "options": options[:MAX_OPTIONS]}
            if len(options) > MAX_OPTIONS:
                d["more_options"] = len(options) - MAX_OPTIONS
        else:
            d = {"type": decl[0] if isinstance(decl, list) and decl else decl}
        for k in ("default", "min", "max", "step", "multiline"):
            if k in extra:
                d[k] = extra[k]
        return d
    declared = spec.get("input") or {}
    labels = spec.get("output_name") or spec.get("output") or []
    return {
        "display_name": spec.get("display_name"), "category": spec.get("category"),
        "description": (spec.get("description") or "")[:300],
        "inputs": {part: {k: one(v) for k, v in (declared.get(part) or {}).items()} for part in ("required", "optional")},
        "outputs": [{"index": i, "type": t, "name": labels[i] if i < len(labels) else t}
                    for i, t in enumerate(spec.get("output") or [])],
        "output_node": bool(spec.get("output_node")),
    }

