# Leitstand

Weboberfläche für powermaxx: Ollama, ComfyUI und die GPU im Browser.

- **GPU**: Auslastung, VRAM (aufgeteilt nach Ollama, ComfyUI und sonstigen Prozessen), Temperatur, Leistung, Takt, CPU, RAM; Verläufe der letzten 10 Minuten
- **Ollama**: geladene Modelle (Größe, GPU-/CPU-Anteil, Kontext, Restzeit bis zum Entladen), installierte Modelle, Laden/Entladen
- **ComfyUI**: Modelle im Speicher, Dateien des letzten Workflows, Warteschlange, Speicher freigeben
- **Dienste**: Status, Starten/Stoppen/Neustart, Log
- **Chat** (`/chat/`, Knopf oben rechts): ein Web-Chat mit den Ollama-Modellen -- Streaming, Chat-Verlauf, System-Prompt, Temperatur und Kontextlänge pro Chat, Thinking-Modelle, Anhänge (Text, Code, PDF über `pdftotext`, Bilder für Vision-Modelle), Modelle herunterladen, Export als Markdown. Die Chats liegen als eine JSON-Datei pro Chat in `~/.local/share/ollama-chat/`

Aufruf: <http://192.168.178.102:8100>

## Aufbau

- `server.py`: Python-Standardbibliothek, keine Abhängigkeiten. Liest `nvidia-smi`, fragt die APIs von Ollama (11434) und ComfyUI (8188) ab und ruft `systemctl` auf
- `static/`: die Seite (HTML, CSS, JS ohne Build). Wird bei jedem Abruf von der Platte gelesen, Änderungen gelten nach einem Neuladen im Browser
- `chat.py` + `static/chat.html`: der Chat unter `/chat/` (hervorgegangen aus `~/Documents/devel/ollama-chat`): leitet `/chat/api/*` an Ollama weiter, speichert die Chats, wandelt PDFs in Text
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

Für den Chat gilt dasselbe (alles außer GET braucht den Header), und zusätzlich: Er antwortet nur, wenn er per IP, `localhost` oder den Rechnernamen angesprochen wird, und nur Anfragen von seiner eigenen Seite -- der Proxy verbirgt den Ursprung des Browsers vor Ollama, also prüft der Leitstand ihn. Jeder im Heimnetz kann die gespeicherten Chats lesen.
