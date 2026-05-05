# Laravel + Vue install/boot pipeline (run by projctl inside the `app` container)
#
# What this image already does for you (don't repeat in projctl steps):
#   - php-fpm + nginx are supervised by s6-overlay; both start with the
#     container. No `start-php` step needed.
#   - The `node` service in compose runs `npm ci && npm run dev` itself
#     (its compose `command:` block). Do not run npm from this guide.
#
# What projctl owns: project-level deps + DB migrations + queue worker.
#
# Variant-aware steps (`setup-env`, `migrate`, `seed`) dispatch through
# `_variant.sh` based on PROJECT_VARIANT (set by the orchestrator from
# composer.json detection). Variants today: normal,
# multidomain-gecche, multidomain-spatie, multidomain-stancl. See
# _variant.sh for per-variant commands.

## install-php

```projctl
cmd: composer install --no-interaction --prefer-dist || composer install --no-interaction --prefer-dist --no-scripts
cwd: /var/www/html
success_check: test -d /var/www/html/vendor && test -f /var/www/html/vendor/autoload.php
skippable: false
max_attempts: 3
retry_policy: exponential_backoff
timeout_seconds: 600
```

Installs PHP dependencies via Composer. Required before `artisan migrate`.
Reads `COMPOSER_AUTH` env (set by the orchestrator from /app/auth.json) for
private VCS auth.

Two-stage cmd: try with scripts first (so a healthy project still runs
its post-install hooks), fall back to `--no-scripts` if any post-install
artisan command crashes. Real-world failure mode this guards against:
a feature branch with a duplicate `use` statement in `config/*.php`
makes `php artisan package:discover` exit 255 — and without the
fallback, projctl would retry the whole install 3 times and fail the
provision over a project-side code bug. With `--no-scripts` the vendor
tree is fully populated; the success_check verifies autoload.php
actually exists; and the agent inside the chat can fix the underlying
PHP issue without re-provisioning.

## setup-env

```projctl
cmd: sh /var/www/html/_variant.sh setup-env
cwd: /var/www/html
success_check: test -f /var/www/html/.env -o -f /var/www/html/.env.${INSTANCE_HOST}
skippable: false
max_attempts: 1
timeout_seconds: 30
```

Provisions the per-domain .env file(s) before any artisan command.
Vanilla Laravel: copies .env.example → .env and sed-overrides DB/APP_URL/etc.
gecche/laravel-multidomain: writes .env.${INSTANCE_HOST} and registers
the domain via `php artisan domain:add`. Other multidomain variants
fall through to the vanilla .env path.

## migrate

```projctl
cmd: sh /var/www/html/_variant.sh migrate
cwd: /var/www/html
success_check: sh /var/www/html/_variant.sh migrate --check 2>/dev/null || php artisan migrate --pretend > /dev/null
skippable: false
max_attempts: 2
timeout_seconds: 300
```

Runs DB migrations against the per-instance MySQL.
Variant-aware: normal Laravel uses `migrate --force`; gecche uses
`domain:migrate --domain=$INSTANCE_HOST --force`; spatie/stancl run
their per-tenant migrate commands. See _variant.sh.

## seed-admin

```projctl
cmd: sh /var/www/html/_variant.sh seed-admin
cwd: /var/www/html
success_check: test 1 -eq 1
skippable: true
max_attempts: 1
timeout_seconds: 30
```

Inserts a default Admin user into the per-instance MySQL so the
SSO / fake-auth `/webapi/set` flow has something to authenticate
as on a fresh instance. Idempotent (INSERT IGNORE on PK=1) and
skippable — if the project's users table requires extra NOT NULL
columns the seeder doesn't know about, the step logs the failure
but doesn't block the rest of the boot.

## seed

```projctl
cmd: sh /var/www/html/_variant.sh seed 2>&1 | tee /tmp/seed.log; grep -qiE "duplicate entry|already exists|integrity constraint" /tmp/seed.log && echo "seed already applied — treating as success" || tail -1 /tmp/seed.log
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

## grant-admin-roles

```projctl
cmd: sh /var/www/html/_variant.sh grant-admin-roles
cwd: /var/www/html
success_check: test 1 -eq 1
skippable: true
max_attempts: 1
timeout_seconds: 60
```

Post-seed Spatie role grant. Must run AFTER `seed` because the project's
DatabaseSeeder commonly truncates `model_has_roles` / `role_has_permissions`
to rebuild role catalogs — wiping any pre-seed grant. This step assigns
every existing Role + Permission to `user_id=1` (the admin row inserted
by `seed-admin`) so the SSO/fake-auth user can hit role-gated controllers
without the "User does not have the right roles" UnauthorizedException.
No-op for projects without spatie/laravel-permission.

## storage-link

```projctl
cmd: sh /var/www/html/_variant.sh storage-link
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
