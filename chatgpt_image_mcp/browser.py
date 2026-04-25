from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

# patchright is a drop-in undetected fork of playwright; it patches the
# fingerprint leaks (navigator.webdriver, CDP Runtime.enable, etc.) that
# Cloudflare / Turnstile / DataDome use to flag automation.
from patchright.async_api import BrowserContext, Page, Playwright, async_playwright


WORKSPACE = Path(__file__).resolve().parents[1]
STATE_DIR = WORKSPACE / ".chatgpt-image-mcp"
PROFILE_RUNS_DIR = STATE_DIR / "runs"
OUTPUT_ROOT = WORKSPACE / "output" / "chatgpt-images"
CHATGPT_URL = "https://chatgpt.com/"


class LoginRequired(RuntimeError):
    """Raised when no usable ChatGPT session is available."""


class ImageGenerationRefused(RuntimeError):
    """Raised when ChatGPT refuses or cannot create an image."""


@dataclass
class GeneratedImage:
    path: str
    mime_type: str
    width: int
    height: int
    source_url: str


@dataclass
class GenerateResult:
    prompt: str
    output_dir: str
    images: list[GeneratedImage]
    session_reused: bool
    conversation_mode: str

    @property
    def primary_image(self) -> GeneratedImage:
        return self.images[0]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["primary_image"] = asdict(self.primary_image)
        return data


def _now_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_slug(text: str, max_len: int = 42) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return (slug or "image")[:max_len].strip("-") or "image"


def _guess_mime(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "image/png"


def _find_chrome() -> str:
    candidates = [
        os.environ.get("CHROME_PATH"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe"),
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise FileNotFoundError("Chrome or Edge executable not found. Set CHROME_PATH to the browser executable.")


# Off-screen launch position. We intentionally do NOT minimize or SW_HIDE the
# window, because that flips `document.visibilityState` to 'hidden' and
# ChatGPT pauses streaming/animations in that state, which breaks our poll
# for the generated image. A far off-screen position keeps visibilityState
# 'visible' while the window is invisible to the user. On Windows we also
# remove the taskbar entry after the window is moved off-screen.
HIDDEN_WINDOW_LEFT = -32000
HIDDEN_WINDOW_TOP = -32000
HIDDEN_WINDOW_WIDTH = 1440
HIDDEN_WINDOW_HEIGHT = 960
HIDDEN_WINDOW_ARGS = [
    f"--window-position={HIDDEN_WINDOW_LEFT},{HIDDEN_WINDOW_TOP}",
    f"--window-size={HIDDEN_WINDOW_WIDTH},{HIDDEN_WINDOW_HEIGHT}",
]


def _env_hidden_default() -> bool:
    return os.environ.get("CHATGPT_HIDE_WINDOW", "").strip().lower() in {"1", "true", "yes", "on"}


def _remove_offscreen_window_from_taskbar() -> int:
    """Remove matching off-screen Chromium windows from the Windows taskbar."""
    if os.name != "nt":
        return 0

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    enum_windows_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    GWL_EXSTYLE = -20
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_TOOLWINDOW = 0x00000080
    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    SWP_FRAMECHANGED = 0x0020

    matches: list[int] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        offscreen_match = (
            abs(rect.left - HIDDEN_WINDOW_LEFT) <= 80
            and abs(rect.top - HIDDEN_WINDOW_TOP) <= 80
            and abs(width - HIDDEN_WINDOW_WIDTH) <= 120
            and abs(height - HIDDEN_WINDOW_HEIGHT) <= 120
        )
        if offscreen_match:
            matches.append(hwnd)
        return True

    user32.EnumWindows(enum_windows_proc(callback), 0)
    for hwnd in matches:
        exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        exstyle = (exstyle & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW
        # A hide/show cycle is the reliable way to force Explorer to drop the
        # taskbar button. The window is immediately restored off-screen.
        user32.ShowWindow(hwnd, SW_HIDE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, exstyle)
        user32.SetWindowPos(
            hwnd,
            0,
            HIDDEN_WINDOW_LEFT,
            HIDDEN_WINDOW_TOP,
            HIDDEN_WINDOW_WIDTH,
            HIDDEN_WINDOW_HEIGHT,
            SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(
            hwnd,
            0,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
    return len(matches)


async def _move_window_offscreen(page: Page) -> None:
    """Move an already-open Chromium window off-screen without minimizing it."""
    cdp = await page.context.new_cdp_session(page)
    window = await cdp.send("Browser.getWindowForTarget")
    window_id = window["windowId"]
    await cdp.send(
        "Browser.setWindowBounds",
        {
            "windowId": window_id,
            "bounds": {"windowState": "normal"},
        },
    )
    await cdp.send(
        "Browser.setWindowBounds",
        {
            "windowId": window_id,
            "bounds": {
                "left": HIDDEN_WINDOW_LEFT,
                "top": HIDDEN_WINDOW_TOP,
                "width": HIDDEN_WINDOW_WIDTH,
                "height": HIDDEN_WINDOW_HEIGHT,
            },
        },
    )
    await page.wait_for_timeout(250)
    await asyncio.to_thread(_remove_offscreen_window_from_taskbar)


@asynccontextmanager
async def _browser_context(*, hidden: bool | None = None) -> AsyncIterator[BrowserContext]:
    """Launch a real Chrome with a fresh temporary profile via patchright.

    Patchright recommends persistent context + the ``chrome`` channel and
    *minimal* launch flags so the resulting fingerprint matches a normal user.

    If ``hidden`` is True (or the ``CHATGPT_HIDE_WINDOW`` env var is set),
    the Chrome window is launched off-screen so no UI is visible.
    """
    if hidden is None:
        hidden = _env_hidden_default()
    PROFILE_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(tempfile.mkdtemp(prefix="run-", dir=PROFILE_RUNS_DIR))
    common_kwargs: dict = dict(
        user_data_dir=str(profile_dir),
        headless=False,
        accept_downloads=True,
        viewport={"width": 1440, "height": 960},
        # Patchright stealth recommendations: do not pass automation flags,
        # no --disable-blink-features, no --no-first-run, etc.
    )
    if hidden:
        common_kwargs["args"] = list(HIDDEN_WINDOW_ARGS)
    async with async_playwright() as pw:
        context: BrowserContext
        try:
            context = await pw.chromium.launch_persistent_context(
                channel="chrome",
                **common_kwargs,
            )
        except Exception:
            # Fall back to an explicit Chrome/Edge executable if the channel is unavailable.
            executable = _find_chrome()
            context = await pw.chromium.launch_persistent_context(
                executable_path=executable,
                **common_kwargs,
            )

        try:
            yield context
        finally:
            try:
                await context.close()
            except Exception:
                pass
            shutil.rmtree(profile_dir, ignore_errors=True)


async def _page_state(page: Page) -> dict:
    return await page.evaluate(
        """() => ({
            title: document.title,
            url: location.href,
            text: (document.body?.innerText || '').slice(0, 3000)
        })"""
    )


async def _mark_best_composer(page: Page) -> str | None:
    return await page.evaluate(
        """() => {
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width >= 250 && r.height >= 24 &&
                    s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
            };
            const score = (el) => {
                const r = el.getBoundingClientRect();
                return r.top * 20 + r.width * r.height + (el.tagName === 'TEXTAREA' ? 10000 : 0);
            };
            const candidates = Array.from(document.querySelectorAll('textarea,[contenteditable="true"],[role="textbox"]'))
                .filter(visible)
                .filter((el) => !`${el.getAttribute('aria-label') || ''} ${el.getAttribute('placeholder') || ''}`.toLowerCase().includes('search'))
                .sort((a, b) => score(b) - score(a));
            const chosen = candidates[0];
            if (!chosen) return null;
            const token = `chatgpt-image-composer-${Date.now()}-${Math.random().toString(36).slice(2)}`;
            chosen.setAttribute('data-chatgpt-image-composer', token);
            return `[data-chatgpt-image-composer="${token}"]`;
        }"""
    )


async def _composer_ready(page: Page) -> bool:
    return await _composer_selector_if_ready(page) is not None


async def _composer_selector_if_ready(page: Page) -> str | None:
    selector = await _mark_best_composer(page)
    if not selector:
        return None
    state = await _page_state(page)
    if re.search(r"log in|sign up|continue with google|continue with apple", state["text"], re.I):
        return None
    return selector


def _looks_blocked(state: dict) -> bool:
    haystack = f"{state.get('title', '')}\n{state.get('url', '')}\n{state.get('text', '')}"
    return bool(re.search(r"verify you are human|just a moment|captcha|cloudflare|security check", haystack, re.I))


def _generation_failure_reason(state: dict) -> str | None:
    text = str(state.get("text") or "")
    patterns = [
        r"image we created may violate our guardrails",
        r"may violate our guardrails",
        r"similarity to third-party content",
        r"we're so sorry",
        r"we(?: are|'re) unable to generate",
        r"couldn(?:'|’)t generate",
        r"could not generate",
        r"cannot generate",
        r"can(?:not|'t) create",
        r"unable to create",
        r"this request (?:may )?violate",
        r"content policy",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            start = max(0, match.start() - 180)
            end = min(len(text), match.end() + 360)
            return re.sub(r"\s+", " ", text[start:end]).strip()
    return None


async def _wait_for_composer(page: Page, *, timeout_seconds: int) -> str:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_state: dict | None = None
    while asyncio.get_running_loop().time() < deadline:
        selector = await _composer_selector_if_ready(page)
        if selector:
            return selector
        try:
            last_state = await _page_state(page)
        except Exception:
            last_state = None
        await page.wait_for_timeout(2000)

    if last_state and _looks_blocked(last_state):
        raise LoginRequired("Timed out waiting for ChatGPT verification. Complete verification/login faster, then run the command again.")
    raise LoginRequired("Timed out waiting for ChatGPT login. Log in in the opened browser before the login timeout.")


async def _open_new_chat(page: Page) -> None:
    await _wait_for_composer(page, timeout_seconds=30)
    selector = await page.evaluate(
        """() => {
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width >= 20 && r.height >= 20 &&
                    s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
            };
            const candidates = Array.from(document.querySelectorAll('a,button'))
                .filter(visible)
                .filter((el) => {
                    const label = `${el.getAttribute('aria-label') || ''} ${el.textContent || ''}`.trim().toLowerCase();
                    return /^new chat$/.test(label) || label.includes('new chat');
                });
            const chosen = candidates[0];
            if (!chosen) return null;
            const token = `chatgpt-image-new-chat-${Date.now()}-${Math.random().toString(36).slice(2)}`;
            chosen.setAttribute('data-chatgpt-image-new-chat', token);
            return `[data-chatgpt-image-new-chat="${token}"]`;
        }"""
    )
    if selector:
        await page.locator(selector).first.click(force=True)
        await page.wait_for_timeout(1500)
    else:
        await page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(1500)
    await _wait_for_composer(page, timeout_seconds=30)


async def _collect_images(page: Page) -> list[dict]:
    return await page.evaluate(
        """() => {
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width >= 160 && r.height >= 160 &&
                    s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
            };
            return Array.from(document.querySelectorAll('main img'))
                .filter(visible)
                .map((img) => {
                    const r = img.getBoundingClientRect();
                    return {
                        src: img.currentSrc || img.src,
                        alt: img.alt || '',
                        naturalWidth: img.naturalWidth || 0,
                        naturalHeight: img.naturalHeight || 0,
                        displayWidth: Math.round(r.width),
                        displayHeight: Math.round(r.height),
                    };
                })
                .filter((img) => img.src && img.naturalWidth >= 256 && img.naturalHeight >= 256)
                .filter((img) => !/avatar|profile|icon|logo/i.test(img.alt));
        }"""
    )


async def _read_composer_text(page: Page, selector: str) -> str:
    locator = page.locator(selector).first
    return await locator.evaluate(
        """(el) => el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement
            ? el.value
            : (el.innerText || el.textContent || '')"""
    )


async def _click_send_near(page: Page, composer_selector: str) -> None:
    selector = await page.evaluate(
        """(sourceSelector) => {
            const source = document.querySelector(sourceSelector);
            if (!source) return null;
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width >= 20 && r.height >= 20 &&
                    s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
            };
            const sr = source.getBoundingClientRect();
            const sx = sr.left + sr.width / 2;
            const sy = sr.top + sr.height / 2;
            const candidates = Array.from(document.querySelectorAll('button'))
                .filter((button) => visible(button) && !button.disabled && button.getAttribute('aria-disabled') !== 'true')
                .map((button) => {
                    const r = button.getBoundingClientRect();
                    const label = `${button.getAttribute('aria-label') || ''} ${button.textContent || ''}`.toLowerCase();
                    return {
                        button,
                        dist: Math.hypot(r.left + r.width / 2 - sx, r.top + r.height / 2 - sy) +
                            (/send|submit|up arrow/.test(label) ? -300 : 0)
                    };
                })
                .sort((a, b) => a.dist - b.dist);
            const chosen = candidates[0]?.button;
            if (!chosen) return null;
            const token = `chatgpt-image-send-${Date.now()}-${Math.random().toString(36).slice(2)}`;
            chosen.setAttribute('data-chatgpt-image-send', token);
            return `[data-chatgpt-image-send="${token}"]`;
        }""",
        composer_selector,
    )
    if selector:
        await page.locator(selector).first.click(force=True)


async def _export_image_from_src(page: Page, image: dict, path_without_ext: Path) -> GeneratedImage:
    payload = await page.evaluate(
        """async (src) => {
            const response = await fetch(src);
            if (!response.ok) throw new Error(`fetch failed ${response.status}`);
            const blob = await response.blob();
            const dataUrl = await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = () => resolve(reader.result);
                reader.onerror = () => reject(new Error('failed to read image blob'));
                reader.readAsDataURL(blob);
            });
            return { dataUrl, mimeType: blob.type || 'image/png' };
        }""",
        image["src"],
    )
    match = re.match(r"^data:([^;]+);base64,(.+)$", payload["dataUrl"], re.S)
    if not match:
        raise RuntimeError("Image fetch did not return a data URL.")
    mime_type = payload.get("mimeType") or match.group(1)
    ext = {"image/jpeg": ".jpg", "image/webp": ".webp", "image/png": ".png"}.get(mime_type, ".png")
    out_path = path_without_ext.with_suffix(ext)
    out_path.write_bytes(base64.b64decode(match.group(2)))
    return GeneratedImage(
        path=str(out_path),
        mime_type=mime_type,
        width=int(image["naturalWidth"]),
        height=int(image["naturalHeight"]),
        source_url=image["src"],
    )


async def _send_prompt_and_export(
    page: Page,
    prompt: str,
    *,
    timeout_seconds: int,
    max_images: int,
    session_reused: bool,
    mode: str,
    conversation_mode: str = "new",
) -> GenerateResult:
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("Prompt cannot be empty.")

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    out_dir = OUTPUT_ROOT / f"{_now_slug()}-{_safe_slug(prompt)}"
    out_dir.mkdir(parents=True, exist_ok=True)

    page.set_default_timeout(30_000)
    if conversation_mode == "new":
        await _open_new_chat(page)
    elif conversation_mode == "continue":
        await _wait_for_composer(page, timeout_seconds=30)
    else:
        raise ValueError("conversation_mode must be 'new' or 'continue'.")
    composer_selector = await _wait_for_composer(page, timeout_seconds=30)
    known_sources = {image["src"] for image in await _collect_images(page)}

    composer = page.locator(composer_selector).first
    await composer.click(force=True)
    try:
        await composer.fill(prompt)
    except Exception:
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.insert_text(prompt)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(1200)
    if (await _read_composer_text(page, composer_selector)).strip():
        await _click_send_near(page, composer_selector)

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    exported: list[GeneratedImage] = []
    while asyncio.get_running_loop().time() < deadline:
        state = await _page_state(page)
        if _looks_blocked(state):
            await page.wait_for_timeout(2500)
            continue
        failure_reason = _generation_failure_reason(state)
        if failure_reason:
            diagnostic = out_dir / "diagnostic-refusal.png"
            await page.screenshot(path=str(diagnostic), full_page=True)
            raise ImageGenerationRefused(f"{failure_reason} Diagnostic screenshot: {diagnostic}")

        fresh = [image for image in await _collect_images(page) if image["src"] not in known_sources]
        if not fresh:
            await page.wait_for_timeout(2500)
            continue

        await page.wait_for_timeout(1500)
        final_fresh = [image for image in await _collect_images(page) if image["src"] not in known_sources] or fresh
        final_fresh.sort(key=lambda image: image["naturalWidth"] * image["naturalHeight"], reverse=True)
        for index, image in enumerate(final_fresh[:max_images], start=1):
            exported.append(await _export_image_from_src(page, image, out_dir / f"image-{index:02d}"))
        break

    if not exported:
        diagnostic = out_dir / "diagnostic-timeout.png"
        await page.screenshot(path=str(diagnostic), full_page=True)
        raise TimeoutError(f"Timed out waiting for an image. Diagnostic screenshot: {diagnostic}")

    metadata = {
        "prompt": prompt,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "images": [asdict(image) for image in exported],
        "session_reused": session_reused,
        "mode": mode,
        "conversation_mode": conversation_mode,
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    return GenerateResult(
        prompt=prompt,
        output_dir=str(out_dir),
        images=exported,
        session_reused=session_reused,
        conversation_mode=conversation_mode,
    )


class ChatGPTBrowserSession:
    """A long-lived browser/page pair reused by MCP tool calls."""

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._profile_dir: Path | None = None
        self._lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    @property
    def profile_dir(self) -> str | None:
        return str(self._profile_dir) if self._profile_dir else None

    async def start(self, *, wait_for_enter: bool, login_timeout_seconds: int = 900) -> dict:
        if self.ready:
            return await self.status()

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PROFILE_RUNS_DIR.mkdir(parents=True, exist_ok=True)
        self._profile_dir = Path(tempfile.mkdtemp(prefix="mcp-", dir=PROFILE_RUNS_DIR))
        self._pw = await async_playwright().start()
        common_kwargs: dict = dict(
            user_data_dir=str(self._profile_dir),
            headless=False,
            accept_downloads=True,
            viewport={"width": 1440, "height": 960},
        )
        hide_window = _env_hidden_default()
        # If login must be done manually, launch visibly first. We move the
        # same long-lived window off-screen after ChatGPT is ready.
        if hide_window and not wait_for_enter:
            common_kwargs["args"] = list(HIDDEN_WINDOW_ARGS)
        try:
            self._context = await self._pw.chromium.launch_persistent_context(channel="chrome", **common_kwargs)
        except Exception:
            executable = _find_chrome()
            self._context = await self._pw.chromium.launch_persistent_context(executable_path=executable, **common_kwargs)

        self._page = await self._context.new_page()
        self._page.set_default_timeout(30_000)
        await self._page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=90_000)
        await self._page.wait_for_timeout(2500)

        if not await _composer_ready(self._page):
            print("Browser opened. Log in to ChatGPT or complete verification there.", file=sys.stderr)
            if wait_for_enter:
                print("When the normal ChatGPT composer is visible, return here and press Enter.", file=sys.stderr)
                await asyncio.to_thread(input, "Press Enter after ChatGPT is ready...")
            else:
                print("Waiting automatically until the normal ChatGPT composer is visible.", file=sys.stderr)

        await _wait_for_composer(self._page, timeout_seconds=login_timeout_seconds)
        if hide_window:
            await _move_window_offscreen(self._page)
        return await self.status()

    async def status(self) -> dict:
        return {
            "ready": self.ready,
            "mode": "long-lived-browser",
            "session_reused": self.ready,
            "profile_dir": self.profile_dir,
        }

    async def generate(
        self,
        prompt: str,
        *,
        timeout_seconds: int = 420,
        max_images: int = 1,
        conversation_mode: str = "new",
    ) -> GenerateResult:
        if not self.ready or self._page is None:
            raise LoginRequired("MCP browser session is not ready. Start the MCP server and complete ChatGPT login first.")
        async with self._lock:
            await _wait_for_composer(self._page, timeout_seconds=30)
            return await _send_prompt_and_export(
                self._page,
                prompt,
                timeout_seconds=timeout_seconds,
                max_images=max_images,
                session_reused=True,
                mode="long-lived-browser",
                conversation_mode=conversation_mode,
            )

    async def close(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
        finally:
            self._context = None
            self._page = None
            try:
                if self._pw is not None:
                    await self._pw.stop()
            finally:
                self._pw = None
                if self._profile_dir is not None:
                    shutil.rmtree(self._profile_dir, ignore_errors=True)
                    self._profile_dir = None


async def login_interactive(*, force: bool = False) -> dict:
    """Open a fresh ChatGPT browser and wait until the composer is usable."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    async with _browser_context() as context:
        page = await context.new_page()
        await page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(2500)
        if not await _composer_ready(page):
            print("Fresh browser opened. Log in to ChatGPT or complete verification there.")
            print("This command will continue automatically when the normal ChatGPT composer is visible.")
        await _wait_for_composer(page, timeout_seconds=600)
        return {
            "ready": True,
            "mode": "fresh-browser-login-each-run",
            "session_saved": False,
            "force_ignored": force,
        }


async def status() -> dict:
    return {
        "ready": True,
        "cli_generate_mode": "fresh-browser-login-each-run",
        "mcp_server_mode": "thin-client-to-browser-daemon",
        "note": "Use `browser-daemon` to keep a logged-in browser alive across MCP server restarts.",
    }


async def generate(
    prompt: str,
    *,
    timeout_seconds: int = 420,
    max_images: int = 1,
    login_timeout_seconds: int = 600,
    conversation_mode: str = "new",
) -> GenerateResult:
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("Prompt cannot be empty.")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    async with _browser_context() as context:
        page = await context.new_page()
        page.set_default_timeout(30_000)
        await page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_timeout(2500)
        await _wait_for_composer(page, timeout_seconds=login_timeout_seconds)
        return await _send_prompt_and_export(
            page,
            prompt,
            timeout_seconds=timeout_seconds,
            max_images=max_images,
            session_reused=False,
            mode="fresh-browser-login-each-run",
            conversation_mode=conversation_mode,
        )


def image_as_base64(image_path: str) -> tuple[str, str]:
    path = Path(image_path)
    return base64.b64encode(path.read_bytes()).decode("ascii"), _guess_mime(path)
