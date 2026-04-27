#!/usr/bin/env bash
# Deploy TAGH Dev to the staging server (devops.staging-ami.com on 165.227.228.113).
#
# Idempotent: re-running updates code, rebuilds changed images, and restarts
# containers. Safe to re-run after partial failures.
#
# Requirements on your laptop:
#   - SSH key already authorized as root@165.227.228.113
#   - /home/web/vhosts/openclow/app/ exists on server OR your key is authorized
#     for `web` (we clone as web via sudo)
#   - Local .env + auth.json present at the repo root (this script refuses
#     without them — secrets come from here)
#
# Usage: bash scripts/deploy-staging.sh [--skip-build]

set -euo pipefail

SERVER=${SERVER:-165.227.228.113}
DOMAIN=${DOMAIN:-devops.staging-ami.com}
REMOTE_DIR=${REMOTE_DIR:-/home/web/vhosts/openclow}
REPO_URL=${REPO_URL:-https://github.com/AhmedMAfana/openclaw.git}
BRANCH=${BRANCH:-undocker}
CERTBOT_EMAIL=${CERTBOT_EMAIL:-admin@staging-ami.com}
SKIP_BUILD=${SKIP_BUILD:-0}

[[ ${1:-} == --skip-build ]] && SKIP_BUILD=1

HERE=$(cd "$(dirname "$0")/.." && pwd)
cd "$HERE"

# Sanity: secrets present on laptop
for f in .env auth.json; do
  [[ -f $f ]] || { echo "error: $HERE/$f is required (source of truth for secrets)"; exit 1; }
done

# Sanity: built frontend bundle present on laptop. The prod compose
# bind-mounts ./chat_frontend/dist into the api container read-only
# (docker-compose.prod.yml:46), so an empty / missing host dir would mask
# the image's baked bundle and 404 every /chat/ asset. We build locally
# (or in the worker container — see README) and rsync the result up in
# step 3.5 below.
if ! [[ -f chat_frontend/dist/index.html ]]; then
  echo "error: chat_frontend/dist/index.html missing — build the React frontend first."
  echo "  if your laptop has Node:    (cd chat_frontend && npm ci && npm run build)"
  echo "  if your laptop has no Node: build inside the worker container, e.g."
  echo "    docker compose exec worker bash -c 'cd /app/chat_frontend && npm install && npm run build'"
  echo "    docker cp tagh-devops-worker-1:/app/chat_frontend/dist/. chat_frontend/dist/"
  exit 1
fi

# Sanity: server reachable as root
ssh -o BatchMode=yes -o ConnectTimeout=5 "root@$SERVER" 'true' \
  || { echo "error: cannot SSH root@$SERVER — add your key first"; exit 1; }

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

# ── 1. Free disk + install Docker (idempotent) ───────────────────────────────
say "1/6  docker + swap + base deps"
ssh "root@$SERVER" bash -s <<'REMOTE'
set -euo pipefail

# Only touch things that aren't already in place.
if ! command -v docker >/dev/null; then
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi

# Ensure `web` is in the docker group.
if ! id -nG web | grep -qw docker; then
  usermod -aG docker web
  echo "added web to docker group — web must reconnect for it to take effect"
fi

# 2 GB swap if none exists (protects against OOM during agent bursts).
if ! swapon --show | grep -q '^/swapfile'; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -q vm.swappiness=10
fi

# Docker log rotation — keep json logs from eating disk over weeks.
if ! [[ -f /etc/docker/daemon.json ]] || ! grep -q '"max-size"' /etc/docker/daemon.json; then
  mkdir -p /etc/docker
  cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "3" }
}
JSON
  systemctl restart docker
fi

# Light disk cleanup — non-destructive.
apt-get clean
journalctl --vacuum-time=14d >/dev/null 2>&1 || true

df -h / | tail -1
REMOTE

# ── 2. Ensure deploy dir + git clone/pull ────────────────────────────────────
say "2/6  sync repo → $REMOTE_DIR/app  (branch: $BRANCH)"
ssh "root@$SERVER" bash -s <<REMOTE
set -euo pipefail
install -d -m 0755 -o web -g web "$REMOTE_DIR" "$REMOTE_DIR/backups"
sudo -u web bash <<EOSU
  set -euo pipefail
  if [[ -d "$REMOTE_DIR/app/.git" ]]; then
    cd "$REMOTE_DIR/app"
    # Explicit refspec so the remote-tracking ref exists after a shallow
    # fetch — without ":refs/remotes/origin/<branch>" only FETCH_HEAD is
    # set and "git reset --hard origin/<branch>" fails for new branches.
    git fetch --depth=1 origin "+$BRANCH:refs/remotes/origin/$BRANCH"
    git reset --hard "origin/$BRANCH"
  else
    git clone --depth=1 --branch "$BRANCH" "$REPO_URL" "$REMOTE_DIR/app"
  fi
  cd "$REMOTE_DIR/app"
  # Ensure the dir exists with web ownership; we'll populate the bundle in
  # the next step via rsync-from-laptop.
  install -d -m 0755 chat_frontend/dist
EOSU
REMOTE

# ── 2.5 Ship the freshly-built React bundle to the server ────────────────────
say "2.5/6  rsync chat_frontend/dist → server (built locally — see prod compose:46)"
# `--delete` so removed assets on the new bundle don't linger on the server.
# The prod compose mounts ./chat_frontend/dist:ro into the api container; an
# empty dir would mask the image's baked fallback and 404 the entire /chat/.
# We rsync as root (which can overwrite any existing file regardless of
# ownership from prior partial deploys), then chown the result to web so
# the api container reads it under its expected uid.
rsync -az --delete chat_frontend/dist/ "root@$SERVER:$REMOTE_DIR/app/chat_frontend/dist/"
ssh "root@$SERVER" "chown -R web:web '$REMOTE_DIR/app/chat_frontend/dist'"

# ── 3. Push local secrets ────────────────────────────────────────────────────
say "3/6  scp .env + auth.json → server (600, owned by web)"
scp -q .env auth.json "root@$SERVER:$REMOTE_DIR/app/"
ssh "root@$SERVER" "chown web:web '$REMOTE_DIR/app/.env' '$REMOTE_DIR/app/auth.json' \
  && chmod 600 '$REMOTE_DIR/app/.env' \
  && chmod 644 '$REMOTE_DIR/app/auth.json'"

# ── 4. Build + migrate + up ──────────────────────────────────────────────────
say "4/6  docker compose build + migrate + up"
ssh "root@$SERVER" bash -s <<REMOTE
set -euo pipefail
cd "$REMOTE_DIR/app"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

\$COMPOSE pull postgres redis dozzle
if [[ "$SKIP_BUILD" != "1" ]]; then
  # Build *every* service that has a build: stanza, not just api/worker/bot.
  # The migrate service has its own image (app-migrate) because it inherits
  # the *app YAML anchor's build. Skipping it leaves a stale image frozen
  # at whatever migrations existed last time it was built — alembic then
  # silently no-ops because it can't see the new revision files. (Same goes
  # for the setup service.) This bit us once; don't let it bite again.
  \$COMPOSE build api worker bot migrate setup
fi

# Start data services first, wait for healthcheck.
\$COMPOSE up -d postgres redis

# Run migrations to completion.
\$COMPOSE run --rm migrate

# App services.
\$COMPOSE up -d api bot worker dozzle

\$COMPOSE ps
REMOTE

# ── 5. nginx vhost + TLS via certbot ─────────────────────────────────────────
say "5/6  nginx reverse proxy + certbot SSL for $DOMAIN"
ssh "root@$SERVER" bash -s <<REMOTE
set -euo pipefail
CONF=/etc/nginx/sites-available/$DOMAIN
if ! [[ -f \$CONF ]]; then
  cat > "\$CONF" <<'NGINX'
server {
    listen 80;
    server_name $DOMAIN;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl http2;
    server_name $DOMAIN;

    # certbot --nginx will inject ssl_certificate/... lines on first run.

    client_max_body_size 25m;
    access_log /var/log/nginx/openclow.access.log;
    error_log  /var/log/nginx/openclow.error.log;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;

        # Streaming endpoints (/api/assistant) send chunked Claude responses.
        # Without these, nginx accumulates the FULL upstream body into its own
        # buffer before flushing to the client — so the user sees nothing for
        # 5-15 seconds, then the entire reply lands in one chunk, killing the
        # token-by-token streaming UX. Keep these off for the whole vhost; the
        # static asset perf impact is negligible (the browser still caches).
        proxy_buffering off;
        proxy_cache off;
        proxy_request_buffering off;
        chunked_transfer_encoding on;
    }
}
NGINX
  ln -sf "\$CONF" /etc/nginx/sites-enabled/$DOMAIN
fi
nginx -t
systemctl reload nginx

if ! [[ -d /etc/letsencrypt/live/$DOMAIN ]]; then
  certbot --nginx -d $DOMAIN --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --redirect
fi
REMOTE

# ── 6. Smoke test ────────────────────────────────────────────────────────────
say "6/6  smoke test"
code=$(curl -sS -o /dev/null -w '%{http_code}' "https://$DOMAIN/chat/" || true)
echo "GET https://$DOMAIN/chat/ → $code"
ssh "root@$SERVER" "ss -tlnp | awk 'NR==1 || /:(8000|5432|6379|9999)/'"
ssh "root@$SERVER" "cd $REMOTE_DIR/app && docker compose -f docker-compose.yml -f docker-compose.prod.yml ps"

# What did we actually ship?
deployed_sha=$(ssh "root@$SERVER" "cd $REMOTE_DIR/app && git rev-parse --short HEAD")
deployed_msg=$(ssh "root@$SERVER" "cd $REMOTE_DIR/app && git log -1 --pretty=format:'%s'")

echo
echo "✓ deploy complete — https://$DOMAIN/chat/"
echo "  branch:   $BRANCH"
echo "  commit:   $deployed_sha — $deployed_msg"
