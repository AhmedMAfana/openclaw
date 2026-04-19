"""Git operations via subprocess — clone, branch, push, PR."""
import asyncio
import os

from openclow.utils.logging import get_logger

log = get_logger()


async def _get_git_env() -> dict:
    """Get environment with GitHub token from DB."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        from openclow.services.config_service import get_config
        config = await get_config("git", "provider")
        if config and config.get("token"):
            env["GH_TOKEN"] = config["token"]
            env["GITHUB_TOKEN"] = config["token"]
    except Exception:
        pass
    return env


async def run_exec(
    *args: str,
    cwd: str | None = None,
    ignore_errors: bool = False,
    env: dict | None = None,
) -> str:
    """Run a command with argument list (safe from injection)."""
    if env is None:
        env = await _get_git_env()
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    stdout_str = stdout.decode().strip()
    stderr_str = stderr.decode().strip()
    if proc.returncode != 0 and not ignore_errors:
        log.error("cmd.failed", cmd=" ".join(args), stderr=stderr_str, returncode=proc.returncode)
        raise RuntimeError(f"Command failed: {' '.join(args)}\n{stderr_str}")
    return stdout_str



async def clone(repo: str, dest: str):
    """Clone a GitHub repo."""
    await run_exec("git", "clone", f"https://github.com/{repo}.git", dest)


async def fetch_and_reset(workspace: str, branch: str):
    """Fetch latest and reset to remote branch."""
    await run_exec("git", "fetch", "origin", branch, cwd=workspace)
    await run_exec("git", "checkout", branch, cwd=workspace)
    await run_exec("git", "reset", "--hard", f"origin/{branch}", cwd=workspace)


async def create_branch(workspace: str, branch_name: str):
    """Create and checkout a branch. If it already exists, just check it out."""
    try:
        await run_exec("git", "checkout", "-b", branch_name, cwd=workspace)
    except Exception:
        # Branch already exists — check it out instead
        await run_exec("git", "checkout", branch_name, cwd=workspace)


async def add_all(workspace: str):
    """Stage all changes."""
    await run_exec("git", "add", "-A", cwd=workspace)


async def commit(workspace: str, message: str):
    """Commit staged changes."""
    await run_exec("git", "commit", "-m", message, cwd=workspace)


async def push(workspace: str, branch_name: str):
    """Push branch to remote."""
    await run_exec("git", "push", "origin", branch_name, cwd=workspace)


async def commit_and_push(workspace: str, branch_name: str, message: str):
    """Stage, commit, and push all changes."""
    await add_all(workspace)
    # Check if there are changes to commit
    status = await run_exec("git", "status", "--porcelain", cwd=workspace)
    if not status:
        log.warning("git.no_changes", workspace=workspace)
        return
    await commit(workspace, message)
    await push(workspace, branch_name)


async def create_pull_request(
    repo: str, branch: str, base: str, title: str, body: str
) -> tuple[str, int]:
    """Create a GitHub PR. Returns (pr_url, pr_number)."""
    result = await run_exec(
        "gh", "pr", "create",
        "--repo", repo,
        "--head", branch,
        "--base", base,
        "--title", title,
        "--body", body,
    )
    pr_url = result.strip()

    # Get PR number
    pr_num_str = await run_exec(
        "gh", "pr", "view", branch,
        "--repo", repo,
        "--json", "number",
        "-q", ".number",
    )
    pr_number = int(pr_num_str)

    return pr_url, pr_number


async def merge_pull_request(repo: str, pr_number: int):
    """Merge a PR with squash."""
    await run_exec("gh", "pr", "merge", str(pr_number), "--repo", repo, "--squash", "--admin")


async def close_pull_request(repo: str, pr_number: int):
    """Close a PR without merging."""
    await run_exec("gh", "pr", "close", str(pr_number), "--repo", repo)


async def delete_remote_branch(repo: str, branch: str):
    """Delete a remote branch."""
    await run_exec(
        "gh", "api", f"repos/{repo}/git/refs/heads/{branch}",
        "-X", "DELETE",
        ignore_errors=True,
    )


async def diff_stat(workspace: str) -> str:
    """Get a summary of changes (files changed, insertions, deletions)."""
    return await run_exec("git", "diff", "--stat", "HEAD", cwd=workspace)


async def changed_files(workspace: str) -> list[str]:
    """Get list of changed file paths — uncommitted OR last commit (coder commits before deploy)."""
    files: set[str] = set()

    # Uncommitted changes
    output = await run_exec("git", "diff", "--name-only", "HEAD", cwd=workspace, ignore_errors=True)
    files.update(f.strip() for f in output.splitlines() if f.strip())

    # Latest commit — coder typically commits before deploy runs, so HEAD is clean.
    # Include the last commit's files so the deploy step knows to rebuild.
    output = await run_exec("git", "log", "-1", "--name-only", "--pretty=format:", cwd=workspace, ignore_errors=True)
    files.update(f.strip() for f in output.splitlines() if f.strip())

    return list(files)


async def reset_hard(workspace: str):
    """Reset working tree to HEAD (used for empty-diff retry path)."""
    await run_exec("git", "reset", "--hard", "HEAD", cwd=workspace)


async def diff_size(workspace: str) -> int:
    """Get the size of the diff in lines."""
    diff = await run_exec("git", "diff", "HEAD", cwd=workspace, ignore_errors=True)
    return len(diff.splitlines())


async def status(workspace: str) -> str:
    """Get git status."""
    return await run_exec("git", "status", "--short", cwd=workspace)


async def worktree_add(base_repo: str, work_path: str, branch: str = "HEAD"):
    """Create a git worktree."""
    await run_exec("git", "worktree", "add", work_path, branch, cwd=base_repo)


async def worktree_remove(base_repo: str, work_path: str):
    """Remove a git worktree."""
    await run_exec("git", "worktree", "remove", work_path, "--force", cwd=base_repo, ignore_errors=True)
