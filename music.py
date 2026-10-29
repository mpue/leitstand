"""Musik fuer den Chat: ACE-Step 1.5 als eigener Dienst (acestep.service, 127.0.0.1:8001).

Nicht ueber ComfyUI -- dessen ACE-Step-1.5-Vorlage ruft ein Cloud-Modell auf. Der
Dienst laeuft nur, solange jemand Musik machen will: der Schalter im Chat startet
ihn (das laedt DiT, Sprachmodell und VAE in die Grafikkarte) und stoppt ihn
wieder. Bleibt er an, stoppt der Leitstand ihn nach IDLE_STOP ohne Auftrag selbst.

Ein Auftrag laeuft wie die API von ACE-Step es will: /release_task, dann
/query_result bis fertig (mit Fortschritt), dann die Datei ueber /v1/audio. Sie
landet in der Ablage des Chats (images/<id>.mp3), wie die Bilder und Videos.

Der Prompt kommt woertlich an: alles vor "Songtext:" (oder dem ersten [verse] …)
ist der Stil, alles danach der Text, und nichts davon schreibt ACE-Steps
Sprachmodell um -- kein sample_mode, kein create_sample, use_cot_caption und
use_cot_language aus. Das Sprachmodell setzt nur die Musik-Codes (thinking) und
ergaenzt Tempo, Tonart und Laenge, wenn sie nicht vorgegeben sind. Ohne Songtext
singt ACE-Step keine Worte.
"""

import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin

ACE = os.environ.get("LEITSTAND_ACESTEP", "http://127.0.0.1:8001")
UNIT = "acestep.service"
START_TIMEOUT = 300      # der erste Start laedt die Modelle
RUN_TIMEOUT = 900
IDLE_STOP = 15 * 60
# eine Zeile "Songtext:" / "Lyrics:" oder Abschnitte wie [verse] trennen Stil und Text
LYRICS_MARK = re.compile(r"^\s*(?:songtext|lyrics|text)\s*:\s*$|^\s*\[(?:verse|chorus|intro|bridge|outro|pre-chorus|hook)[^\]]*\]",
                         re.I | re.M)

DIT = "acestep-v15-turbo"
LM = "acestep-5Hz-lm-1.7B"

_last_use = 0.0
_busy = 0
_loading = False
_lock = threading.Lock()
_watch = None


def _json(path, body=None, timeout=10):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(ACE + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())
    if isinstance(out, dict) and out.get("code") not in (None, 200):
        raise RuntimeError(out.get("error") or f"ACE-Step antwortet mit {out.get('code')}")
    return out.get("data") if isinstance(out, dict) and "data" in out else out


def _health():
    try:
        return _json("/health", timeout=2) or {}
    except (OSError, ValueError, RuntimeError, urllib.error.URLError):
        return {}


def up():
    return _health().get("status") == "ok"


def ready():
    """Antwortet der Server und sind DiT und Sprachmodell geladen? Ohne /v1/init laedt
    ACE-Step sie erst mit dem ersten Auftrag."""
    h = _health()
    return h.get("status") == "ok" and bool(h.get("models_initialized")) and bool(h.get("llm_initialized"))


def _preload():
    """Nach dem Start: warten, bis der Server antwortet, dann die Modelle laden (gut 20 s)."""
    global _loading
    _loading = True
    try:
        deadline = time.monotonic() + START_TIMEOUT
        while not up():
            if time.monotonic() > deadline or _unit().get("ActiveState") in ("failed", "inactive"):
                return
            time.sleep(1)
        _json("/v1/init", {"model": DIT, "init_llm": True, "lm_model_path": LM}, timeout=600)
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as e:
        print("music: Modelle nicht geladen:", e, flush=True)
    finally:
        _loading = False


def _unit():
    out = subprocess.run(["systemctl", "show", UNIT, "--no-pager", "-p", "LoadState,ActiveState,SubState"],
                         capture_output=True, text=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def status():
    global _last_use
    u = _unit()
    if u.get("ActiveState") == "active" and not _last_use:
        # vor einem Neustart des Leitstands eingeschaltet: ab jetzt zaehlt der Leerlauf
        _last_use = time.time()
        _watcher()
    return {"installed": u.get("LoadState") == "loaded", "active": u.get("ActiveState"), "up": up(), "ready": ready(),
            "loading": _loading, "busy": _busy > 0,
            "idle_stop_in": max(0, round(_last_use + IDLE_STOP - time.time())) if _last_use and u.get("ActiveState") == "active" else None}


def _systemctl(verb):
    p = subprocess.run(["systemctl", "--no-ask-password", verb, UNIT], capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).strip() or f"systemctl {verb} {UNIT} ging nicht")


def start(ollama=None):
    """Startet den Dienst; die Modelle laedt er danach im Hintergrund (status().up).
    Vorher gehen Ollamas Modelle aus der Karte: ACE-Steps Sprachmodell bemisst seinen
    Speicher nach dem, was beim Start frei ist. Gibt die entladenen Modelle zurueck."""
    global _last_use
    if not status()["installed"]:
        raise RuntimeError(f"{UNIT} ist nicht eingerichtet (sudo sh install.sh)")
    unloaded = []
    if ollama and _unit().get("ActiveState") != "active":
        try:
            with urllib.request.urlopen(ollama + "/api/ps", timeout=5) as r:
                loaded = [m.get("name") for m in json.loads(r.read()).get("models", [])]
            for name in loaded:
                req = urllib.request.Request(ollama + "/api/generate", json.dumps({"model": name, "keep_alive": 0}).encode(),
                                             {"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=60).read()
                unloaded.append(name)
        except (OSError, ValueError, urllib.error.URLError):
            pass
    _systemctl("start")
    _last_use = time.time()
    if not _loading and not ready():
        threading.Thread(target=_preload, daemon=True).start()
    _watcher()
    return dict(status(), unloaded=unloaded)


def stop():
    if _busy:
        raise RuntimeError("ACE-Step komponiert gerade; danach stoppen")
    _systemctl("stop")
    return status()


def _watcher():
    """Stoppt den Dienst, wenn er IDLE_STOP lang nichts zu tun hatte."""
    global _watch
    if _watch and _watch.is_alive():
        return

    def loop():
        while True:
            time.sleep(min(60, IDLE_STOP / 4))
            if _busy or not _last_use or time.time() - _last_use < IDLE_STOP:
                continue
            if _unit().get("ActiveState") == "active":
                try:
                    _systemctl("stop")
                    print("music: ACE-Step nach Leerlauf gestoppt", flush=True)
                except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                    print("music:", e, flush=True)
            return
    _watch = threading.Thread(target=loop, daemon=True)
    _watch.start()


def split_prompt(text):
    """(Beschreibung oder Stil, Songtext oder None): ab "Songtext:" oder dem ersten [verse] ist es Text."""
    text = str(text or "").strip()
    m = LYRICS_MARK.search(text)
    if not m:
        return text, None
    style, lyrics = text[:m.start()].strip(), text[m.start():]
    lyrics = re.sub(r"^\s*(?:songtext|lyrics|text)\s*:\s*\n", "", lyrics, flags=re.I).strip()
    return style, lyrics


def _stage(text):
    """Was ACE-Step gerade tut, aus seinen Log-Zeilen (tqdm und INFO), die /query_result
    als progress_text mitschickt: erst rechnet sein Sprachmodell die Musik-Codes, dann
    setzen DiT und VAE den Klang zusammen."""
    if re.search(r"generate_music|DCW|tiled_decode|VAE|Normaliz|decode", text, re.I):
        return "sound"
    if text.startswith("Generating") or "tok/s" in text:
        return "codes"
    return "work"


def run(req, media_dir, ollama=None):
    """Ein Stueck Musik als Folge von Zustaenden (fuer den NDJSON-Strom des Chats)."""
    global _busy, _last_use
    text = str(req.get("prompt") or "").strip()
    if not text:
        yield {"state": "error", "error": "Keine Beschreibung"}
        return
    with _lock:
        _busy += 1
    try:
        t0 = time.monotonic()
        if not up():
            yield {"state": "starting"}
            try:
                if _unit().get("ActiveState") not in ("active", "activating"):
                    start(ollama)
            except RuntimeError as e:
                yield {"state": "error", "error": f"ACE-Step ließ sich nicht starten: {e}"}
                return
            while not up():
                if time.monotonic() - t0 > START_TIMEOUT:
                    yield {"state": "error", "error": "ACE-Step ist nach dem Start nicht bereit (Log: journalctl -u acestep)"}
                    return
                if _unit().get("ActiveState") == "failed":
                    yield {"state": "error", "error": "ACE-Step ist beim Start abgestürzt (Log: journalctl -u acestep)"}
                    return
                time.sleep(1)
        while _loading and not ready():   # der Schalter laedt die Modelle gerade
            if time.monotonic() - t0 > START_TIMEOUT:
                break
            yield {"state": "loading"}
            time.sleep(1)
        style, lyrics = split_prompt(text)
        lyrics = "[Instrumental]" if req.get("instrumental") else lyrics or ""
        body = {"prompt": style, "lyrics": lyrics, "thinking": True, "use_cot_caption": False, "use_cot_language": False,
                "audio_format": "mp3", "batch_size": 1, "inference_steps": int(req.get("steps") or 8)}
        if req.get("language"):
            body["vocal_language"] = req["language"]
        if req.get("duration"):
            body["audio_duration"] = max(10, min(600, int(req["duration"])))
        if str(req.get("seed") or "").strip().lstrip("-").isdigit() and int(req["seed"]) >= 0:
            body.update(use_random_seed=False, seed=int(req["seed"]))
        try:
            task = _json("/release_task", body, timeout=30)["task_id"]
        except (OSError, ValueError, KeyError, RuntimeError, urllib.error.URLError) as e:
            yield {"state": "error", "error": f"ACE-Step nahm den Auftrag nicht an: {getattr(e, 'reason', e)}"}
            return
        yield {"state": "queued", "task": task}
        item = {}
        while True:
            if time.monotonic() - t0 > RUN_TIMEOUT:
                yield {"state": "error", "error": f"ACE-Step hat nach {RUN_TIMEOUT // 60} Minuten nichts geliefert"}
                return
            try:
                item = (_json("/query_result", {"task_id_list": [task]}, timeout=15) or [{}])[0]
            except (OSError, ValueError, RuntimeError, urllib.error.URLError) as e:
                yield {"state": "error", "error": f"ACE-Step nicht erreichbar: {getattr(e, 'reason', e)}"}
                return
            if item.get("status") in (1, 2):
                break
            yield {"state": "running", "progress": item.get("progress"), "stage": _stage(item.get("progress_text") or "")}
            time.sleep(1)
        try:
            results = json.loads(item.get("result") or "[]")
        except ValueError:
            results = []
        if item.get("status") == 2 or not results:
            detail = next((r.get("error") for r in results if isinstance(r, dict) and r.get("error")), None)
            yield {"state": "error", "error": detail or item.get("progress_text") or "ACE-Step meldet einen Fehler"}
            return
        tracks = []
        media_dir.mkdir(parents=True, exist_ok=True)
        for r in results:
            if not r.get("file"):
                continue
            name = secrets.token_hex(16) + ".mp3"
            try:
                with urllib.request.urlopen(urljoin(ACE + "/", r["file"].lstrip("/")), timeout=120) as f:
                    (media_dir / name).write_bytes(f.read())
            except (OSError, urllib.error.URLError) as e:
                yield {"state": "error", "error": f"Musik nicht abholbar: {getattr(e, 'reason', e)}"}
                return
            metas = r.get("metas") or {}
            tracks.append({"url": f"images/{name}", "seed": r.get("seed_value"),
                           "caption": style, "lyrics": lyrics,   # was ACE-Step bekam, woertlich
                           "bpm": metas.get("bpm"), "key": metas.get("keyscale") or metas.get("key_scale"),
                           "duration": metas.get("duration"), "language": metas.get("vocal_language") or metas.get("language"),
                           "lm_model": r.get("lm_model"), "dit_model": r.get("dit_model")})
        if not tracks:
            yield {"state": "error", "error": "ACE-Step hat keine Datei geliefert"}
            return
        yield {"state": "done", "kind": "audio", "prompt": text, "tracks": tracks, "seconds": round(time.monotonic() - t0, 1)}
    finally:
        with _lock:
            _busy -= 1
        _last_use = time.time()
        _watcher()
