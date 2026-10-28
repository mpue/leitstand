#!/bin/sh
# SearXNG fuer die Internet-Recherche des Chats: eine eigene Metasuchmaschine
# als Docker-Container, nur auf diesem Rechner erreichbar (127.0.0.1:8888),
# mit JSON-Ausgabe, die der Leitstand abfragt (websearch.py). Startet mit
# Docker von selbst. Ohne sudo, wenn dein Benutzer in der Gruppe docker ist.
#
#   sh searxng.sh            einrichten oder aktualisieren
#   docker rm -f searxng     wieder entfernen
set -e

CONF=${XDG_CONFIG_HOME:-$HOME/.config}/leitstand/searxng
PORT=${SEARXNG_PORT:-8888}
IMAGE=${SEARXNG_IMAGE:-docker.io/searxng/searxng:latest}

mkdir -p "$CONF"
if [ ! -f "$CONF/settings.yml" ]; then
    KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
    cat > "$CONF/settings.yml" <<EOF
# SearXNG des Leitstands (searxng.sh). Nur fuer diesen Rechner: kein Limiter,
# und JSON fuer die Abfragen des Chats.
use_default_settings: true
server:
  secret_key: "$KEY"
  limiter: false
  image_proxy: false
  base_url: "http://127.0.0.1:$PORT/"
search:
  safe_search: 0
  formats:
    - html
    - json
ui:
  default_locale: de
EOF
fi

docker pull "$IMAGE"
docker rm -f searxng >/dev/null 2>&1 || true
docker run -d --name searxng --restart unless-stopped \
    -p "127.0.0.1:$PORT:8080" \
    -v "$CONF:/etc/searxng" \
    "$IMAGE"

echo "Warte auf SearXNG..."
for i in $(seq 1 60); do
    if curl -fs "http://127.0.0.1:$PORT/search?q=test&format=json" >/dev/null 2>&1; then
        echo "SearXNG laeuft: http://127.0.0.1:$PORT"
        exit 0
    fi
    sleep 1
done
echo "SearXNG antwortet nicht -- docker logs searxng" >&2
exit 1
