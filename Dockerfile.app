# Build frontend
FROM node:20-alpine AS frontend
WORKDIR /chat_frontend
COPY chat_frontend/package*.json ./
RUN npm ci
COPY chat_frontend/ ./
RUN npm run build

# Python app
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ffmpeg \
    && curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$(dpkg --print-architecture) \
       -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/
COPY alembic.ini ./
COPY alembic/ ./alembic/
COPY scripts/ ./scripts/

# Copy built frontend from Node stage
COPY --from=frontend /chat_frontend/dist ./chat_frontend/dist

RUN pip install --no-cache-dir -e .

RUN useradd -m openclow && chown -R openclow:openclow /app
USER openclow

CMD ["python", "-m", "uvicorn", "openclow.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
