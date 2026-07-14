#!/usr/bin/env bash
# Redeploy Healtify — Django backend + pgvector + Vite/React frontend (nginx).
# The main compose serves the PREBUILT ./frontend/dist via nginx, so the
# frontend must be built first. This script builds it in a throwaway Node
# container (no host Node needed), then brings the stack up.
#
# Usage: ./redeploy.sh
set -euo pipefail
cd "$(dirname "$0")"

# Required mounts must exist or Docker will create them as empty dirs.
[ -f training/.env ] || { echo "==> [healtify] creating training/.env from example"; cp training/.env.example training/.env; }

echo "==> [healtify] Building frontend (Vite) into ./frontend/dist ..."
docker run --rm -v "$PWD/frontend":/app -w /app node:20-alpine \
  sh -c "npm ci && npm run build"

echo "==> [healtify] Rebuilding & restarting containers..."
docker compose up -d --build

# Attach nginx to the shared reverse-proxy edge network so healthify.twenti.studio
# routes here (by container name only, to avoid DNS collisions). Refresh the proxy.
echo "==> [healtify] Wiring into public reverse proxy (edge network)..."
docker network connect sim-rumah-maggot_maggot healtify_nginx 2>/dev/null && echo "   connected healtify_nginx" || echo "   healtify_nginx already attached"
docker exec sim-rumah-maggot-web-1 nginx -s reload 2>/dev/null && echo "   proxy reloaded" || echo "   (proxy reload skipped)"

echo "==> [healtify] Waiting for services..."
sleep 6
docker compose ps
echo "==> [healtify] Health check:"
curl -fsS -m 5 -o /dev/null -w "  nginx :8090 -> HTTP %{http_code}\n" http://127.0.0.1:8090/ || echo "  (not ready yet — check: docker compose logs -f backend)"
echo "==> [healtify] Done. App: http://127.0.0.1:8090 (localhost only — front with reverse proxy)"
