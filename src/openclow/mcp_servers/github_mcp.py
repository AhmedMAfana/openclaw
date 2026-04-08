"""GitHub MCP Server — gives agents direct access to GitHub operations.

Agents can list repos, check access, read files, create PRs, etc.
Uses GitHub REST API with token from DB, falls back to gh CLI.
"""
import asyncio
import json
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("github-openclow")


async def _get_github_env() -> dict:
    """Get environment with GitHub token from DB config."""
    env = {**os.environ}
    try:
        from openclow.services.config_service import get_config
        config = await get_config("git", "provider")
        if config and config.get("token"):
            env["GH_TOKEN"] = config["token"]
            env["GITHUB_TOKEN"] = config["token"]
    except Exception:
        pass
    return env


async def _get_github_token() -> str:
    """Get GitHub token from DB or environment."""
    try:
        from openclow.services.config_service import get_config
        config = await get_config("git", "provider")
        if config and config.get("token"):
            return config["token"]
    except Exception:
        pass
    return os.environ.get("GITHUB_TOKEN", os.environ.get("GH_TOKEN", ""))


async def _gh_cmd(*args: str, timeout: int = 15) -> tuple[int, str, str]:
    """Run a gh CLI command with token from DB. Returns (returncode, stdout, stderr)."""
    env = await _get_github_env()
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode().strip(), stderr.decode().strip()
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", "Command timed out"


@mcp.tool()
async def list_repos(limit: int = 30) -> str:
    """List GitHub repositories the authenticated user has access to."""
    # Try direct API first (no gh CLI dependency)
    token = await _get_github_token()
    if token:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.github.com/user/repos",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                    },
                    params={"per_page": limit, "sort": "updated", "affiliation": "owner,collaborator,organization_member"},
                )
                if resp.status_code == 200:
                    repos = resp.json()
                    if not repos:
                        return "No repositories found."
                    lines = []
                    for r in repos:
                        vis = "private" if r.get("private") else "public"
                        desc = r.get("description") or "no description"
                        lines.append(f"{r['full_name']} | {desc} | {vis}")
                    return "\n".join(lines)
        except Exception:
            pass

    # Fallback to gh CLI
    rc, stdout, stderr = await _gh_cmd(
        "gh", "repo", "list", "--limit", str(limit),
        "--json", "nameWithOwner,description,isPrivate,updatedAt",
        "--jq", '.[] | "\\(.nameWithOwner) | \\(.description // "no description") | \\(if .isPrivate then "private" else "public" end)"'
    )
    if rc == 0 and stdout:
        return stdout
    return f"No repositories found. Check GitHub token config. Error: {stderr}"


@mcp.tool()
async def repo_info(repo: str) -> str:
    """Get detailed information about a GitHub repository."""
    # Try direct API first
    token = await _get_github_token()
    if token:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return json.dumps({
                        "name": data["name"],
                        "description": data.get("description", ""),
                        "default_branch": data.get("default_branch", "main"),
                        "language": data.get("language", ""),
                        "url": data.get("html_url", ""),
                        "private": data.get("private", False),
                    }, indent=2)
        except Exception:
            pass

    # Fallback to gh CLI
    rc, stdout, stderr = await _gh_cmd(
        "gh", "repo", "view", repo,
        "--json", "name,description,defaultBranchRef,languages,url",
        "--jq", "."
    )
    if rc != 0:
        return f"Error: {stderr}"
    return stdout


@mcp.tool()
async def list_branches(repo: str, limit: int = 20) -> str:
    """List branches of a GitHub repository."""
    token = await _get_github_token()
    if token:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo}/branches",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                    },
                    params={"per_page": limit},
                )
                if resp.status_code == 200:
                    branches = resp.json()
                    return "\n".join(b["name"] for b in branches)
        except Exception:
            pass

    rc, stdout, _ = await _gh_cmd(
        "gh", "api", f"repos/{repo}/branches",
        "--jq", f".[:{limit}] | .[] | .name"
    )
    return stdout


@mcp.tool()
async def list_prs(repo: str, state: str = "open") -> str:
    """List pull requests for a repository."""
    token = await _get_github_token()
    if token:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo}/pulls",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                    },
                    params={"state": state, "per_page": 20},
                )
                if resp.status_code == 200:
                    prs = resp.json()
                    if not prs:
                        return f"No {state} PRs"
                    lines = []
                    for pr in prs:
                        author = pr.get("user", {}).get("login", "unknown")
                        lines.append(f"#{pr['number']} [{pr['state']}] {pr['title']} by {author}")
                    return "\n".join(lines)
        except Exception:
            pass

    rc, stdout, _ = await _gh_cmd(
        "gh", "pr", "list", "--repo", repo, "--state", state,
        "--json", "number,title,state,author",
        "--jq", '.[] | "#\\(.number) [\\(.state)] \\(.title) by \\(.author.login)"'
    )
    return stdout or f"No {state} PRs"


@mcp.tool()
async def check_repo_access(repo: str) -> str:
    """Check if the authenticated user has write access to a repository."""
    token = await _get_github_token()
    if token:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                if resp.status_code == 200:
                    perms = resp.json().get("permissions", {})
                    return f"Access: {json.dumps(perms)}"
                return f"No access or repo not found (HTTP {resp.status_code})"
        except Exception:
            pass

    rc, stdout, stderr = await _gh_cmd(
        "gh", "api", f"repos/{repo}",
        "--jq", ".permissions"
    )
    if rc != 0:
        return f"No access or repo not found: {stderr}"
    return f"Access: {stdout}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
