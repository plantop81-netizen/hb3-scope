"""HTTP 수집기. 인코딩(EUC-KR 등) 자동 판별, robots.txt 존중, 호스트별 간격, 선택적 JS 렌더링."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from bs4.dammit import UnicodeDammit

from .config import settings

log = logging.getLogger(__name__)


@dataclass
class Page:
    url: str
    final_url: str
    status: int
    html: str
    error: str | None = None
    rendered: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300 and bool(self.html)


def decode_body(resp: httpx.Response) -> str:
    raw = resp.content
    ctype = resp.headers.get("content-type", "")
    hinted = None
    m = re.search(r"charset=([\w\-]+)", ctype, re.I)
    if m:
        hinted = m.group(1).lower()
    dammit = UnicodeDammit(raw, known_definite_encodings=[hinted] if hinted else [], is_html=True)
    return dammit.unicode_markup or raw.decode("utf-8", errors="replace")


class Fetcher:
    def __init__(self, concurrency: int | None = None, timeout: float | None = None, respect_robots: bool = True):
        self.sem = asyncio.Semaphore(concurrency or settings.concurrency)
        self.timeout = timeout or settings.request_timeout
        self.respect_robots = respect_robots
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._host_last: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self.client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"},
            follow_redirects=True,
            timeout=self.timeout,
            verify=False,  # 일부 국내 병원 사이트가 오래된 인증서 체인을 쓴다.
        )
        self._browser = None
        self._pw = None

    async def close(self) -> None:
        await self.client.aclose()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = await self.client.get(base + "/robots.txt")
                if r.status_code == 200 and r.text.strip():
                    rp.parse(r.text.splitlines())
                    self._robots[base] = rp
                else:
                    self._robots[base] = None
            except Exception:
                self._robots[base] = None
        rp = self._robots.get(base)
        if rp is None:
            return True
        try:
            return rp.can_fetch(settings.user_agent, url) or rp.can_fetch("*", url)
        except Exception:
            return True

    async def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._host_last.get(host, 0.0)
            wait = settings.per_host_delay - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._host_last[host] = time.monotonic()

    async def get(self, url: str, render: str | None = None) -> Page:
        render = render or settings.render_mode
        async with self.sem:
            if not await self._allowed(url):
                return Page(url, url, 0, "", error="robots.txt disallow")
            await self._throttle(url)
            page = await self._get_http(url)
            if render == "always" or (render == "auto" and page.ok and looks_js_rendered(page.html)):
                rendered = await self._get_rendered(url)
                if rendered and rendered.ok and len(visible_text(rendered.html)) > len(visible_text(page.html)):
                    return rendered
            return page

    async def _get_http(self, url: str) -> Page:
        last_err = None
        for attempt in range(3):
            try:
                r = await self.client.get(url)
                if r.status_code >= 500 and attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                html = decode_body(r) if r.status_code < 400 else ""
                return Page(url, str(r.url), r.status_code, html)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                last_err = f"{type(e).__name__}: {e}"
                await asyncio.sleep(1.5 * (attempt + 1))
            except Exception as e:  # noqa: BLE001
                return Page(url, url, 0, "", error=f"{type(e).__name__}: {e}")
        return Page(url, url, 0, "", error=last_err or "unknown")

    async def _get_rendered(self, url: str) -> Page | None:
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except ImportError:
            return None
        try:
            if self._browser is None:
                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch()
            ctx = await self._browser.new_context(user_agent=settings.user_agent, ignore_https_errors=True)
            pg = await ctx.new_page()
            resp = await pg.goto(url, wait_until="networkidle", timeout=int(self.timeout * 1000))
            await pg.wait_for_timeout(800)
            html = await pg.content()
            await ctx.close()
            return Page(url, pg.url, resp.status if resp else 200, html, rendered=True)
        except Exception as e:  # noqa: BLE001
            log.debug("render failed %s: %s", url, e)
            return None


_JS_HINTS = re.compile(r'id="(root|app|__next|__nuxt)"|ng-app|data-reactroot|vue\.js|react\.', re.I)


def looks_js_rendered(html: str) -> bool:
    text = visible_text(html)
    return len(text) < 400 and bool(_JS_HINTS.search(html))


def visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript", "svg", "iframe", "template"]):
        t.decompose()
    # 표 한 행은 한 줄로 (진료과 | 이름 | 직위 형태를 보존)
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        tr.replace_with(soup.new_string(" | ".join(x for x in cells if x) + "\n"))
    text = soup.get_text("\n")
    lines = [re.sub(r"[ \t　]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def content_hash(text: str) -> str:
    # 날짜/카운터 등 사소한 변화는 무시하도록 공백 정규화 후 해시
    norm = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()
