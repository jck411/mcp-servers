"""MCP server exposing Playwright-based browser automation tools.

Always launches fresh app-mode browser windows for each session.
Connects via Chrome DevTools Protocol (CDP) for browser-native automation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastmcp import FastMCP

# Default ports
DEFAULT_HTTP_PORT = 9011
DEFAULT_CDP_PORT = 9222
CDP_PORT_ENV = "MCP_PLAYWRIGHT_CDP_PORT"
CDP_PORT_ENV_FALLBACK = "PLAYWRIGHT_CDP_PORT"

# Protected URLs that Playwright must NEVER navigate to or open
# These are the chat app endpoints that would hijack the user's session
PROTECTED_URL_PATTERNS: tuple[str, ...] = (
    "localhost:5173",
    "127.0.0.1:5173",
    "192.168.1.223:5173",  # User's local network chat app
)

# Waybar bookmark presets (brave --app mode minimal windows)
_WAYBAR_PRESETS: dict[str, str] = {
    "chatgpt": "https://chat.openai.com",
    "gemini": "https://gemini.google.com/app",
    "google": "https://www.google.com/",
    "calendar": "https://calendar.google.com/calendar/u/0/r?pli=1",
    "gmail": "https://mail.google.com/mail/u/0/#inbox",
    "github": "https://github.com/jck411?tab=repositories",
}


def _is_protected_url(url: str) -> bool:
    """Check if a URL matches any protected pattern (chat app URLs)."""
    url_lower = url.lower()
    for pattern in PROTECTED_URL_PATTERNS:
        if pattern in url_lower:
            return True
    return False


mcp = FastMCP("playwright")

# =============================================================================
# Global Browser State (single app-mode window per session)
# =============================================================================

_playwright: Any = None
_browser: Any = None
_context: Any = None
_page: Any = None
_connected: bool = False
_browser_process: subprocess.Popen[str] | None = None
_profile_dir: Path | None = None


async def _ensure_playwright() -> Any:
    """Lazy-load playwright module."""
    global _playwright
    if _playwright is None:
        try:
            from playwright.async_api import async_playwright

            _playwright = await async_playwright().start()
        except ImportError:
            raise RuntimeError(
                "Playwright not installed. Run: pip install playwright && playwright install chromium"
            )
    return _playwright


async def _get_page() -> Any:
    """Get the current page, raising if not connected."""
    if not _connected or _page is None:
        raise RuntimeError("Not connected. Call browser_open first.")
    return _page


async def _close_browser() -> None:
    """Internal: Close browser and reset global state."""
    global _browser, _context, _page, _connected, _playwright, _browser_process

    try:
        if _browser:
            await _browser.close()
    except Exception:
        pass
    try:
        if _playwright:
            await _playwright.stop()
    except Exception:
        pass
    await _terminate_browser_process()
    _cleanup_profile_dir()

    _browser = None
    _context = None
    _page = None
    _playwright = None
    _connected = False


def _parse_cdp_port(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    raw = value.strip().lower()
    if raw in ("auto", "0"):
        return 0
    try:
        return int(raw)
    except ValueError:
        return None


def _port_is_listening(port: int) -> bool:
    if port <= 0:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _resolve_cdp_port(requested: int | str | None) -> tuple[int, int | None, bool]:
    parsed_requested = _parse_cdp_port(requested)
    if requested is not None and parsed_requested is None:
        raise ValueError(f"Invalid CDP port: {requested}")

    env_value = _parse_cdp_port(os.environ.get(CDP_PORT_ENV, ""))
    if env_value is None:
        env_value = _parse_cdp_port(os.environ.get(CDP_PORT_ENV_FALLBACK, ""))

    if parsed_requested is None:
        requested = env_value if env_value is not None else DEFAULT_CDP_PORT
    else:
        requested = parsed_requested

    if requested == 0:
        return _pick_free_port(), requested, True

    if not (1 <= requested <= 65535):
        raise ValueError(f"Invalid CDP port: {requested}")

    if _port_is_listening(requested):
        return _pick_free_port(), requested, True

    return requested, requested, False


def _is_utility_page(url: str) -> bool:
    return url.startswith(("chrome-extension://", "devtools://", "chrome://"))


def _urls_match(page_url: str, target_url: str) -> bool:
    if not page_url or not target_url:
        return False
    if page_url == target_url:
        return True
    if page_url.rstrip("/") == target_url.rstrip("/"):
        return True
    if page_url.startswith(target_url) or target_url.startswith(page_url):
        return True

    try:
        page = urlparse(page_url)
        target = urlparse(target_url)
    except Exception:
        return False

    if page.scheme and target.scheme and page.scheme != target.scheme:
        return False
    if page.netloc and target.netloc and page.netloc != target.netloc:
        return False
    if page.netloc and target.netloc:
        return True
    return False


async def _select_page_for_target(
    browser: Any,
    target_url: str,
    timeout_ms: int,
) -> tuple[Any | None, Any | None]:
    wait_seconds = max(0.2, min(2.0, timeout_ms / 1000))
    deadline = time.time() + wait_seconds
    while True:
        pages: list[Any] = []
        for context in browser.contexts:
            pages.extend(context.pages)

        for page in pages:
            if _urls_match(page.url, target_url):
                return page.context, page

        non_utility = [page for page in pages if not _is_utility_page(page.url)]
        if len(non_utility) == 1:
            page = non_utility[0]
            return page.context, page

        if time.time() >= deadline:
            return None, None

        await asyncio.sleep(0.2)


async def _terminate_browser_process() -> None:
    global _browser_process
    process = _browser_process
    _browser_process = None
    if not process:
        return
    try:
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=2.0)
            except Exception:
                process.kill()
                try:
                    await asyncio.to_thread(process.wait, timeout=1.0)
                except Exception:
                    pass
    except Exception:
        pass


def _cleanup_profile_dir() -> None:
    global _profile_dir

    profile_dir = _profile_dir
    _profile_dir = None
    if not profile_dir:
        return

    try:
        shutil.rmtree(profile_dir, ignore_errors=True)
    except Exception:
        pass


# =============================================================================
# Browser Tools
# =============================================================================


@mcp.tool("browser_status")  # type: ignore[misc]
async def browser_status() -> str:
    """Check current browser connection status.

    Call this FIRST to see if browser is connected before using other tools.
    If not connected, call browser_open to start a new session.

    Returns:
        JSON with connection status, current URL, and page title if connected.
    """
    if not _connected or _page is None:
        return json.dumps(
            {
                "status": "ok",
                "connected": False,
                "message": "No browser session. Call browser_open to start.",
            }
        )

    try:
        current_url = _page.url
        page_title = await _page.title()
        return json.dumps(
            {
                "status": "ok",
                "connected": True,
                "url": current_url,
                "title": page_title[:100] if page_title else None,
            }
        )
    except Exception as exc:
        return json.dumps(
            {
                "status": "ok",
                "connected": False,
                "message": f"Session stale: {exc}. Call browser_open to start fresh.",
            }
        )


@mcp.tool("browser_open")  # type: ignore[misc]
async def browser_open(
    url: str | None = None,
    preset: str | None = None,
    timeout_ms: int = 10000,
    force_new: bool = False,
    cdp_port: int | None = None,
) -> str:
    """Open a browser window for automation, reusing existing if available.

    If a browser session is already active, navigates to the new URL.
    Only launches a fresh window when no session exists or force_new=True.

    Args:
        url: URL to open (ignored if preset is provided)
        preset: Waybar bookmark preset name. Options:
            - "chatgpt", "gemini", "google", "calendar", "gmail", "github"
        timeout_ms: CDP connection timeout (default: 10000)
        force_new: Force close existing session and start fresh (default: False)
        cdp_port: Override the CDP port (0 = auto-select free port)

    Returns:
        JSON with status and connection info.
    """
    global _browser, _context, _page, _connected, _browser_process

    # Resolve URL from preset or direct URL first (needed for reuse path)
    if preset:
        preset_lower = preset.lower()
        if preset_lower not in _WAYBAR_PRESETS:
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Unknown preset: {preset}",
                    "available_presets": list(_WAYBAR_PRESETS.keys()),
                }
            )
        target_url = _WAYBAR_PRESETS[preset_lower]
    elif url:
        target_url = url
    else:
        target_url = "about:blank"

    # Block protected URLs (chat app)
    if _is_protected_url(target_url):
        return json.dumps(
            {
                "status": "error",
                "message": "Cannot open chat app URL - this would hijack your session",
                "url": target_url,
            }
        )

    # Reuse existing session if connected (unless force_new)
    if _connected and _page is not None and not force_new:
        try:
            await _page.goto(
                target_url, wait_until="domcontentloaded", timeout=timeout_ms
            )
            return json.dumps(
                {
                    "status": "ok",
                    "url": target_url,
                    "preset": preset,
                    "current_url": _page.url,
                    "reused": True,
                }
            )
        except Exception:
            # Session is stale, fall through to launch new browser
            await _close_browser()

    # Close any existing session if force_new requested
    if _connected and force_new:
        await _close_browser()

    try:
        try:
            resolved_cdp_port, requested_cdp_port, port_changed = _resolve_cdp_port(
                cdp_port
            )
        except ValueError as exc:
            return json.dumps(
                {
                    "status": "error",
                    "message": str(exc),
                }
            )

        # Launch Brave in app mode with CDP enabled
        # --disable-session-restore: No tab restoration
        # --no-first-run: Skip first-run dialogs
        # --force-dark-mode: Dark theme
        _cleanup_profile_dir()
        profile_dir = Path(tempfile.mkdtemp(prefix="mcp-playwright-"))
        _profile_dir = profile_dir
        cmd = [
            "brave",
            f"--app={target_url}",
            f"--remote-debugging-port={resolved_cdp_port}",
            f"--user-data-dir={profile_dir}",
            "--disable-session-restore",
            "--no-first-run",
            "--force-dark-mode",
        ]

        _browser_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )

        # Wait for CDP to become available
        # Give browser a moment to start before polling
        pw = await _ensure_playwright()
        await asyncio.sleep(1.0)  # Initial wait for browser startup
        start_time = time.time()
        last_error = None

        while (time.time() - start_time) * 1000 < timeout_ms:
            if _browser_process and _browser_process.poll() is not None:
                last_error = (
                    f"Browser process exited with code {_browser_process.returncode}"
                )
                break

            await asyncio.sleep(0.3)

            try:
                # Use 127.0.0.1 explicitly to avoid IPv6 issues (::1)
                _browser = await pw.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{resolved_cdp_port}",
                    timeout=2000,
                )

                _context, _page = await _select_page_for_target(
                    _browser,
                    target_url,
                    timeout_ms,
                )
                if _context is None or _page is None:
                    _context = await _browser.new_context()
                    _page = await _context.new_page()

                if target_url != "about:blank" and not _urls_match(
                    _page.url, target_url
                ):
                    await _page.goto(
                        target_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )

                _connected = True

                return json.dumps(
                    {
                        "status": "ok",
                        "url": target_url,
                        "preset": preset,
                        "current_url": _page.url,
                        "cdp_port": resolved_cdp_port,
                        "cdp_port_requested": requested_cdp_port,
                        "cdp_port_changed": port_changed,
                    }
                )

            except Exception as e:
                last_error = str(e)

        await _close_browser()
        return json.dumps(
            {
                "status": "error",
                "message": f"CDP connection timed out: {last_error}",
                "hint": "Browser may still be starting. Try again.",
                "cdp_port": resolved_cdp_port,
                "cdp_port_requested": requested_cdp_port,
                "cdp_port_changed": port_changed,
            }
        )

    except Exception as exc:
        await _close_browser()
        return json.dumps(
            {
                "status": "error",
                "message": str(exc),
            }
        )


@mcp.tool("browser_navigate")  # type: ignore[misc]
async def browser_navigate(
    url: str,
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 30000,
) -> str:
    """Navigate to a URL in the current page.

    Args:
        url: The URL to navigate to
        wait_until: When to consider navigation complete:
            - "domcontentloaded" (default): DOM is ready
            - "load": Full page load including resources
            - "networkidle": No network activity for 500ms
        timeout_ms: Navigation timeout in milliseconds

    Returns:
        JSON with status, final URL, and page title.
    """
    try:
        # Block protected URLs (chat app)
        if _is_protected_url(url):
            return json.dumps(
                {
                    "status": "error",
                    "message": "Cannot navigate to chat app URL - this would hijack your session",
                    "url": url,
                }
            )

        page = await _get_page()

        response = await page.goto(
            url,
            wait_until=wait_until,
            timeout=timeout_ms,
        )

        page_title = await page.title()
        return json.dumps(
            {
                "status": "ok",
                "url": page.url,
                "title": page_title[:100] if page_title else None,
                "http_status": response.status if response else None,
            }
        )

    except Exception as exc:
        return json.dumps(
            {
                "status": "error",
                "message": str(exc),
            }
        )


@mcp.tool("browser_click")  # type: ignore[misc]
async def browser_click(
    selector: str,
    button: str = "left",
    click_count: int = 1,
    timeout_ms: int = 20000,
    index: int | None = None,
) -> str:
    """Click an element on the page with basic robustness.

    Args:
        selector: CSS selector, text selector ("text=Click me"), or XPath
        button: Mouse button - "left", "right", or "middle"
        click_count: Number of clicks (2 for double-click)
        timeout_ms: Timeout waiting for element (defaults to 20s for flaky SERP links)
        index: Optional index if the selector matches multiple elements (0-based)

    Returns:
        JSON with status.
    """
    try:
        page = await _get_page()

        locator = page.locator(selector)
        target = locator.nth(index) if index is not None else locator

        # Ensure the element is ready and on-screen before clicking.
        await target.wait_for(state="visible", timeout=timeout_ms)
        await target.scroll_into_view_if_needed(timeout=timeout_ms)

        await target.click(
            button=button,
            click_count=click_count,
            timeout=timeout_ms,
        )

        return json.dumps(
            {
                "status": "ok",
                "selector": selector,
                "index": index,
            }
        )

    except Exception as exc:
        return json.dumps(
            {
                "status": "error",
                "selector": selector,
                "index": index,
                "message": str(exc),
            }
        )


@mcp.tool("browser_type")  # type: ignore[misc]
async def browser_type(
    selector: str,
    text: str,
    clear_first: bool = True,
    delay_ms: int = 0,
    timeout_ms: int = 10000,
) -> str:
    """Type text into an input element.

    Args:
        selector: CSS selector for the input element. Common selectors:
            - Google search: textarea[name="q"]
            - YouTube search: input#search
            - Generic search: input[type="search"], input[name="search"]
            - Generic text input: input[type="text"]
        text: Text to type
        clear_first: Clear existing content before typing (default: True)
        delay_ms: Delay between keystrokes in milliseconds
        timeout_ms: Timeout waiting for element

    Tip: If typing fails, use browser_navigate with URL params instead:
        https://www.google.com/search?q=your+search+query

    Returns:
        JSON with status.
    """
    try:
        page = await _get_page()

        if clear_first:
            await page.fill(selector, text, timeout=timeout_ms)
        else:
            await page.type(selector, text, delay=delay_ms, timeout=timeout_ms)

        return json.dumps(
            {
                "status": "ok",
                "selector": selector,
                "typed_length": len(text),
            }
        )

    except Exception as exc:
        return json.dumps(
            {
                "status": "error",
                "selector": selector,
                "message": str(exc),
            }
        )


@mcp.tool("browser_press_key")  # type: ignore[misc]
async def browser_press_key(
    key: str,
    selector: str | None = None,
) -> str:
    """Press a keyboard key on the page.

    Use this to submit forms (Enter), navigate (Tab), close dialogs (Escape), etc.

    Args:
        key: Key to press. Examples:
            - "Enter" - submit form
            - "Tab" - next field
            - "Escape" - close dialog
            - "ArrowDown", "ArrowUp" - navigate lists
            - "Backspace", "Delete" - delete text
            - Modifiers: "Control+a", "Shift+Tab", "Meta+Enter"
        selector: Optional element to focus first before pressing key

    Returns:
        JSON with status.
    """
    try:
        page = await _get_page()

        if selector:
            await page.focus(selector)

        await page.keyboard.press(key)

        return json.dumps(
            {
                "status": "ok",
                "key": key,
                "selector": selector,
            }
        )

    except Exception as exc:
        return json.dumps(
            {
                "status": "error",
                "key": key,
                "message": str(exc),
            }
        )


@mcp.tool("browser_extract")  # type: ignore[misc]
async def browser_extract(
    selector: str | None = None,
    content_type: str = "text",
    limit: int = 5000,
) -> str:
    """Extract content from the page.

    Args:
        selector: CSS selector to extract from (None = entire page)
        content_type: What to extract:
            - "text": Visible text content (default)
            - "html": Inner HTML
            - "value": Input value
            - "attribute:name": Specific attribute
        limit: Maximum characters to return (default: 5000)

    Returns:
        JSON with status and extracted content.
    """
    try:
        page = await _get_page()

        if selector:
            element = await page.query_selector(selector)
            if not element:
                return json.dumps(
                    {
                        "status": "error",
                        "message": f"Element not found: {selector}",
                    }
                )

            if content_type == "text":
                content = await element.inner_text()
            elif content_type == "html":
                content = await element.inner_html()
            elif content_type == "value":
                content = await element.input_value()
            elif content_type.startswith("attribute:"):
                attr_name = content_type.split(":", 1)[1]
                content = await element.get_attribute(attr_name) or ""
            else:
                content = await element.inner_text()
        else:
            if content_type == "text":
                content = await page.inner_text("body")
            elif content_type == "html":
                content = await page.content()
            else:
                content = await page.inner_text("body")

        truncated = len(content) > limit
        content = content[:limit]

        return json.dumps(
            {
                "status": "ok",
                "selector": selector or "body",
                "content_type": content_type,
                "content": content,
                "truncated": truncated,
            }
        )

    except Exception as exc:
        return json.dumps(
            {
                "status": "error",
                "message": str(exc),
            }
        )


@mcp.tool("browser_wait")  # type: ignore[misc]
async def browser_wait(
    selector: str | None = None,
    state: str = "visible",
    timeout_ms: int = 10000,
) -> str:
    """Wait for a condition on the page.

    Args:
        selector: CSS selector to wait for (None = just wait timeout_ms)
        state: Element state to wait for:
            - "visible": Element is visible (default)
            - "hidden": Element is hidden or removed
            - "attached": Element exists in DOM
        timeout_ms: Maximum wait time

    Returns:
        JSON with status and elapsed time.
    """
    try:
        page = await _get_page()
        start = time.time()

        if selector:
            await page.wait_for_selector(
                selector,
                state=state,
                timeout=timeout_ms,
            )
        else:
            await asyncio.sleep(timeout_ms / 1000)

        elapsed_ms = int((time.time() - start) * 1000)

        return json.dumps(
            {
                "status": "ok",
                "selector": selector,
                "state": state,
                "elapsed_ms": elapsed_ms,
            }
        )

    except Exception as exc:
        return json.dumps(
            {
                "status": "error",
                "selector": selector,
                "message": str(exc),
            }
        )


@mcp.tool("browser_screenshot")  # type: ignore[misc]
async def browser_screenshot(
    selector: str | None = None,
    full_page: bool = False,
    path: str | None = None,
) -> str:
    """Take a screenshot of the page or a specific element.

    Args:
        selector: CSS selector for element to screenshot (None = viewport)
        full_page: Capture entire scrollable page (ignored if selector provided)
        path: Save path. If None, saves to temp file.

    Returns:
        JSON with status and file path.
    """
    try:
        page = await _get_page()

        if path:
            save_path = Path(path)
        else:
            save_path = (
                Path(tempfile.gettempdir()) / f"screenshot_{int(time.time())}.png"
            )

        if selector:
            element = await page.query_selector(selector)
            if not element:
                return json.dumps(
                    {
                        "status": "error",
                        "message": f"Element not found: {selector}",
                    }
                )
            await element.screenshot(path=str(save_path))
        else:
            await page.screenshot(path=str(save_path), full_page=full_page)

        return json.dumps(
            {
                "status": "ok",
                "path": str(save_path),
                "selector": selector,
                "full_page": full_page if not selector else False,
            }
        )

    except Exception as exc:
        return json.dumps(
            {
                "status": "error",
                "message": str(exc),
            }
        )


@mcp.tool("browser_close")  # type: ignore[misc]
async def browser_close() -> str:
    """Close the browser and disconnect.

    Closes the app-mode window entirely.

    Returns:
        JSON with status.
    """
    try:
        await _close_browser()

        return json.dumps(
            {
                "status": "ok",
                "message": "Browser closed",
            }
        )

    except Exception as exc:
        return json.dumps(
            {
                "status": "error",
                "message": str(exc),
            }
        )


# =============================================================================
# Server Entry Point
# =============================================================================


def run(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = DEFAULT_HTTP_PORT,
) -> None:  # pragma: no cover - integration entrypoint
    """Run the MCP server with the specified transport."""
    if transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=host,
            port=port,
            json_response=True,
            stateless_http=True,
            uvicorn_config={"access_log": False},
        )
    else:
        mcp.run(transport="stdio")


def main() -> None:  # pragma: no cover - CLI helper
    parser = argparse.ArgumentParser(description="Playwright MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport protocol to use",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind HTTP server to",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help="Port for HTTP server",
    )
    args = parser.parse_args()
    run(args.transport, args.host, args.port)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "mcp",
    "run",
    "browser_status",
    "browser_open",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_press_key",
    "browser_extract",
    "browser_wait",
    "browser_screenshot",
    "browser_close",
]
