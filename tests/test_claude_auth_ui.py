"""Playwright test — Claude Authentication UI flow.

Verifies that:
1. Login works
2. Settings panel opens and shows LLM / AI tab
3. Claude Authentication section is visible
4. Clicking 'Authenticate with Claude' produces a valid PKCE OAuth URL
5. Auto-detection spinner appears (no code input box)
6. Copy URL button is present

Run inside the worker container:
  docker compose exec -u root -e PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers \\
    worker python tests/test_claude_auth_ui.py
"""
from __future__ import annotations

import asyncio
import sys

BASE_URL = "http://host.docker.internal:8000"

# playwright-core path bundled with @playwright/mcp
_PW_PATH = (
    "/usr/local/nvm/versions/node/v20.20.2/lib/node_modules"
    "/@playwright/mcp/node_modules/playwright-core"
)


async def run():
    # Use the playwright-core that ships with @playwright/mcp (has correct browser)
    import importlib.util, sys as _sys
    spec = importlib.util.spec_from_file_location(
        "playwright_core",
        f"{_PW_PATH}/__init__.py",
    )
    # Fall back to the standard playwright package if available
    try:
        from playwright.async_api import async_playwright, expect, TimeoutError as PWTimeout
    except ImportError:
        print("SKIP: Python playwright package not installed.")
        print("      Run: pip install playwright")
        sys.exit(0)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(ignore_https_errors=True)
        page = await ctx.new_page()

        # ── Login ─────────────────────────────────────────────────────────
        print("→ Logging in as testuser…")
        await page.goto(f"{BASE_URL}/chat/login")
        await page.fill("input[name=username]", "testuser")
        await page.fill("input[name=password]", "testpass123")
        await page.click("button[type=submit]")
        await page.wait_for_url(f"{BASE_URL}/chat/**", timeout=10_000)
        print("  ✓ Logged in")

        # ── Open Settings panel ───────────────────────────────────────────
        print("→ Opening Settings panel…")
        await page.locator("button", has_text="Settings").first.click()
        await expect(page.locator("text=LLM / AI")).to_be_visible(timeout=5_000)
        print("  ✓ Settings panel opened")

        # ── Click LLM / AI tab ────────────────────────────────────────────
        print("→ Clicking LLM / AI tab…")
        await page.locator("text=LLM / AI").click()
        await expect(page.locator("text=Claude Authentication")).to_be_visible(timeout=5_000)
        print("  ✓ Claude Authentication section visible")

        # ── Verify initial state ──────────────────────────────────────────
        await expect(page.locator("text=Not authenticated")).to_be_visible(timeout=5_000)
        print("  ✓ Shows Not authenticated (expected — no credentials)")

        # ── Trigger OAuth flow ────────────────────────────────────────────
        print("→ Clicking Authenticate with Claude…")
        await page.locator("button", has_text="Authenticate with Claude").click()

        print("  Waiting for OAuth URL (up to 20 s)…")
        url_link = page.locator("a[href*='claude.com/cai/oauth']")
        try:
            await expect(url_link).to_be_visible(timeout=20_000)
        except Exception:
            content = await page.content()
            print("FAIL: OAuth URL not found.\nPage:", content[:1000])
            await browser.close()
            sys.exit(1)

        oauth_url = await url_link.get_attribute("href")
        print(f"  ✓ OAuth URL: {oauth_url[:80]}…")

        # ── Validate URL structure ────────────────────────────────────────
        assert "response_type=code" in oauth_url, "Missing response_type=code"
        assert "code_challenge" in oauth_url, "Missing PKCE code_challenge"
        assert "code_challenge_method=S256" in oauth_url, "Missing S256 method"
        print("  ✓ URL has correct PKCE parameters")

        # ── Auto-detection spinner ────────────────────────────────────────
        await expect(page.locator("text=Waiting for you to sign in")).to_be_visible(timeout=5_000)
        print("  ✓ Auto-detection spinner visible")

        # ── Copy button ───────────────────────────────────────────────────
        await expect(page.locator("button[title='Copy URL']")).to_be_visible(timeout=3_000)
        print("  ✓ Copy URL button present")

        # ── No old code input box ─────────────────────────────────────────
        code_inputs = await page.locator("input[placeholder*='authorization code']").count()
        assert code_inputs == 0, "Old code input still present — should be removed"
        print("  ✓ No code input field (auto-detect flow, correct)")

        await browser.close()
        print("\n✅ All assertions passed")
        print("   Open the URL above in your browser to complete authentication.")
        print("   The UI will detect it automatically (polls every 3 s).")


if __name__ == "__main__":
    asyncio.run(run())
