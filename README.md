# Leitstand

Weboberfläche für powermaxx: Ollama, ComfyUI und die GPU im Browser.

- **GPU**: Auslastung, VRAM (aufgeteilt nach Ollama, ComfyUI und sonstigen Prozessen), Temperatur, Leistung, Takt, CPU, RAM; Verläufe der letzten 10 Minuten
- **Ollama**: geladene Modelle (Größe, GPU-/CPU-Anteil, Kontext, Restzeit bis zum Entladen), installierte Modelle, Laden/Entladen
- **ComfyUI**: Modelle im Speicher, Dateien des letzten Workflows, Warteschlange, Speicher freigeben. Oben in der Karte wählt man den Server: den eigenen (der Dienst auf diesem Rechner) oder einen im Netz (*+ Server hinzufügen…*: Name und Adresse wie `192.168.178.89:8188`)
- **Dienste**: Status, Starten/Stoppen/Neustart, Log
- **Chat** (`/chat/`, Knopf oben rechts): ein Web-Chat mit den Ollama-Modellen -- Streaming, Chat-Verlauf, System-Prompt, Temperatur und Kontextlänge pro Chat, Thinking-Modelle, Anhänge (Text, Code, PDF über `pdftotext`, Bilder für Vision-Modelle), Modelle herunterladen, Export als Markdown. Formeln (`$…$`, `$$…$$`, `\(…\)`, `\[…\]`, ```` ```math ````) setzt KaTeX, Codeblöcke färbt highlight.js nach ihrer Sprache. Die Chats liegen als eine JSON-Datei pro Chat in `~/.local/share/ollama-chat/`
- **Bilder im Chat** über ComfyUI: der Schalter „🖼 Bild“ schickt die Nachricht als Bildbeschreibung direkt an ComfyUI; Modelle mit Tools (qwen3-coder …) dürfen im Gespräch selbst malen lassen („mal mir …“) und schreiben dafür einen englischen Prompt. Format, Schritte, Seed und Workflow stehen in den Einstellungen. Läuft ComfyUI nicht, startet der Chat es. Die Bilder liegen unter `~/.local/share/ollama-chat/images/` und gehen mit ihrem Chat, wenn er gelöscht wird
- **Videos im Chat** mit Ton (MiniMax H3 über ComfyUI): der Schalter „🎬 Video“ schickt die Nachricht als Videobeschreibung an ComfyUI, und Modelle mit Tools dürfen auf Wunsch selbst ein Video drehen lassen (`generate_video`: ein englisches Drehbuch mit Kamera, Zeitplan und einem Absatz „Audio:“ für Geräusche, Musik und gesprochene Sätze). Format, Länge (5, 10, 15 s) und Workflow (schnell mit 4 Schritten oder 8 Schritte) stehen in den Einstellungen. Während ComfyUI rechnet, zeigt der Chat Schritt x von y mit Balken, die Laufzeit und die Restzeit; Stopp bricht den Auftrag in ComfyUI ab. Auf der 4090 dauert ein 5-s-Video gut 80 s (4 Schritte) bzw. 2,5 min (8 Schritte)
- **Internet-Recherche im Chat**: Modelle mit Tools suchen über das eigene SearXNG (`web_search`) und lesen Seiten und PDFs (`fetch_page`); die Antwort nennt die gelesenen Quellen, und über der Antwort steht, was gesucht und gelesen wurde. Abschaltbar in den Einstellungen; ohne laufendes SearXNG gibt es die Werkzeuge nicht

Aufruf: <http://192.168.178.102:8100>

## Aufbau

- `server.py`: Python-Standardbibliothek, keine Abhängigkeiten. Liest `nvidia-smi`, fragt die APIs von Ollama (11434) und ComfyUI (8188) ab und ruft `systemctl` auf
- `static/`: die Seite (HTML, CSS, JS ohne Build). Wird bei jedem Abruf von der Platte gelesen, Änderungen gelten nach einem Neuladen im Browser
- `chat.py` + `static/chat.html`: der Chat unter `/chat/` (hervorgegangen aus `~/Documents/devel/ollama-chat`): leitet `/chat/api/*` an Ollama weiter, speichert die Chats, wandelt PDFs in Text
- `static/vendor/`: KaTeX 0.19.0 und highlight.js 11.12.0 für den Chat (aus den npm-Paketen `katex` und `@highlightjs/cdn-assets`, mit ihren Lizenzen; von KaTeX nur die woff2-Schriften). Der Chat liefert sie unter `/chat/vendor/` aus; die Version steht im Verzeichnisnamen, damit der Browser sie dauerhaft zwischenspeichern darf
- `websearch.py`: die Recherche des Chats. Sucht über SearXNG (`LEITSTAND_SEARXNG`, Standard `http://127.0.0.1:8888`), liest Seiten selbst und macht daraus Text (Hauptteil der Seite, ohne Menüs und Skripte; PDFs über `pdftotext`)
- `searxng.sh`: richtet SearXNG als Docker-Container ein (`sh searxng.sh`, ohne sudo, wenn man in der Gruppe `docker` ist): nur auf `127.0.0.1:8888`, mit JSON-Ausgabe, startet mit Docker. Die Einstellungen liegen in `~/.config/leitstand/searxng/settings.yml` (der Container gibt den Ordner seinem eigenen Benutzer; ändern daher mit sudo)
- `imagegen.py` + `workflows/`: Bilder und Videos für den Chat. Ein Workflow ist ComfyUIs API-Format mit einem Kopf darum (siehe `workflows/z-image-turbo.json`): `set` sagt, welcher Knoten den Prompt, die Größe, den Seed und die Schritte bekommt, `defaults` die Vorgaben, `progress` den Sampler, dessen Schritte der Chat zeigt (über ComfyUIs Websocket). Ein Video-Workflow (`workflows/minimax-h3*.json`) hat dazu `"kind": "video"`, `frames` (Sekunden → Bilder auf dem Raster des Modells, bei H3 17k+5 bei 24 fps), `multiple` (Seitenlängen in 32er-Schritten), `timeout` und `"card": "exclusive"`. Ein weiterer Workflow: in ComfyUI über *Export (API)* speichern, so einpacken und als `workflows/<name>.json` ablegen -- er erscheint ohne Neustart in den Einstellungen

### ComfyUI-Server

Welcher ComfyUI gilt, ist eine Einstellung für den ganzen Leitstand: die ComfyUI-Karte zeigt ihn, und der Chat malt auf ihm (umschaltbar auch in den Bildeinstellungen des Chats). Gespeichert in `~/.config/leitstand/settings.json` (`comfyservers.py`). Bei einem entfernten Server gibt es keine Dienst-Knöpfe und kein Log (die gelten dem eigenen Dienst), der Chat startet ihn nicht und räumt für ihn keinen Grafikspeicher -- er rechnet auf seiner eigenen Karte. Er muss mit `--listen` laufen und die Modelle des Workflows haben; ohne den Leitstand-Node (`comfy_node/leitstand_status.py` in seinen `custom_nodes`) zeigt die Karte nicht, was er im Speicher hat.

### Grafikspeicher

Ein großes Ollama-Modell (qwen3-coder:30b: rund 20 GB) und Z-Image Turbo (rund 12 GB) passen nicht zusammen auf die 24-GB-Karte. Mit „Grafikspeicher teilen“ (Standard) entlädt ein Bild vorher die Ollama-Modelle, und ComfyUI gibt seins frei, sobald der Chat das nächste Mal Ollama fragt -- mehrere Bilder hintereinander laden das Bildmodell nur einmal. Ein Bild dauert so etwa 4 s, ein Chat-Modell nach einem Bild wieder zu laden ein paar Sekunden. Kleinere Chat-Modelle (gemma4:12b, qwen3.5:9b: rund 10 GB mit 32k Kontext) passen neben Z-Image, mit ihnen kann das Teilen aus bleiben.

Ein Video (MiniMax H3) braucht die ganze Karte (bis 23,8 GB): sein Workflow sagt `"card": "exclusive"`, also entlädt es die Ollama-Modelle immer, auch ohne „Grafikspeicher teilen“. Vor der nächsten Chat-Antwort gibt ComfyUI die Karte wieder her, und der Chat wartet, bis sie wirklich frei ist (ComfyUI erledigt `/free` erst zwischen zwei Aufträgen). Rechnet ComfyUI gerade etwas anderes -- etwa ein Workflow, den man in ComfyUI selbst gestartet hat --, gibt ComfyUI nichts her, bis der fertig ist, und das Chat-Modell findet keinen Platz (Fehler „out of memory“).
- `comfy_node/leitstand_status.py`: ComfyUI hat keinen Endpunkt für geladene Modelle; der Node hängt `GET /leitstand/models` an
- `install.sh`: richtet alles ein (`sudo sh install.sh`)

## Was install.sh einrichtet

| Was | Wo |
|---|---|
| Leitstand-Dienst (Port 8100, Autostart) | `/etc/systemd/system/leitstand.service` |
| ComfyUI-Dienst (`--listen 0.0.0.0`, kein Autostart) | `/etc/systemd/system/comfyui.service` |
| Start/Stopp ohne Passwort für ollama und comfyui | `/etc/polkit-1/rules.d/50-leitstand.rules` |
| Leitstand-Node | Symlink in `ComfyUI/custom_nodes/` |
| ufw: 8100 und 8188 nur aus 192.168.178.0/24 | greift erst, wenn ufw aktiv ist |

Nach Änderungen an `server.py`: `sudo systemctl restart leitstand`.

## Sicherheit

Kein Login: Jeder im Heimnetz kann die Dienste starten und stoppen, genau wie er die Ollama-API ohnehin nutzen kann. POST-Anfragen brauchen den Header `X-Leitstand: 1`. Dadurch kann eine fremde Webseite im Browser keine Aktionen auslösen.

Die Recherche liest nur das öffentliche Internet: Adressen, die ins eigene Netz zeigen (localhost, 192.168.\*, 10.\* …), auch über Weiterleitungen, holt sie nicht. Was das Modell liest, kommt von fremden Seiten, und eine Seite könnte es sonst anweisen, den Router oder den Leitstand selbst abzurufen.

Für den Chat gilt dasselbe (alles außer GET braucht den Header), und zusätzlich: Er antwortet nur, wenn er per IP, `localhost` oder den Rechnernamen angesprochen wird, und nur Anfragen von seiner eigenen Seite -- der Proxy verbirgt den Ursprung des Browsers vor Ollama, also prüft der Leitstand ihn. Jeder im Heimnetz kann die gespeicherten Chats lesen.
