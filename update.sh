#!/usr/bin/env bash
# Frissítés a GitHubról: legújabb kód letöltése, konténer újraépítése és újraindítása.
# Használat a szerveren:  /opt/hazikonyvtar/update.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "== Kód frissítése (git pull)"
git pull --ff-only

echo "== Konténer újraépítése és indítása"
docker compose up -d --build

echo "== Régi, nem használt image-ek takarítása"
docker image prune -f >/dev/null

echo "== Állapot"
sleep 15
docker ps --filter name=hazikonyvtar --format '{{.Names}}: {{.Status}}'
docker logs --tail 5 hazikonyvtar
