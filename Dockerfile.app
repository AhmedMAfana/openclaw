# Build frontend
FROM node:20-alpine AS frontend
WORKDIR /chat_frontend
COPY chat_frontend/package*.json ./
RUN npm ci
COPY chat_frontend/ ./
RUN npm run build

# Python app + Claude Agent runtime
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ffmpeg ca-certificates gnupg procps \
    && curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$(dpkg --print-architecture) \
       -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared \
    && rm -rf /var/lib/apt/lists/*

# NVM + Node.js — agents can run `nvm install X && nvm use X` to switch versions
ENV NVM_DIR=/usr/local/nvm
ENV NODE_VERSION=20
RUN mkdir -p $NVM_DIR \
    && curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash \
    && . $NVM_DIR/nvm.sh \
    && nvm install $NODE_VERSION \
    && nvm alias default $NODE_VERSION \
    && node_ver=$(ls $NVM_DIR/versions/node/) \
    && ln -sf $NVM_DIR/versions/node/$node_ver/bin/node /usr/local/bin/node \
    && ln -sf $NVM_DIR/versions/node/$node_ver/bin/npm /usr/local/bin/npm \
    && ln -sf $NVM_DIR/versions/node/$node_ver/bin/npx /usr/local/bin/npx \
    && printf 'export NVM_DIR=%s\n[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"\n' \
       "$NVM_DIR" > /etc/profile.d/nvm.sh && chmod +x /etc/profile.d/nvm.sh

# Claude Code CLI + Playwright MCP (same versions as worker)
RUN npm install -g @anthropic-ai/claude-code@latest @playwright/mcp@latest

# Playwright browsers — shared path accessible by any user
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN npx playwright install --with-deps chromium

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY scripts/ ./scripts/

# Copy built frontend from Node stage
COPY --from=frontend /chat_frontend/dist ./chat_frontend/dist

RUN pip install --no-cache-dir -e .

RUN useradd -m openclow \
    && mkdir -p /home/openclow/.claude /app/logs \
    && chown -R openclow:openclow /app /home/openclow $NVM_DIR
# Note on /app/logs: explicit mkdir + chown so docker-compose's
# `activity_logs` named volume inherits openclow:openclow on FIRST
# creation. Once the volume exists, docker never re-syncs perms — so
# pre-existing volumes stay with whatever uid/gid they had. The deploy
# script does a one-shot chown of the existing volume to handle that.

COPY scripts/api-entrypoint.sh /usr/local/bin/api-entrypoint.sh
RUN chmod +x /usr/local/bin/api-entrypoint.sh

USER openclow

ENTRYPOINT ["api-entrypoint.sh"]
CMD ["uvicorn", "openclow.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
