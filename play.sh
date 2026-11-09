#!/usr/bin/env bash
# Opens the audiobook.
#   ./play.sh                  native player (GTK + GStreamer, low power)  <- default
#   ./play.sh --web [browser]  the older browser player (starts a tiny local server; Firefox by default)
#   ./play.sh --stop           stop the browser player's server
# Both players share progress.json (position, bookmarks, history).
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

if [ "$1" != "--web" ] && [ "$1" != "--stop" ]; then
  exec /usr/bin/python3 "$DIR/player/gradplayer.py"
fi

PORT="${AUDIOBOOK_PORT:-8765}"
URL="http://127.0.0.1:$PORT/player/index.html"
running() { curl -s -o /dev/null -m 1 "http://127.0.0.1:$PORT/api/ping"; }

if [ "$1" = "--stop" ]; then
  pkill -f "$DIR/player/server.py" && echo "stopped" || echo "not running"; exit 0
fi
shift   # --web

if ! running; then
  nohup python3 "$DIR/player/server.py" "$PORT" >/dev/null 2>&1 &
  for _ in $(seq 1 30); do running && break; sleep 0.1; done
fi
if ! running; then
  echo "Server did not start (port $PORT busy?). Opening the player straight from disk instead;"
  echo "progress will then be kept in the browser only."
  URL="file://$DIR/player/index.html"
fi

if [ -n "$1" ]; then exec "$1" "$URL"; fi
# Firefox by default: Epiphany (WebKitGTK) played this book as pure silence on this machine.
if command -v firefox >/dev/null 2>&1; then exec firefox --new-window "$URL" >/dev/null 2>&1; fi
exec xdg-open "$URL" >/dev/null 2>&1
