"""Port allocator — assigns unique, deterministic port ranges per project.

Each project gets a 100-port range based on its DB id:
  Project 1 → 10000–10099
  Project 2 → 10100–10199
  Project 3 → 10200–10299
  ...

Within the range, well-known services get fixed offsets:
  +0  = app/web (nginx, HTTP)
  +1  = database (MySQL/Postgres)
  +2  = Redis
  +3  = Meilisearch/Elasticsearch
  +4  = Mailpit SMTP
  +5  = Mailpit dashboard
  +6  = Vite HMR
  +7  = Selenium
  +8..+99 = spare

This is injected as env vars before `docker compose up`, so projects
that use `${FORWARD_DB_PORT:-3306}` automatically get unique ports.
Works with standard Sail AND custom compose files.
"""

PORT_BASE = 10000
PORT_RANGE = 100

# Standard service offsets within a project's range
OFFSETS = {
    "app":          0,   # nginx/web/HTTP
    "db":           1,   # MySQL, Postgres
    "redis":        2,
    "meilisearch":  3,
    "mailpit_smtp": 4,
    "mailpit_dash": 5,
    "vite":         6,
    "selenium":     7,
}

# Env var names used by Laravel Sail and common Docker setups
PORT_ENV_VARS = {
    "APP_PORT":                       "app",
    "FORWARD_DB_PORT":                "db",
    "FORWARD_REDIS_PORT":             "redis",
    "FORWARD_MEILISEARCH_PORT":       "meilisearch",
    "FORWARD_MAILPIT_PORT":           "mailpit_smtp",
    "FORWARD_MAILPIT_DASHBOARD_PORT": "mailpit_dash",
    "VITE_PORT":                      "vite",
    # Common non-Sail names
    "MYSQL_PORT":                     "db",
    "POSTGRES_PORT":                  "db",
    "REDIS_PORT":                     "redis",
    "NGINX_HOST_HTTP_PORT":           "app",
}


def get_port(project_id: int, service: str) -> int:
    """Get the unique host port for a service in a project.

    Args:
        project_id: The project's DB id (1, 2, 3, ...)
        service: Service name from OFFSETS (e.g., "app", "db", "redis")

    Returns:
        Unique port number (e.g., 10201 for project 3's database)
    """
    offset = OFFSETS.get(service, 0)
    return PORT_BASE + (project_id * PORT_RANGE) + offset


def get_all_ports(project_id: int) -> dict[str, int]:
    """Get all port assignments for a project.

    Returns dict like {"app": 10100, "db": 10101, "redis": 10102, ...}
    """
    return {svc: get_port(project_id, svc) for svc in OFFSETS}


def get_port_env_vars(project_id: int) -> dict[str, str]:
    """Get env vars to inject before docker compose up.

    Returns dict like {"APP_PORT": "10100", "FORWARD_DB_PORT": "10101", ...}
    """
    result = {}
    for env_var, service in PORT_ENV_VARS.items():
        result[env_var] = str(get_port(project_id, service))
    return result


def get_app_port(project_id: int) -> int:
    """Convenience: get the web/app port for a project."""
    return get_port(project_id, "app")
