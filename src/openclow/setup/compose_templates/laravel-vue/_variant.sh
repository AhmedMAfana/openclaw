#!/bin/sh
# Per-step dispatch for the laravel-vue template.
#
# Invoked from `guide.md` step `cmd:` lines as:
#   sh /var/www/html/_variant.sh <step-name>
#
# Picks commands based on PROJECT_VARIANT (set by the orchestrator
# from `_detect_project_variant(workspace)` — see
# `src/openclow/worker/tasks/instance_tasks.py`):
#
#   normal              — vanilla Laravel single-tenant
#   multidomain-gecche  — gecche/laravel-multidomain (per-domain .env files,
#                         `domain:add` + `domain:migrate` artisan commands)
#   multidomain-spatie  — spatie/laravel-multitenancy
#   multidomain-stancl  — stancl/tenancy
#
# Adding a new variant: add a new arm here AND add the package name →
# variant string mapping in `_VARIANT_PACKAGES` in instance_tasks.py.
# Those two are the ONLY places to extend.

set -e
STEP="$1"
VARIANT="${PROJECT_VARIANT:-normal}"

# Diagnostic capture: every step's full stdout+stderr (with `set -x` trace)
# is teed to /var/lib/projctl/_variant-logs/<step>.log. The orchestrator's
# _mark_failed handler copies that directory out of the projctl-state volume
# into /var/log/openclow/failed-instances/ BEFORE teardown wipes the volume.
# On failure, we also dump the last 60 lines to stderr so projctl's own
# stderrTail (saved into state.json by RecordFailure) carries the diagnostic.
# Without this, a failing step records exit_code only — no clue *why*.
_LOG_DIR="/var/lib/projctl/_variant-logs"
mkdir -p "$_LOG_DIR" 2>/dev/null || _LOG_DIR=/tmp
_LOG_FILE="$_LOG_DIR/${STEP}.log"
: > "$_LOG_FILE" 2>/dev/null || :
# POSIX sh has no process substitution — use plain redirection. All step
# output goes into $_LOG_FILE; on failure the EXIT trap dumps a tail to
# stderr (which projctl captures into state.json:last_stderr) so the
# diagnostic isn't lost.
exec >> "$_LOG_FILE" 2>&1
trap '_rc=$?; if [ "$_rc" -ne 0 ]; then
  exec >&2
  echo ""
  echo "=== _variant.sh FAILED step=$STEP variant=$VARIANT exit=$_rc ==="
  echo "--- env (filtered) ---"
  env | grep -E "^(INSTANCE_HOST|APP_KEY|DB_PASSWORD|PROJECT_VARIANT|COMPOSER_AUTH|GITHUB_TOKEN)=" | sed "s/=.*/=<set>/"
  echo "--- last 80 lines of $_LOG_FILE ---"
  tail -80 "$_LOG_FILE" 2>/dev/null || echo "(no log captured)"
  echo "=== end _variant.sh failure ==="
fi' EXIT
set -x

# Some projects override Laravel's environmentPath in bootstrap/app.php
# to read .env files from /var/www/html/envs/ instead of the root.
# Detect that and mirror every .env / .env.<host> file we write into
# both locations so env() resolves regardless of which path Laravel
# loads. (Caught on tagh-test 2026-04-29: env() returned null because
# the project sets `dirname(__DIR__).'/envs'` as the second arg to
# Application::__construct.)
# POSIX sh (busybox ash on alpine, dash on debian) has no arrays. Use a
# space-separated string and rely on word-splitting in `for d in $ENV_PATHS`.
# (Caught 2026-04-29: bash `ENV_PATHS=( "." )` blew up the whole script
# with "syntax error: unexpected (" before any case arm could run, so
# every multidomain provision failed at setup-env with exit 2 and no log.)
ENV_PATHS="."
if grep -qE "envs|environmentPath" bootstrap/app.php 2>/dev/null && [ -d envs ]; then
    ENV_PATHS=". envs"
fi
mirror_env() {
    src="$1"
    [ -f "$src" ] || return 0
    base="$(basename "$src")"
    for d in $ENV_PATHS; do
        target="$d/$base"
        [ "$(realpath "$src" 2>/dev/null)" = "$(realpath "$target" 2>/dev/null)" ] && continue
        cp -f "$src" "$target" 2>/dev/null || :
    done
}

# ── Helpers ─────────────────────────────────────────────────────────────────

# Idempotent upsert: if KEY exists in the .env file → sed-replace it.
# Otherwise append it. The sed-only pattern would silently miss keys
# that aren't in .env.example (e.g. PUSHER_* on a project whose example
# doesn't list them) and the resulting .env would never get the value.
upsert_env() {
    file="$1"
    key="$2"
    value="$3"
    if grep -qE "^${key}=" "$file" 2>/dev/null; then
        # Use a delimiter unlikely to appear in URLs / passwords / JSON.
        sed -i "s|^${key}=.*|${key}=${value}|" "$file" 2>/dev/null || :
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

# Apply per-instance infrastructure overrides to a .env file. Idempotent.
# Used by both the normal and gecche paths.
apply_infra_env() {
    target="$1"
    upsert_env "$target" APP_URL "https://${INSTANCE_HOST}"
    upsert_env "$target" APP_SHORT_URL "${INSTANCE_HOST}"
    upsert_env "$target" APP_ENV local
    upsert_env "$target" APP_DEBUG true
    upsert_env "$target" APP_KEY "${APP_KEY}"
    upsert_env "$target" DB_CONNECTION mysql
    upsert_env "$target" DB_HOST db
    upsert_env "$target" DB_PORT 3306
    upsert_env "$target" DB_DATABASE app
    upsert_env "$target" DB_USERNAME app
    upsert_env "$target" DB_PASSWORD "${DB_PASSWORD}"
    upsert_env "$target" REDIS_HOST redis
    upsert_env "$target" REDIS_PORT 6379
    upsert_env "$target" MAIL_HOST mailpit
    upsert_env "$target" MAIL_PORT 1025
    upsert_env "$target" MEILISEARCH_HOST "http://meilisearch:7700"
}

# Apply per-app config that EVERY instance needs regardless of variant
# (Pusher creds, AMI SSO endpoints). These are the values the user wired
# into the orchestrator so the deployed app can reach the staging
# Pusher cluster + AMI's SSO server for the /webapi/set fake-auth flow.
# Hardcoded here so a brand-new chat instance is functional out of the
# box; long-term these belong in platform_config so they're outside git.
apply_app_env() {
    target="$1"
    # Pusher (real-time messaging)
    upsert_env "$target" PUSHER_APP_ID 1755863
    upsert_env "$target" PUSHER_APP_KEY e315dd664caa7dedee07
    upsert_env "$target" PUSHER_APP_SECRET e315dd664caa7dedee07
    upsert_env "$target" PUSHER_SCHEME https
    upsert_env "$target" PUSHER_APP_CLUSTER ap2
    # AMI SSO endpoints — point at the new sso.tagh.uk deployment
    # (sso-new codebase). The legacy sso-back.staging-ami.com /
    # sso.staging-ami.com pair is deprecated. The /webapi/set fake-auth
    # flow now hits sso.tagh.uk/sso/dev-token, which validates the
    # Origin against an *.tagh.co.uk allowlist and mints a fresh
    # Passport token without any redirect dance.
    upsert_env "$target" AUTH_SERVER_URL "https://sso.tagh.uk/"
    upsert_env "$target" AUTH_FE_SERVER_URL "https://sso.tagh.uk/"
    upsert_env "$target" AUTH_SERVER_CLIENT_ID 0199bf2e-1a90-727e-b305-c71284ee9044
    upsert_env "$target" AUTH_SERVER_CLIENT_SECRET lFNaMPhHykAWL9Wpl3JKKKoWYvYrTR9gOcsrQIFI
    # serversideup/php image honors WWWUSER/WWWGROUP for the runtime user
    # (tagh-test's own docker-compose.yml uses these too).
    upsert_env "$target" WWWUSER 1000
    upsert_env "$target" WWWGROUP 1000
}

# gecche/laravel-multidomain setup. Per the package README:
#   - `domain:add <host>` creates `.env.<host>` AS A COPY OF `.env`
#     (NOT .env.example), and adds an entry to config/domain.php's
#     `domains` array.
#   - Migrations / seeds / any standard artisan command then take
#     `--domain=<host>` to pick which env file to read.
#
# Critical sequencing: `.env` must exist AND have the right
# infrastructure values BEFORE `domain:add` runs, because
# `domain:add` snapshots `.env` into `.env.<host>` at that moment.
gecche_setup_env() {
    if [ -z "$INSTANCE_HOST" ]; then
        echo "_variant.sh: INSTANCE_HOST not set — gecche setup-env can't proceed" >&2
        exit 3
    fi
    if [ ! -f .env ]; then
        if [ ! -f .env.example ]; then
            echo "_variant.sh: neither .env nor .env.example present — can't bootstrap" >&2
            exit 4
        fi
        cp .env.example .env
    fi
    # Apply infra + app config to .env BEFORE domain:add so they're
    # inherited by .env.<INSTANCE_HOST>.
    apply_infra_env .env
    apply_app_env .env
    # Register the domain. domain:add creates .env.<INSTANCE_HOST>
    # from .env. Tolerate a "domain already exists" failure (e.g. if
    # an earlier provision attempt left a stale config/domain.php
    # entry from the cloned repo).
    php artisan domain:add "${INSTANCE_HOST}" 2>&1 || \
        echo "  (domain:add returned non-zero — falling back to manual provisioning)"

    # FALLBACK: gecche's domain:add can fail silently for several reasons
    # (artisan boot error from broken project code, migrations not yet
    # run, file permission quirks, cached config, …). Without it,
    # config/domain.php has no mapping for INSTANCE_HOST and incoming
    # HTTP requests get an empty env() — every Laravel call that depends
    # on env vars (auth, db host, AUTH_SERVER_*) breaks.
    # Defence in depth: always ensure
    #   1) .env.<INSTANCE_HOST> exists with a copy of .env
    #   2) config/domain.php has a mapping for the host
    # so the project boots correctly even when gecche's command path is
    # bricked by some other layer.
    if [ ! -f ".env.${INSTANCE_HOST}" ]; then
        cp .env ".env.${INSTANCE_HOST}"
        echo "  fallback: created .env.${INSTANCE_HOST} from .env"
    fi
    # Mirror to /envs/ if the project uses a custom env path.
    mirror_env .env
    mirror_env ".env.${INSTANCE_HOST}"
    if [ -f config/domain.php ] && ! grep -q "'${INSTANCE_HOST}'" config/domain.php; then
        # Insert a mapping line into the 'domains' array. Use a sentinel
        # comment so the same instance can't be added twice on re-run.
        php -r '
            $f = "config/domain.php";
            $s = file_get_contents($f);
            $host = $argv[1];
            $key  = "tagh_dev_" . preg_replace("/[^a-z0-9]/i","_",$host);
            $line = "    \x27" . $host . "\x27 => \x27" . $key . "\x27, // tagh-platform-injected\n";
            $s = preg_replace(
                "/(\x27domains\x27\s*=>\s*\[\s*)/",
                "$1" . $line,
                $s,
                1
            );
            file_put_contents($f, $s);
        ' "${INSTANCE_HOST}" && echo "  fallback: registered ${INSTANCE_HOST} in config/domain.php"
    fi

    # Re-apply infra + app config to the per-domain file too. domain:add
    # may not propagate keys that weren't in .env at copy time (e.g. if
    # a previous run left a stale .env.<host>); upsert_env handles both
    # missing-key (append) and present-key (replace) paths.
    if [ -f ".env.${INSTANCE_HOST}" ]; then
        apply_infra_env ".env.${INSTANCE_HOST}"
        apply_app_env ".env.${INSTANCE_HOST}"
    fi
}

# Vanilla single-tenant Laravel needs a .env file at root for
# php-fpm + artisan to read. Most templates don't ship one.
normal_setup_env() {
    if [ ! -f .env ] && [ -f .env.example ]; then
        cp .env.example .env
    fi
    if [ -f .env ]; then
        apply_infra_env .env
        apply_app_env .env
        mirror_env .env
    fi
}

# ── Dispatch ────────────────────────────────────────────────────────────────

case "${STEP}:${VARIANT}" in

    # setup-env: prepare .env file(s) before any artisan command. Always
    # the first projctl step. Variant-specific because gecche needs a
    # per-domain .env.<host>, vanilla wants a single .env.
    setup-env:multidomain-gecche)
        gecche_setup_env
        ;;
    setup-env:*)
        normal_setup_env
        ;;

    # migrate: schema migrations. gecche extends EVERY standard artisan
    # command with `--domain=<host>` to pick which .env.<host> file to
    # read. So it's `php artisan migrate --domain=...` (NOT
    # `domain:migrate` which doesn't exist in this package).
    #
    # SHARED SETUP: remove any committed schema dumps (database/schema/*.sql).
    # When Laravel sees a dump file it loads it FIRST then runs migrations —
    # which conflicts whenever the dump's snapshot date is older than the
    # newest migration files (Laravel re-runs the same CREATE TABLE
    # statements and dies with `Base table or view already exists`). On
    # an ephemeral per-chat dev DB starting empty there's no perf benefit
    # to the dump shortcut, so unconditionally take the migrations-only
    # path. (Caught on tagh-test 2026-04-28 — `mysql-schema.sql` had
    # `brands` plus a newer `create_brands_table.php` migration.)
    migrate:multidomain-gecche|migrate:multidomain-spatie|migrate:multidomain-stancl|migrate:*)
        rm -f database/schema/*.sql 2>/dev/null || :
        case "${VARIANT}" in
            multidomain-gecche)
                php artisan migrate --domain="${INSTANCE_HOST}" --force
                ;;
            multidomain-spatie)
                php artisan migrate --force --path=database/migrations/landlord
                php artisan tenants:artisan "migrate --force"
                ;;
            multidomain-stancl)
                php artisan migrate --force
                php artisan tenants:migrate --force
                ;;
            *)
                php artisan migrate --force
                ;;
        esac
        ;;

    # seed: optional seed data. Same `--domain` rule as migrate for gecche.
    seed:multidomain-gecche)
        php artisan db:seed --domain="${INSTANCE_HOST}" --force
        ;;
    seed:multidomain-spatie)
        php artisan db:seed --force
        php artisan tenants:artisan "db:seed --force" || :
        ;;
    seed:*)
        php artisan db:seed --force
        ;;

    # seed-admin: insert a default Admin user so /webapi/set + the
    # SSO/fake-auth flow have something to authenticate as on a fresh
    # instance. Idempotent via INSERT IGNORE on the primary key.
    # Schema-tolerant: uses only columns that exist in a vanilla
    # Laravel users table (id, name, email, created_at, updated_at).
    # If tagh-test's users table requires additional NOT NULL columns
    # (e.g. password), the INSERT silently fails and the step is
    # logged but not a hard failure (skippable: true in guide.md).
    seed-admin:multidomain-gecche|seed-admin:*)
        # Schema-tolerant INSERT: introspect the users table first so
        # we only reference columns that actually exist. Different
        # Laravel templates ship different users schemas — basic
        # Breeze has (id, name, email, password, *_at); Vuexy templates
        # add division_id, default_department_id, external_id; spatie
        # has uuid; some have role_id NOT NULL; etc. Hardcoding any
        # specific column set fails on whichever shape the project
        # doesn't have. (Caught when tagh-test had no `uuid` column
        # and the INSERT errored out, leaving the users table empty.)
        echo "  introspecting users schema for portable INSERT..."
        cols=$(mysql -h db -u app -p"${DB_PASSWORD}" -N -B app -e \
            "SELECT COLUMN_NAME FROM information_schema.columns \
             WHERE table_schema='app' AND table_name='users';" 2>/dev/null)
        if [ -z "$cols" ]; then
            echo "  (no users table — skipping seed-admin)"
        else
            # Bodyease default seed — the SSO `external_id` UUID is the
            # one that sso-back.staging-ami.com recognises for this dev
            # account; without it the SDK lookup `users.external_id`
            # in validateCredentials returns no match and the SSO flow
            # fails post-token-exchange.
            SSO_UUID="0199bf2d-406d-71e6-b6b2-f28f8256c6df"
            sql_cols="id,name,email"
            sql_vals="1,'Bodyease','accounts@bodyease.co.uk'"
            # Different Laravel templates use different column names
            # for the SSO link. Match whichever exists.
            for sso_col in external_id sso_uuid uuid; do
                if echo "$cols" | grep -qx "$sso_col"; then
                    sql_cols="${sql_cols},${sso_col}"
                    sql_vals="${sql_vals},'${SSO_UUID}'"
                    break
                fi
            done
            echo "$cols" | grep -qx "email_verified_at" && {
                sql_cols="${sql_cols},email_verified_at"
                sql_vals="${sql_vals},'2025-12-17 15:08:20'"
            }
            echo "$cols" | grep -qx "created_at" && {
                sql_cols="${sql_cols},created_at"
                sql_vals="${sql_vals},NOW()"
            }
            echo "$cols" | grep -qx "updated_at" && {
                sql_cols="${sql_cols},updated_at"
                sql_vals="${sql_vals},NOW()"
            }
            mysql -h db -u app -p"${DB_PASSWORD}" app -e \
                "INSERT IGNORE INTO users (${sql_cols}) VALUES (${sql_vals});" \
                2>&1 || echo "  (seed-admin INSERT failed — schema needs columns we don't know; skipping)"
            mysql -h db -u app -p"${DB_PASSWORD}" -N -B app -e \
                "SELECT CONCAT('  seeded ', COUNT(*), ' user row(s)') FROM users;" 2>/dev/null
        fi

        # If the project uses Spatie laravel-permission, grant the
        # seeded user every available role + permission. Without this,
        # the user logs in successfully but every controller's
        # role/permission gate rejects with
        # "User does not have the right roles" (UnauthorizedException).
        # Best-effort: only runs if the spatie tables actually exist.
        if [ -d vendor/spatie/laravel-permission ]; then
            php artisan tinker --execute="
                try {
                    \$u = App\\Models\\User::find(1);
                    if (!\$u) { echo 'no-user-1'; return; }
                    if (class_exists(\\Spatie\\Permission\\Models\\Role::class)) {
                        foreach (\\Spatie\\Permission\\Models\\Role::all() as \$r) { \$u->assignRole(\$r); }
                    }
                    if (class_exists(\\Spatie\\Permission\\Models\\Permission::class)) {
                        foreach (\\Spatie\\Permission\\Models\\Permission::all() as \$p) { \$u->givePermissionTo(\$p); }
                    }
                    echo '  granted roles: ' . \$u->roles()->pluck('name')->implode(',');
                    \\Artisan::call('permission:cache-reset');
                } catch (\\Throwable \$e) {
                    echo '  (spatie role-grant skipped: ' . \$e->getMessage() . ')';
                }
            " 2>/dev/null || echo "  (tinker for role grant unavailable)"
        fi
        ;;

    # storage-link: gecche docs note that --domain is honored on this
    # command but the symlink name is hardcoded by Laravel core to
    # `storage`. For our single-domain-per-instance use case the standard
    # link is what we want; gecche-specific multi-link setups (one
    # symlink per registered domain) aren't needed here.
    storage-link:*)
        php artisan storage:link
        ;;

    # config-cache: optional, gecche generates per-domain config-<host>.php
    # files when --domain is passed. Useful in production for boot speed.
    config-cache:multidomain-gecche)
        php artisan config:cache --domain="${INSTANCE_HOST}"
        ;;
    config-cache:*)
        php artisan config:cache
        ;;

    *)
        echo "_variant.sh: unknown step '${STEP}' for variant '${VARIANT}'" >&2
        exit 2
        ;;
esac
