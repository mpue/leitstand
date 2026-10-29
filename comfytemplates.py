"""ComfyUIs mitgelieferte Workflow-Vorlagen fuer das Chat-Modell (Werkzeug comfy_templates).

Ein Modell, das einen Workflow aus dem Kopf schreibt, trifft die Knoten, aber
selten das, was ein Modell wirklich braucht (bei Z-Image etwa CLIPLoader mit
lumina2 und ModelSamplingAuraFlow). Die Vorlagen, die ComfyUI unter
/templates/ ausliefert, laufen. Sie sind im Format der Oberflaeche: Knoten mit
Widget-Werten nach Position, Verbindungen als Liste, oft in Subgraphen. to_api()
macht daraus das API-Format, in dem das Modell Werte aendert und das
downloads.py prueft: Subgraphen aufgeloest (Knoten "57:30" wie im Export von
ComfyUI), Reroutes und Primitive durchgereicht, Ueberbruecktes ueberbrueckt,
Notizen weg.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

import downloads

INDEX_TTL = 300
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO"}
SKIP = {"Note", "MarkdownNote", "Reroute", "PrimitiveNode"}
MUTED, BYPASSED = 2, 4

_index = {}          # Server-URL -> (Zeit, Liste der Vorlagen)
_lock = threading.Lock()


def _get(comfy, path, timeout=20):
    try:
        with urllib.request.urlopen(comfy["url"] + path, timeout=timeout) as r:
            return json.loads(r.read())
    except (OSError, ValueError, urllib.error.URLError) as e:
        raise RuntimeError(f"ComfyUI ({comfy.get('name')}) liefert {path} nicht: {getattr(e, 'reason', e)}") from None


def index(comfy):
    with _lock:
        hit = _index.get(comfy["url"])
        if hit and time.time() - hit[0] < INDEX_TTL:
            return hit[1]
        cats = _get(comfy, "/templates/index.json")
        items = [dict(t, category=f"{c.get('category', '')} / {c.get('title', '')}".strip(" /"))
                 for c in cats if isinstance(c, dict) for t in c.get("templates") or [] if isinstance(t, dict)]
        _index[comfy["url"]] = (time.time(), items)
        return items


def search(comfy, words):
    """Vorlagen, in deren Namen, Titel, Beschreibung, Tags oder Modellen alle Woerter vorkommen."""
    words = str(words or "").lower().split()
    hits = []
    for t in index(comfy):
        text = " ".join([t.get("name", ""), t.get("title", ""), t.get("description", ""), t.get("category", ""),
                         *map(str, t.get("tags") or []), *map(str, t.get("models") or [])]).lower()
        if all(w in text for w in words) and not str(t.get("name", "")).endswith(".app"):   # .app: dieselben als App-Ansicht
            hits.append(t)
    # die freien zuerst: "api_"-Vorlagen rufen bezahlte Dienste auf
    hits.sort(key=lambda t: (t.get("openSource") is False, -(t.get("usage") or 0)))
    return {"count": len(hits), "templates": [{
        "name": t.get("name"), "title": t.get("title"), "models": t.get("models"),
        "description": (t.get("description") or "")[:200],
        **({"paid_api": True} if t.get("openSource") is False else {}),
    } for t in hits[:12]]}


def get(root, comfy, name):
    """Eine Vorlage im API-Format, geprueft gegen die Knoten des Servers."""
    name = str(name or "")
    if not any(t.get("name") == name for t in index(comfy)):
        raise ValueError(f"Keine Vorlage {name!r} -- erst mit search suchen")
    template = _get(comfy, f"/templates/{quote(name)}.json")
    info, _, _ = downloads.object_info(root, comfy)
    if info is None:
        raise RuntimeError("Die Knotenliste des Servers fehlt")
    graph, notes = to_api(template, info)
    problems = downloads._check_api(graph, info)
    return {"name": name, "server": comfy.get("name"), "workflow": graph, "notes": notes,
            "problems": problems[:downloads.MAX_PROBLEMS],
            "hint": "API-Format: ändere Werte (Prompt, Größe, Schritte, Seed …) und lege es mit create_file an. "
                    "Fehlende Modelldateien stehen unter problems."}


# --- UI-Format -> API-Format ------------------------------------------------

def _decode(declared, values, inputs, at=0, prefix="", order=None):
    """Verteilt die Widget-Werte eines Knotens (eine Liste, nach Position) auf seine
    Eingaenge, in der Reihenfolge der Oberflaeche; gibt die naechste Position zurueck.
    Ein Seed hat hinter sich "control_after_generate", ein Upload seinen Knopf; eine
    DynamicCombo speichert ihren Schluessel und dahinter die Werte ihrer gewaehlten
    Option ("sampling_mode.temperature")."""
    for part in ("required", "optional"):
        decls = declared.get(part) or {}
        for name in (order or {}).get(part) or list(decls):
            decl = decls.get(name)
            if not isinstance(decl, list) or not decl or at >= len(values):
                continue
            kind = decl[0]
            opts = decl[1] if len(decl) > 1 and isinstance(decl[1], dict) else {}
            if not _is_widget(kind, opts):
                continue
            inputs[prefix + name] = values[at]
            at += 1
            if kind == "COMFY_DYNAMICCOMBO_V3":
                chosen = next((c for c in opts.get("options") or [] if c.get("key") == values[at - 1]), None)
                if chosen:
                    at = _decode(chosen.get("inputs") or {}, values, inputs, at, prefix + name + ".")
            elif opts.get("control_after_generate") or opts.get("image_upload") or opts.get("video_upload") \
                    or opts.get("audio_upload"):
                at += 1
    return at


def _links(graph):
    """Link-id -> (von Knoten, von Ausgang, zu Knoten, zu Eingang); als Liste oder als Objekte."""
    out = {}
    for link in graph.get("links") or []:
        if isinstance(link, list) and len(link) >= 5:
            out[link[0]] = tuple(link[1:5])
        elif isinstance(link, dict):
            out[link.get("id")] = (link.get("origin_id"), link.get("origin_slot"), link.get("target_id"), link.get("target_slot"))
    return out


def _is_widget(kind, opts):
    """Hat der Eingang ein Widget? Die ueblichen Typen, eine DynamicCombo, und eigene
    Widget-Typen (BOUNDING_BOXES …), die einen Standardwert mitbringen."""
    return not opts.get("forceInput") and (isinstance(kind, list) or kind in WIDGET_TYPES or kind == "COMFY_DYNAMICCOMBO_V3"
                                           or opts.get("socketless") or "default" in opts)


def _defaults(declared, inputs, prefix=""):
    """Widgets, fuer die die Vorlage keinen Wert hat (sie ist aelter als der Knoten),
    bekommen ihren Standardwert -- wie in der Oberflaeche."""
    for part in ("required", "optional"):
        for name, decl in (declared.get(part) or {}).items():
            if not isinstance(decl, list) or not decl:
                continue
            kind = decl[0]
            opts = decl[1] if len(decl) > 1 and isinstance(decl[1], dict) else {}
            key = prefix + name
            if key not in inputs and _is_widget(kind, opts):
                if "default" in opts:
                    inputs[key] = opts["default"]
                elif isinstance(kind, list) and kind:
                    inputs[key] = kind[0]
                elif opts.get("options"):
                    first = opts["options"][0]
                    inputs[key] = first.get("key") if isinstance(first, dict) else first
            if kind == "COMFY_DYNAMICCOMBO_V3" and key in inputs:
                chosen = next((c for c in opts.get("options") or [] if c.get("key") == inputs[key]), None)
                if chosen:
                    _defaults(chosen.get("inputs") or {}, inputs, key + ".")


class _Scope:
    """Eine Ebene: der Hauptgraph oder ein Subgraph unter einem Knoten seiner Elternebene."""

    def __init__(self, graph, prefix="", parent=None, instance=None):
        self.graph, self.prefix, self.parent, self.instance = graph, prefix, parent, instance
        self.nodes = {n.get("id"): n for n in graph.get("nodes") or [] if isinstance(n, dict)}
        self.links = _links(graph)
        self.children = {}


def to_api(template, info):
    subgraphs = {s.get("id"): s for s in (template.get("definitions") or {}).get("subgraphs") or [] if isinstance(s, dict)}
    api, notes = {}, []

    def child(scope, node):
        if node["id"] not in scope.children:
            scope.children[node["id"]] = _Scope(subgraphs[node["type"]], f"{scope.prefix}{node['id']}:", scope, node)
        return scope.children[node["id"]]

    def source(scope, link_id, depth=0):
        """Woher ein Link wirklich kommt: ("<api-id>", Ausgang), ("value", Wert) oder None."""
        link = scope.links.get(link_id) if link_id is not None else None
        if link is None or depth > 50:
            return None
        origin, slot = link[0], link[1]
        if origin == -10:   # Eingang des Subgraphen: was am Knoten aussen anliegt
            if scope.parent is None:
                return None
            names = [i.get("name") for i in scope.graph.get("inputs") or []]
            name = names[slot] if slot < len(names) else None
            for i in scope.instance.get("inputs") or []:
                if i.get("name") == name:
                    return source(scope.parent, i.get("link"), depth + 1)
            return None
        return output(scope, origin, slot, depth + 1)

    def output(scope, node_id, slot, depth):
        node = scope.nodes.get(node_id)
        if node is None:
            return None
        kind = node.get("type")
        if node.get("mode") == MUTED:
            return None
        if node.get("mode") == BYPASSED:   # ein Eingang gleichen Typs geht durch
            outs = node.get("outputs") or []
            want = outs[slot].get("type") if slot < len(outs) else None
            for i in node.get("inputs") or []:
                if i.get("link") is not None and i.get("type") == want:
                    return source(scope, i["link"], depth)
            return None
        if kind in subgraphs:   # Ausgang eines Subgraphen: was innen dort ankommt
            inner = child(scope, node)
            for link_id, link in inner.links.items():
                if link[2] == -20 and link[3] == slot:
                    return source(inner, link_id, depth)
            return None
        if kind == "Reroute":
            ins = node.get("inputs") or []
            return source(scope, ins[0].get("link"), depth) if ins else None
        if kind == "PrimitiveNode":
            return ("value", (node.get("widgets_values") or [None])[0])
        return (scope.prefix + str(node_id), slot)

    def walk(scope):
        for node in scope.nodes.values():
            kind = node.get("type")
            if kind in SKIP or node.get("mode") in (MUTED, BYPASSED):
                continue
            if kind in subgraphs:
                walk(child(scope, node))
                continue
            spec = info.get(kind)
            inputs = {}
            if spec is None:
                notes.append(f"Knotentyp {kind} gibt es auf dem Server nicht")
            else:
                values = node.get("widgets_values")
                if isinstance(values, dict):   # manche Knoten speichern nach Namen
                    inputs.update({k: v for k, v in values.items() if isinstance(k, str)})
                elif isinstance(values, list):
                    _decode(spec.get("input") or {}, values, inputs, order=spec.get("input_order"))
            for i in node.get("inputs") or []:
                src = source(scope, i.get("link"))
                if src is None:
                    continue   # unverbunden: der Widget-Wert gilt
                inputs[i.get("name")] = src[1] if src[0] == "value" else [src[0], src[1]]
            if spec is not None:
                _defaults(spec.get("input") or {}, inputs)
            api[scope.prefix + str(node.get("id"))] = {
                "class_type": kind, "inputs": inputs,
                "_meta": {"title": node.get("title") or (spec or {}).get("display_name") or kind}}

    walk(_Scope(template))
    return api, notes
