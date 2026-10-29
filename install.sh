#!/bin/sh
# Richtet den Leitstand ein (mit sudo ausfuehren):
#  - leitstand.service: der Webserver auf Port 8100, laeuft als dein Benutzer, startet beim Booten
#  - comfyui.service:   ComfyUI als Dienst (ohne Autostart), lauscht im LAN auf 8188
#  - acestep.service:   ACE-Step 1.5 (Musik) als Dienst, ohne Autostart, nur lokal auf 8001;
#                       der Musik-Schalter im Chat startet und stoppt ihn
#  - polkit-Regel:      dein Benutzer darf ollama/comfyui/acestep ohne Passwort starten/stoppen
#  - Leitstand-Node in ComfyUI/custom_nodes (meldet die geladenen Modelle)
#  - ufw-Regeln fuer 8100 und 8188 aus dem Heimnetz (greifen, sobald ufw aktiv ist)
set -e

if [ "$(id -u)" != 0 ]; then
    echo "Bitte mit sudo ausfuehren: sudo sh $0" >&2
    exit 1
fi

USER_NAME=${SUDO_USER:?SUDO_USER fehlt, bitte per sudo starten}
HERE=$(cd "$(dirname "$0")" && pwd)
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
COMFY_DIR=${COMFY_DIR:-$USER_HOME/Documents/devel/ComfyUI}
ACE_DIR=${ACE_DIR:-$USER_HOME/Documents/devel/ACE-Step-1.5}
LAN=${LAN:-192.168.178.0/24}

cat > /etc/systemd/system/leitstand.service <<EOF
[Unit]
Description=Leitstand: Ollama, ComfyUI und GPU im Browser
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
ExecStart=/usr/bin/python3 $HERE/server.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/comfyui.service <<EOF
[Unit]
Description=ComfyUI
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$COMFY_DIR
ExecStart=$COMFY_DIR/venv/bin/python main.py --listen 0.0.0.0 --port 8188
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

if [ -x "$ACE_DIR/.venv/bin/acestep-api" ]; then
cat > /etc/systemd/system/acestep.service <<EOF
[Unit]
Description=ACE-Step 1.5 (Musik)
After=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$ACE_DIR
ExecStart=$ACE_DIR/.venv/bin/acestep-api --host 127.0.0.1 --port 8001
Environment=PYTHONUNBUFFERED=1
Environment=ACESTEP_INIT_LLM=auto
Environment=ACESTEP_LM_MODEL_PATH=acestep-5Hz-lm-1.7B
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
fi

cat > /etc/polkit-1/rules.d/50-leitstand.rules <<EOF
// Leitstand: $USER_NAME darf Ollama, ComfyUI und ACE-Step ohne Passwort starten/stoppen.
polkit.addRule(function (action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" && subject.user == "$USER_NAME") {
        var unit = action.lookup("unit"), verb = action.lookup("verb");
        if ((unit == "ollama.service" || unit == "comfyui.service" || unit == "acestep.service") &&
            (verb == "start" || verb == "stop" || verb == "restart")) {
            return polkit.Result.YES;
        }
    }
});
EOF
chmod 644 /etc/polkit-1/rules.d/50-leitstand.rules

if [ -d "$COMFY_DIR/custom_nodes" ]; then
    ln -sfn "$HERE/comfy_node/leitstand_status.py" "$COMFY_DIR/custom_nodes/leitstand_status.py"
fi

if command -v ufw >/dev/null; then
    ufw allow from "$LAN" to any port 8100 proto tcp comment 'Leitstand LAN'
    ufw allow from "$LAN" to any port 8188 proto tcp comment 'ComfyUI LAN'
fi

systemctl daemon-reload
systemctl enable --now leitstand.service
systemctl restart leitstand.service
echo "Leitstand laeuft: http://$(hostname -I | cut -d' ' -f1):8100"
