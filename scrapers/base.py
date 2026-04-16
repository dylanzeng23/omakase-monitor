import asyncio
import logging
import random
from abc import ABC, abstractmethod
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

CONTEXT_DIR = Path(__file__).parent.parent / "browser_data"


class BaseScraper(ABC):
    name: str = "base"

    def __init__(self, headless: bool = True):
        self._headless = headless
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._stealth = Stealth()

    async def start(self):
        CONTEXT_DIR.mkdir(exist_ok=True)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        state_path = self._storage_state_path()
        self._context = await self._browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="Asia/Tokyo",
            storage_state=str(state_path) if state_path.exists() else None,
        )
        await self._stealth.apply_stealth_async(self._context)
        # Block images/fonts/media to speed up loads
        await self._context.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,eot}", lambda route: route.abort())

    async def stop(self):
        if self._context:
            try:
                await self._context.storage_state(path=str(self._storage_state_path()))
            except Exception:
                pass
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    def _storage_state_path(self) -> Path:
        return CONTEXT_DIR / f"{self.name}_state.json"

    async def new_page(self) -> Page:
        return await self._context.new_page()

    async def random_delay(self, min_sec: float = 15.0, max_sec: float = 25.0):
        delay = random.uniform(min_sec, max_sec)
        logger.debug(f"Waiting {delay:.1f}s...")
        await asyncio.sleep(delay)

    async def human_type(self, page: Page, selector: str, text: str):
        """Type text with human-like delays between keystrokes."""
        await page.click(selector)
        await asyncio.sleep(random.uniform(0.1, 0.3))
        for char in text:
            await page.keyboard.type(char, delay=random.randint(50, 150))

    async def save_debug_screenshot(self, page: Page, name: str):
        """Save a debug screenshot."""
        path = Path(__file__).parent.parent / f"debug_{name}.png"
        await page.screenshot(path=str(path))
        logger.info(f"Saved debug screenshot: {path}")

    async def ensure_logged_in(self, page: Page) -> bool:
        """Check if we're logged into omakase.in. Returns True if logged in."""
        # Check if "Log in" link is visible (means NOT logged in)
        login_link = await page.query_selector('a[href="/en/users/sign_in"]')
        return login_link is None
