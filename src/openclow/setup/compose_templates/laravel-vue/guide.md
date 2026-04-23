## install-php

```projctl
cmd: composer install --no-interaction --prefer-dist
cwd: /app
success_check: test -d /app/vendor
skippable: false
max_attempts: 3
retry_policy: exponential_backoff
timeout_seconds: 600
```

Installs PHP dependencies via Composer. Required before `artisan migrate`.

## install-node

```projctl
cmd: npm ci
cwd: /app
success_check: test -d /app/node_modules
skippable: false
max_attempts: 2
retry_policy: exponential_backoff
timeout_seconds: 900
```

Installs front-end deps for Vite. npm ci is strict about lockfile
consistency — if it fails, the lockfile is the usual suspect.

## migrate

```projctl
cmd: php artisan migrate --force
cwd: /app
success_check: php artisan migrate:status | grep -q "Ran"
skippable: false
max_attempts: 2
timeout_seconds: 120
```

Runs DB migrations against the per-instance MySQL.

## start-queue

```projctl
cmd: php artisan queue:work --daemon &
cwd: /app
success_check: pgrep -f "queue:work" > /dev/null
skippable: true
max_attempts: 1
timeout_seconds: 10
```

Starts the Laravel queue worker. Skippable for projects without queues.

## start-php

```projctl
cmd: php-fpm -D
cwd: /app
success_check: pgrep -f php-fpm > /dev/null
skippable: false
max_attempts: 2
timeout_seconds: 15
```

Starts php-fpm so the nginx `web` service can reach it at app:9000.

## start-node

```projctl
cmd: npm run dev -- --host 0.0.0.0 &
cwd: /app
success_check: curl -sf http://localhost:5173 > /dev/null
skippable: false
max_attempts: 2
timeout_seconds: 60
```

Starts Vite dev server. Must stay up for HMR to reach the browser via
the cloudflared sidecar (see spec §5.4).
