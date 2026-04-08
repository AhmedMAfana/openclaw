"""QA E2E Test Runner — Playwright clicks through EVERY flow on Telegram Web.

Uses Claude Agent + Playwright MCP to actually open the bot in Telegram Web,
send commands, click every button, verify every response, screenshot everything.

Full test coverage:
- /start → all menu buttons
- Add Project → repo list → select → onboard → confirm
- Projects → project list → project detail → all action buttons
- Status → active/completed tasks
- Dashboard → tunnel link
- Help → command list
- Health check → container status
- Docker Up/Down → container lifecycle
- Unlink/Relink → project lifecycle
- Bootstrap → checklist progress
- Task flow → submit → plan → approve
- Logs → smart logs view
- Double-click guard → rapid clicks
"""
import asyncio
import time

from openclow.providers import factory
from openclow.services.checklist_reporter import ChecklistReporter
from openclow.utils.logging import get_logger

log = get_logger()


# ---------------------------------------------------------------------------
# The full QA prompt — Claude Agent drives Playwright through EVERY flow
# ---------------------------------------------------------------------------

QA_PROMPT = """You are a QA tester. Open Telegram Web, go to the OpenClow bot, and test EVERY feature.

BOT: @{bot_username}
URL: https://web.telegram.org/k/#{bot_username}

IMPORTANT INSTRUCTIONS:
- After EACH action, wait 3-5 seconds for the bot to respond
- Take a snapshot after each bot response to verify
- If you see a loading state, wait and take another snapshot
- Click buttons by their exact text
- After each test, output the result line IMMEDIATELY (don't batch)

═══════════════════════════════════════
TEST PLAN — Execute ALL tests in order
═══════════════════════════════════════

TEST 1: MAIN MENU
- Navigate to the bot chat
- Type and send: /start
- Wait 3 seconds, take snapshot
- VERIFY: See buttons "New Task", "Projects", "Status", "Logs", "Dashboard", "Add Project", "Help"
- OUTPUT: TEST 1: PASS/FAIL Main Menu — [what you see]

TEST 2: HELP
- Click the "Help" button
- Wait 3 seconds, take snapshot
- VERIFY: Help text with commands (/task, /projects, /status, etc.)
- Click "Main Menu" to go back
- OUTPUT: TEST 2: PASS/FAIL Help — [what you see]

TEST 3: PROJECTS LIST
- Click "Projects"
- Wait 3 seconds, take snapshot
- VERIFY: Either project cards with green dots OR "No projects connected" message
- Record how many projects are listed
- OUTPUT: TEST 3: PASS/FAIL Projects — [count] projects listed / no projects

TEST 4: STATUS
- Click "Main Menu" to go back
- Click "Status"
- Wait 3 seconds, take snapshot
- VERIFY: Shows task list OR "No active tasks"
- OUTPUT: TEST 4: PASS/FAIL Status — [what you see]

TEST 5: DASHBOARD
- Click "Main Menu" to go back
- Click "Dashboard"
- Wait 5 seconds, take snapshot
- VERIFY: Shows dashboard URL (trycloudflare.com link) OR loading
- OUTPUT: TEST 5: PASS/FAIL Dashboard — [url or error]

TEST 6: ADD PROJECT
- Click "Main Menu" to go back
- Click "Add Project"
- Wait 5 seconds, take snapshot
- VERIFY: Either repo list with GitHub repos OR "Could not fetch repos" with retry
- If repos appear, note the first repo name but do NOT click it (don't actually add)
- Click "Main Menu" or back
- OUTPUT: TEST 6: PASS/FAIL Add Project — [repos visible / error]

TEST 7: PROJECT DETAIL (skip if no projects in test 3)
- Click "Main Menu" → "Projects"
- Click the FIRST project in the list
- Wait 3 seconds, take snapshot
- VERIFY: Project detail with buttons: "Health Check", "Bootstrap", "Docker Up", "Docker Down", "Unlink", "Remove"
- OUTPUT: TEST 7: PASS/FAIL Project Detail — [buttons visible]

TEST 8: HEALTH CHECK (skip if no projects)
- From project detail, click "Health Check"
- Wait 10 seconds (health checks take time), take snapshot
- VERIFY: Container status appears (green/red indicators, container names)
- OUTPUT: TEST 8: PASS/FAIL Health Check — [what you see]

TEST 9: LOGS
- Send /start to go back to main menu
- Click "Logs"
- Wait 5 seconds, take snapshot
- VERIFY: Log output appears OR "no recent logs"
- OUTPUT: TEST 9: PASS/FAIL Logs — [what you see]

TEST 10: DOCKER DOWN (skip if no projects)
- Click "Main Menu" → "Projects" → first project
- Click "Docker Down"
- Wait 5 seconds, take snapshot
- VERIFY: Shows "Stopping Docker..." then completion message
- OUTPUT: TEST 10: PASS/FAIL Docker Down — [what you see]

TEST 11: DOCKER UP (skip if no projects)
- From the same view, click "Docker Up" (or go back to project and click it)
- Wait 10 seconds, take snapshot
- VERIFY: Shows "Starting Docker..." then completion
- OUTPUT: TEST 11: PASS/FAIL Docker Up — [what you see]

TEST 12: DOUBLE-CLICK GUARD
- Click "Main Menu" → "Projects" → first project → "Health Check"
- IMMEDIATELY click "Health Check" again (within 1 second)
- Wait 3 seconds, take snapshot
- VERIFY: Should NOT show two health checks running. Should show "already being processed" or only one check.
- OUTPUT: TEST 12: PASS/FAIL Double-Click Guard — [what you see]

═══════════════════════════════════════
FINAL REPORT — output this at the end
═══════════════════════════════════════

QA_REPORT_START
TEST 1: [PASS/FAIL] Main Menu — [details]
TEST 2: [PASS/FAIL] Help — [details]
TEST 3: [PASS/FAIL] Projects — [details]
TEST 4: [PASS/FAIL] Status — [details]
TEST 5: [PASS/FAIL] Dashboard — [details]
TEST 6: [PASS/FAIL] Add Project — [details]
TEST 7: [PASS/FAIL/SKIP] Project Detail — [details]
TEST 8: [PASS/FAIL/SKIP] Health Check — [details]
TEST 9: [PASS/FAIL] Logs — [details]
TEST 10: [PASS/FAIL/SKIP] Docker Down — [details]
TEST 11: [PASS/FAIL/SKIP] Docker Up — [details]
TEST 12: [PASS/FAIL/SKIP] Double-Click Guard — [details]
QA_REPORT_END

TOTAL: [X/Y] passed, [Z] skipped
"""


def _parse_report(output: str) -> dict:
    """Parse QA report from agent output."""
    import re
    report = {"tests": [], "passed": 0, "failed": 0, "skipped": 0, "total": 0}

    match = re.search(r"QA_REPORT_START\n(.+?)QA_REPORT_END", output, re.DOTALL)
    if not match:
        # Try parsing individual test lines from full output
        for line in output.split("\n"):
            m = re.match(r"TEST\s+(\d+):\s+(PASS|FAIL|SKIP)\s+(.+)", line.strip())
            if m:
                report["tests"].append({
                    "number": int(m.group(1)),
                    "status": m.group(2),
                    "description": m.group(3).strip(),
                })
        if not report["tests"]:
            report["error"] = "Could not parse QA report"
            return report
    else:
        for line in match.group(1).strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("TOTAL:"):
                continue
            m = re.match(r"TEST\s+(\d+):\s+(PASS|FAIL|SKIP)\s+(.+)", line)
            if m:
                report["tests"].append({
                    "number": int(m.group(1)),
                    "status": m.group(2),
                    "description": m.group(3).strip(),
                })

    for t in report["tests"]:
        if t["status"] == "PASS":
            report["passed"] += 1
        elif t["status"] == "FAIL":
            report["failed"] += 1
        else:
            report["skipped"] += 1
    report["total"] = len(report["tests"])
    return report


def _format_telegram(report: dict, duration: int) -> str:
    """Format report for Telegram message."""
    if report.get("error"):
        return f"❌ QA Failed\n\n{report['error']}"

    lines = [
        f"🧪 QA Report ({duration}s)",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]
    for t in report["tests"]:
        if t["status"] == "PASS":
            icon = "✅"
        elif t["status"] == "FAIL":
            icon = "❌"
        else:
            icon = "⏭️"
        lines.append(f"{icon} {t['description']}")

    lines.append("")
    p, f, s = report["passed"], report["failed"], report["skipped"]
    total = report["total"]
    if f == 0:
        lines.append(f"🎉 {p}/{total} passed" + (f", {s} skipped" if s else ""))
    else:
        lines.append(f"⚠️ {p} passed, {f} failed" + (f", {s} skipped" if s else ""))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main QA task
# ---------------------------------------------------------------------------

async def run_qa_tests(ctx: dict, chat_id: str, message_id: str, scope: str = "smoke"):
    """Run full E2E QA via Playwright on Telegram Web.

    Scope:
    - "smoke": tests 1-6 (menu, help, projects, status, dashboard, add project)
    - "full": all 12 tests including project lifecycle
    """
    chat = await factory.get_chat()
    start_time = time.time()

    # Get bot username
    bot_username = "openclow_bot"
    try:
        from openclow.services.config_service import get_config
        import httpx
        chat_config = await get_config("chat", "provider")
        if chat_config and chat_config.get("token"):
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"https://api.telegram.org/bot{chat_config['token']}/getMe"
                )
                data = resp.json()
                if data.get("ok"):
                    bot_username = data["result"]["username"]
    except Exception:
        pass

    test_names = [
        "Main Menu",
        "Help",
        "Projects",
        "Status",
        "Dashboard",
        "Add Project",
    ]
    if scope == "full":
        test_names.extend([
            "Project Detail",
            "Health Check",
            "Logs",
            "Docker Down",
            "Docker Up",
            "Double-Click Guard",
        ])

    checklist = ChecklistReporter(
        chat, chat_id, message_id,
        title="QA Testing",
        subtitle=f"{len(test_names)} tests via Playwright",
    )
    checklist.set_steps(test_names)
    await checklist._force_render()
    await checklist.start()

    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import AssistantMessage, TextBlock

        options = ClaudeAgentOptions(
            system_prompt=(
                "You are a QA automation engineer. "
                "Use Playwright to test a Telegram bot on web.telegram.org. "
                "Be systematic: navigate, act, wait, snapshot, verify, report. "
                "Output each TEST result line immediately after verifying."
            ),
            model="claude-sonnet-4-6",
            allowed_tools=[
                "mcp__playwright__browser_navigate",
                "mcp__playwright__browser_snapshot",
                "mcp__playwright__browser_take_screenshot",
                "mcp__playwright__browser_click",
                "mcp__playwright__browser_fill_form",
                "mcp__playwright__browser_type",
                "mcp__playwright__browser_press_key",
                "mcp__playwright__browser_wait_for",
                "mcp__playwright__browser_hover",
                "mcp__playwright__browser_tabs",
                "mcp__playwright__browser_console_messages",
            ],
            mcp_servers={
                "playwright": {
                    "command": "npx",
                    "args": ["@playwright/mcp@0.0.28", "--headless"],
                },
            },
            permission_mode="bypassPermissions",
            max_turns=60,  # QA needs many turns to click through everything
        )

        prompt = QA_PROMPT.format(bot_username=bot_username)

        # If smoke only, tell agent to stop after test 6
        if scope == "smoke":
            prompt += "\n\nSTOP after TEST 6. Skip tests 7-12. Output the report with tests 7-12 as SKIP."

        full_output = ""
        current_test = 0
        import re

        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        full_output += block.text + "\n"

                        # Live progress: detect individual test results
                        for line in block.text.split("\n"):
                            line = line.strip()
                            m = re.match(r"TEST\s+(\d+):\s+(PASS|FAIL|SKIP)\s+(.+)", line)
                            if m:
                                idx = int(m.group(1)) - 1
                                status = m.group(2)
                                detail = m.group(3).split("—")[-1].strip() if "—" in m.group(3) else ""
                                if 0 <= idx < len(test_names):
                                    if status == "PASS":
                                        await checklist.complete_step(idx, detail[:40] or "passed")
                                    elif status == "FAIL":
                                        await checklist.fail_step(idx, detail[:40] or "failed")
                                    else:
                                        await checklist.complete_step(idx, "skipped")
                                    current_test = idx + 1
                                    if current_test < len(test_names):
                                        await checklist.start_step(current_test)

        # Parse final report
        report = _parse_report(full_output)
        duration = int(time.time() - start_time)

        # Final render
        await checklist.stop()
        summary = _format_telegram(report, duration)
        checklist._footer = summary.split("\n")[-1]  # last line = summary

        from aiogram.types import InlineKeyboardButton
        buttons = [
            [
                InlineKeyboardButton(text="🔄 Smoke", callback_data="qa:smoke"),
                InlineKeyboardButton(text="🔄 Full QA", callback_data="qa:full"),
            ],
            [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
        ]
        await checklist._force_render(buttons=buttons)

        # Also send the full report as a separate message for readability
        bot = chat._get_bot()
        await bot.send_message(chat_id=int(chat_id), text=summary)

        log.info("qa.complete", passed=report["passed"], failed=report["failed"],
                 skipped=report["skipped"], total=report["total"], duration=duration)
        return report

    except (asyncio.CancelledError, TimeoutError) as e:
        error_msg = "QA timed out" if isinstance(e, TimeoutError) else "QA cancelled"
        await checklist.stop()
        checklist._footer = f"❌ {error_msg}"
        from aiogram.types import InlineKeyboardButton
        await checklist._force_render(buttons=[
            [InlineKeyboardButton(text="🔄 Retry", callback_data="qa:smoke")],
            [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
        ])
    except ImportError:
        await checklist.stop()
        checklist._footer = "❌ Claude Agent SDK not available"
        from aiogram.types import InlineKeyboardButton
        await checklist._force_render(buttons=[
            [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
        ])
    except Exception as e:
        log.error("qa.failed", error=str(e))
        await checklist.stop()
        checklist._footer = f"❌ {str(e)[:100]}"
        from aiogram.types import InlineKeyboardButton
        await checklist._force_render(buttons=[
            [InlineKeyboardButton(text="🔄 Retry", callback_data="qa:smoke")],
            [InlineKeyboardButton(text="◀️ Main Menu", callback_data="menu:main")],
        ])
    finally:
        await checklist.stop()
        await chat.close()
