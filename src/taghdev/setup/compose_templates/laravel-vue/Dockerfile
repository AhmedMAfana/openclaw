# Per-instance Laravel + Vue runtime image.
#
# Used by every chat that opens against a Laravel-Vue project. Boots
# php-fpm; the projctl binary baked in here is what `_projctl_up` invokes
# via `docker compose exec app projctl up` to run the deterministic
# install/migrate/start steps from this template's guide.md.
#
# Base: serversideup/php:8.4-fpm-nginx-alpine — ships php-fpm AND nginx
# in one container (s6-overlay supervises both), plus composer, git,
# and the common Laravel extensions (pdo_mysql, redis, gd, intl,
# bcmath, etc.). Matches Laravel Sail's single-container pattern:
# the project's primary service listens on :80 directly. Removes the
# need for a separate nginx sidecar in the platform's compose.
#
# projctl: copied from the projctl/ Go image (built separately by
# .github/workflows/projctl-publish.yml or `docker build projctl/`).
#
# Build:
#   docker build -t tagh/laravel-vue-app:latest \
#     src/taghdev/setup/compose_templates/laravel-vue/

ARG PROJCTL_TAG=dev

FROM ghcr.io/tagh/projctl:${PROJCTL_TAG} AS projctl-bin

FROM serversideup/php:8.4-fpm-nginx-alpine

# projctl binary — invoked from the orchestrator via
# `docker compose exec app projctl up`. Lives under /usr/local/bin so
# it's on PATH for both root and the default serversideup user.
COPY --from=projctl-bin /projctl /usr/local/bin/projctl

# Tools projctl's guide.md success_checks rely on:
#   - pgrep, curl: busybox in the base image
#   - mysql-client: not in base, used for `php artisan migrate:status` health
#   - git: required by composer to clone source-mode VCS deps (private
#     packages that can't be served from dist). Composer's fallback
#     blows up with "git was not found in your PATH" without it.
USER root
# PHP extensions needed by the wider Laravel ecosystem this template
# supports. Default serversideup/php-alpine ships pdo_mysql, mbstring,
# pcntl, redis, etc. — but NOT gd or exif. Real-world Laravel apps in
# the tagh-* fleet require:
#   - gd:    image processing (milon/barcode, phpspreadsheet, fpdf,
#            simple-qrcode, intervention/image)
#   - exif:  EXIF metadata (spatie/image, spatie/laravel-medialibrary)
# Caught running tagh-test (composer install dies with "ext-gd is
# missing from your system" + the same for exif).
#
# serversideup ships PHP themselves (NOT alpine's php84-* packages),
# so `apk add php84-gd` installs into a different php binary that
# isn't loaded. Their documented mechanism is the `install-php-extensions`
# helper (https://serversideup.net/open-source/docker-php/docs/getting-started/installing-additional-php-extensions),
# which knows how to wire extensions into THEIR php build.
RUN install-php-extensions gd exif \
 && apk add --no-cache mysql-client git \
 && chmod 0755 /usr/local/bin/projctl \
 # Disable SSL for the mysql CLI globally inside this image. The
 # per-instance MySQL container runs on a private compose bridge with
 # TLS turned off (compose.yml: `--tls-version=`); without this client
 # config, any artisan command that shells out to `mysql` (gecche
 # multidomain, schema introspection, etc.) fails with
 # "ERROR 2026 (HY000): TLS/SSL error: SSL is required, but the
 # server does not support it" — the CLI's default ssl-mode=PREFERRED
 # downgrades to REQUIRED on some builds. ssl-mode=DISABLED forces
 # plaintext, matching what the server now offers.
 && mkdir -p /etc/my.cnf.d /etc/mysql/conf.d \
 # Alpine's `mysql-client` is mariadb-client — uses `ssl=0` (MariaDB
 # syntax). Oracle MySQL uses `ssl-mode=DISABLED`. Either client errors
 # on the OTHER's option name in shared [client], so use the tool-
 # specific section names so each only reads its own:
 #   [client-mariadb] → MariaDB CLI
 #   [client-mysql]   → Oracle mysql CLI (8.x)
 # Plus `loose-` prefix on a [client] fallback so unknown options
 # become warnings, not errors, on whichever client reads it.
 && printf '[client]\nloose-ssl=0\nloose-ssl-mode=DISABLED\n[client-mariadb]\nssl=0\n[client-mysql]\nssl-mode=DISABLED\n' \
    | tee /etc/my.cnf.d/no-ssl.cnf /etc/mysql/conf.d/no-ssl.cnf >/dev/null \
 # Pre-create projctl's state dir and hand ownership to the runtime
 # user. The compose file mounts a named volume here; docker copies
 # the mount-point's existing ownership onto the named volume on
 # first use, so projctl can write state.json without sudo. Without
 # this, projctl exits "save state ... permission denied" mid-run.
 && mkdir -p /var/lib/projctl \
 && chown -R www-data:www-data /var/lib/projctl

# Drop back to serversideup's non-root user for php-fpm runtime.
# The orchestrator's compose file sets `user: "0:0"` if a project
# needs root inside the container; the default here is non-root.
USER www-data

WORKDIR /var/www/html
