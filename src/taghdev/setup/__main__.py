"""TAGH Dev Setup Wizard — interactive configuration.

Usage: python -m taghdev.setup

Walks you through connecting your LLM, chat platform, git provider,
projects, and users. Saves everything to the database.
"""
import asyncio
import getpass
import sys


def ask(prompt: str, default: str = "") -> str:
    if default:
        result = input(f"{prompt} [{default}]: ").strip()
        return result or default
    return input(f"{prompt}: ").strip()


def ask_choice(prompt: str, options: list[tuple[str, str]]) -> str:
    print(f"\n{prompt}")
    for i, (key, label) in enumerate(options, 1):
        print(f"  [{i}] {label}")
    while True:
        choice = input(f"Choose (1-{len(options)}): ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx][0]
        except ValueError:
            pass
        print("Invalid choice. Try again.")


def ask_secret(prompt: str) -> str:
    return getpass.getpass(f"{prompt}: ")


def ask_yn(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    result = input(f"{prompt} {suffix}: ").strip().lower()
    if not result:
        return default
    return result in ("y", "yes")


async def run_setup():
    print("=" * 60)
    print("  TAGH Dev Setup Wizard")
    print("  Configure your AI development orchestration platform")
    print("=" * 60)

    # Import here to avoid circular imports at module level
    from taghdev.services import config_service
    from taghdev.models import Project, User, async_session

    # ── 1. LLM Provider ──
    llm_type = ask_choice(
        "Which LLM provider?",
        [("claude", "Claude Code (Max/Pro subscription)"),
         ("openai", "OpenAI (coming soon)")],
    )

    if llm_type == "openai":
        print("\nOpenAI support is coming soon. Using Claude for now.")
        llm_type = "claude"

    llm_config = {"type": llm_type}
    if llm_type == "claude":
        print("\nClaude auth: Run `claude login` inside the worker container after setup.")
        llm_config["coder_max_turns"] = int(ask("Max turns for coder agent", "50"))
        llm_config["reviewer_max_turns"] = int(ask("Max turns for reviewer agent", "20"))

    await config_service.set_config("llm", "provider", llm_config)
    print(f"  LLM provider: {llm_type}")

    # ── 2. Chat Provider ──
    chat_type = ask_choice(
        "Which chat platform?",
        [("telegram", "Telegram"),
         ("slack", "Slack (coming soon)")],
    )

    if chat_type == "slack":
        print("\nSlack support is coming soon. Using Telegram for now.")
        chat_type = "telegram"

    chat_config = {"type": chat_type}
    if chat_type == "telegram":
        print("\nGet your bot token from @BotFather on Telegram (send /newbot).")
        chat_config["token"] = ask_secret("Telegram bot token")
        # Redis URL comes from settings (bootstrap)
        from taghdev.settings import settings
        chat_config["redis_url"] = settings.redis_url

    await config_service.set_config("chat", "provider", chat_config)
    print(f"  Chat provider: {chat_type}")

    # ── 3. Git Provider ──
    git_type = ask_choice(
        "Which git provider?",
        [("github", "GitHub"),
         ("gitlab", "GitLab (coming soon)")],
    )

    if git_type == "gitlab":
        print("\nGitLab support is coming soon. Using GitHub for now.")
        git_type = "github"

    git_config = {"type": git_type}
    if git_type == "github":
        print("\nCreate a fine-grained PAT at github.com → Settings → Developer Settings")
        print("Permissions: Contents (Read/Write), Pull Requests (Read/Write), Metadata (Read)")
        git_config["token"] = ask_secret("GitHub Personal Access Token")

    await config_service.set_config("git", "provider", git_config)
    print(f"  Git provider: {git_type}")

    # ── 4. Projects ──
    print("\n--- Projects ---")
    while True:
        if not ask_yn("Add a project?"):
            break

        repo = ask("Repository (owner/repo)")
        name = ask("Project name", repo.split("/")[-1] if "/" in repo else repo)
        branch = ask("Default branch", "main")
        tech_stack = ask("Tech stack (e.g. 'Laravel 11, Vue 3')", "")
        description = ask("Description", "")
        setup_cmds = ask("Setup commands (e.g. 'cp .env.example .env')", "")

        async with async_session() as session:
            from sqlalchemy import select
            existing = await session.execute(
                select(Project).where(Project.name == name)
            )
            project = existing.scalar_one_or_none()
            if project:
                project.github_repo = repo
                project.default_branch = branch
                project.tech_stack = tech_stack or project.tech_stack
                project.description = description or project.description
                project.setup_commands = setup_cmds or project.setup_commands
                print(f"  Updated project: {name}")
            else:
                project = Project(
                    name=name, github_repo=repo, default_branch=branch,
                    tech_stack=tech_stack, description=description,
                    setup_commands=setup_cmds,
                )
                session.add(project)
                print(f"  Created project: {name}")
            await session.commit()

    # ── 5. Users ──
    print("\n--- Allowed Users ---")
    while True:
        if not ask_yn("Add an allowed user?"):
            break

        provider_type = chat_type  # same as chat provider
        uid = ask(f"{chat_type.title()} user ID")
        username = ask("Username (optional)", "")

        async with async_session() as session:
            from sqlalchemy import select
            existing = await session.execute(
                select(User).where(User.chat_provider_uid == uid)
            )
            user = existing.scalar_one_or_none()
            if user:
                user.is_allowed = True
                user.username = username or user.username
                print(f"  Updated user: {uid}")
            else:
                user = User(
                    chat_provider_type=provider_type,
                    chat_provider_uid=uid,
                    username=username,
                    is_allowed=True,
                )
                session.add(user)
                print(f"  Created user: {uid}")
            await session.commit()

    # ── Done ──
    print("\n" + "=" * 60)
    print("  Setup complete!")
    print("=" * 60)
    print()
    print("Next steps:")
    print("  1. docker compose build")
    print("  2. docker compose run migrate")
    print("  3. docker compose up -d")
    print("  4. docker exec -it taghdev-worker-1 bash")
    print("     claude login   # authenticate Claude (one time)")
    print("  5. Open Telegram → message your bot → /task")
    print()


def main():
    try:
        asyncio.run(run_setup())
    except KeyboardInterrupt:
        print("\nSetup cancelled.")
        sys.exit(1)


if __name__ == "__main__":
    main()
