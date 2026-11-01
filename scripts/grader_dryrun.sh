#!/usr/bin/env sh
# Simulate the grader's exact steps from a clean clone (Linux/macOS/CI; needs git, docker, curl).
#
#   scripts/grader_dryrun.sh [REPO] [ENV_FILE]
#     REPO      git URL or path to clone (default: this repository, i.e. its committed HEAD)
#     ENV_FILE  .env to use (default: ./.env)
#   PORT=8000 KEEP=1 CA_BUNDLE=path/to/bundle.pem  (optional environment overrides)
#
# Steps: 1 git clone -> 2 copy .env -> 3 docker build -t chatbot:1.0 . ->
#        4 docker run -p PORT:PORT --env-file .env chatbot:1.0 -> 5 open localhost:PORT
# then a smoke question inside the container must return a reply with Sources and a page.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
REPO=${1:-$ROOT}
ENV_FILE=${2:-$ROOT/.env}
PORT=${PORT:-8000}
NAME=rag-grader-dryrun
WORK=$(mktemp -d)

cleanup() {
    if [ "${KEEP:-0}" != "1" ]; then docker rm -f "$NAME" >/dev/null 2>&1 || true; fi
    rm -rf "$WORK"
}
trap cleanup EXIT

echo "[1/5] git clone $REPO"
git clone --quiet "$REPO" "$WORK/repo"
cd "$WORK/repo"

echo "[2/5] fill .env from $ENV_FILE"
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (copy .env.example and fill it in)"; exit 1; }
cp "$ENV_FILE" .env

echo "[3/5] docker build -t chatbot:1.0 ."
if [ -n "${CA_BUNDLE:-}" ]; then
    docker build --secret "id=ca_bundle,src=$CA_BUNDLE" -t chatbot:1.0 .
else
    docker build -t chatbot:1.0 .
fi

echo "[4/5] docker run -p $PORT:$PORT --env-file .env chatbot:1.0"
docker rm -f "$NAME" >/dev/null 2>&1 || true
if [ -n "${CA_BUNDLE:-}" ]; then
    docker run -d --name "$NAME" -p "$PORT:$PORT" --env-file .env -e "APP_PORT=$PORT" \
        -v "$CA_BUNDLE:/etc/ssl/certs/dev-ca-bundle.pem:ro" \
        -e SSL_CERT_FILE=/etc/ssl/certs/dev-ca-bundle.pem chatbot:1.0 >/dev/null
else
    docker run -d --name "$NAME" -p "$PORT:$PORT" --env-file .env -e "APP_PORT=$PORT" chatbot:1.0 >/dev/null
fi

echo "[5/5] open http://localhost:$PORT"
i=0
until curl -fsS "http://localhost:$PORT/_stcore/health" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -gt 60 ]; then echo "not healthy after 120 s"; docker logs "$NAME" | tail -n 40; exit 1; fi
    sleep 2
done
curl -fsS "http://localhost:$PORT/" | grep -q "<title>" && echo "UI is served on http://localhost:$PORT"

echo "[smoke] one question through the real UI inside the container"
docker exec -i "$NAME" python - < "$ROOT/scripts/smoke_ui.py"
docker image inspect chatbot:1.0 --format 'image size: {{.Size}} bytes'
echo "DRY RUN PASSED"
