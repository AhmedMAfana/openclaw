# Laravel + Vue install/boot pipeline (run by projctl inside the `app` container)
#
# What this image already does for you (don't repeat in projctl steps):
#   - php-fpm + nginx are supervised by s6-overlay; both start with the
#     container. No `start-php` step needed.
#   - The `node` service in compose runs `npm ci && npm run dev` itself
#     (its compose `command:` block). Do not run npm from this guide.
#
# What projctl owns: project-level deps + DB migrations + queue worker.

## install-php

```projctl
cmd: composer install --no-interaction --prefer-dist
cwd: /var/www/html
success_check: test -d /var/www/html/vendor
skippable: false
max_attempts: 3
retry_policy: exponential_backoff
timeout_seconds: 600
```

Installs PHP dependencies via Composer. Required before `artisan migrate`.
Reads `COMPOSER_AUTH` env (set by the orchestrator from /app/auth.json) for
private VCS auth.

## migrate

```projctl
cmd: php artisan migrate --force
cwd: /var/www/html
success_check: php artisan migrate --pretend > /dev/null
skippable: false
max_attempts: 2
timeout_seconds: 120
```

Runs DB migrations against the per-instance MySQL.

## seed

```projctl
cmd: php artisan db:seed --force 2>&1 | tee /tmp/seed.log; grep -qiE "duplicate entry|already exists|integrity constraint" /tmp/seed.log && echo "seed already applied — treating as success" || tail -1 /tmp/seed.log
cwd: /var/www/html
success_check: test 1 -eq 1
skippable: true
max_attempts: 1
timeout_seconds: 300
```

Seeds the per-instance MySQL with development data via the project's
`Database\Seeders\DatabaseSeeder`. Idempotent at the step level: a
fresh DB seeds normally; a re-run on an already-seeded DB hits unique
constraints, the grep catches that and the step still exits 0. Real
seeder errors (syntax, missing model, etc.) bubble up because the
shell pipeline returns the exit code of the last command (the grep
or tail). Projects without seeders see "seeding database" → 0 rows
seeded → exit 0 — also a no-op success. `success_check` is a
tautology because `db:seed` has no observable idempotent signal;
projects that want stricter verification can override it with a
project-specific assertion (e.g.
`php artisan tinker --execute='exit(\App\Models\User::count() ? 0 : 1)'`).

## storage-link

```projctl
cmd: php artisan storage:link
cwd: /var/www/html
success_check: test -L /var/www/html/public/storage
skippable: true
max_attempts: 1
timeout_seconds: 15
```

Creates the `public/storage` → `storage/app/public` symlink that
Laravel apps using file uploads expect. Skippable for projects that
don't use the public disk. Idempotent — Laravel's command no-ops if
the symlink already exists.

# `start-queue` was removed from this guide. Each projctl step runs in
# its own shell, so backgrounding the queue:work process here doesn't
# survive the step's exit. Projects that need a long-running queue
# worker should add a separate `queue:` service to their compose
# (image: same as app, command: `php artisan queue:work`) — not the
# platform's job to supervise application processes.
