"""Overcast web scraping client.

All Overcast data is fetched by scraping the web interface at overcast.fm.
There is no official API. HTML selectors are isolated here so breakage
from site changes is contained to this file.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import re
from typing import Any, TypeVar

import aiohttp
from bs4 import BeautifulSoup
from yarl import URL

from .const import (
    DEFAULT_SPEED_ID,
    LOGIN_URL,
    OVERCAST_BASE_URL,
    PODCASTS_URL,
    PROGRESS_FINISHED_SENTINEL,
    QR_VERIFY_URL,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CSS selectors — change these when Overcast updates its HTML
# ---------------------------------------------------------------------------
SEL_FEEDCELL = 'a.feedcell[href^="/p"], a.feedcell[href^="/itunes"]'
SEL_EPISODE_CELL = "a.extendedepisodecell"
SEL_AUDIO_PLAYER = "audio#audioplayer"
SEL_AUDIO_SOURCE = "audio#audioplayer source"
SEL_PODCAST_TITLE = "h2.centertext"
SEL_PODCAST_ART = "img.art.fullart"
SEL_UNPLAYED_INDICATOR = "svg.unplayed_indicator"


OVERCAST_HOST = "overcast.fm"
EPISODE_PATH_RE = re.compile(r"^/\+[\w-]+$", re.ASCII)
# Podcast pages: /p<id>-<slug> or /itunes<id>[/<slug>]
FEED_PATH_RE = re.compile(r"^/(p\d+-[\w-]+|itunes\d+(/[^/?#@\\\s]*)?)$", re.ASCII)


class OvercastInvalidURLError(ValueError):
    """A URL or path does not point at an overcast.fm page we accept."""


def episode_path_from_url(value: str) -> str:
    """Return the /+<id> path of an overcast.fm episode URL.

    Every request carries the session cookie, so a URL that is not exactly
    https://overcast.fm/+<id> must be refused. Plain string handling let
    "https://overcast.fm.evil.com/+x" or "https://overcast.fm@evil.com/+x"
    through and sent the cookie to another host.
    """
    value = value.strip()
    if value.startswith("/") and not value.startswith("//"):
        path = value
    else:
        try:
            url = URL(value)
        except (ValueError, TypeError) as err:
            raise OvercastInvalidURLError(f"Not a valid URL: {value}") from err
        if (
            url.scheme != "https"
            or url.host != OVERCAST_HOST
            or url.user is not None
            or url.password is not None
            or url.port != 443
        ):
            raise OvercastInvalidURLError(f"Not an overcast.fm URL: {value}")
        path = url.path
    if not EPISODE_PATH_RE.match(path):
        raise OvercastInvalidURLError(f"Not an Overcast episode URL: {value}")
    return path


def _page_url(path: str, pattern: re.Pattern[str]) -> str:
    """Build an overcast.fm URL from a site path, refusing anything else."""
    if not pattern.match(path):
        raise OvercastInvalidURLError(f"Unexpected Overcast path: {path}")
    url = URL(OVERCAST_BASE_URL).with_path(path, encoded=True)
    if url.host != OVERCAST_HOST:
        raise OvercastInvalidURLError(f"Unexpected Overcast path: {path}")
    return str(url)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Podcast:
    """An Overcast podcast subscription."""

    title: str
    feed_path: str  # e.g. /p1958931-zaMlGF or /itunes981330533/slug
    artwork_url: str | None = None
    has_unplayed: bool = False


@dataclass
class Episode:
    """An Overcast episode."""

    title: str
    episode_path: str  # e.g. /+d5BN-RlCs
    caption: str = ""
    description: str = ""
    is_played: bool = False

    # Populated from the episode detail page
    item_id: str | None = None
    audio_url: str | None = None
    mime_type: str | None = None
    start_time: int = 0
    sync_version: int = 0
    speed_id: int = DEFAULT_SPEED_ID
    saved_for_user: bool = True
    artwork_url: str | None = None
    podcast_title: str | None = None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class OvercastError(Exception):
    """Base class for Overcast client errors."""


class OvercastAuthError(OvercastError):
    """Authentication failed or session expired."""


class OvercastConnectionError(OvercastError):
    """Cannot reach overcast.fm, or it returned a server error."""


class OvercastParseError(OvercastError):
    """HTML structure changed and scraping failed."""


# ---------------------------------------------------------------------------
# HTML parsing (pure functions, run in an executor for big pages)
# ---------------------------------------------------------------------------
def _absolute(url: str | None) -> str | None:
    """Make a scraped src/href absolute against overcast.fm."""
    if not url:
        return None
    return str(URL(OVERCAST_BASE_URL).join(URL(url)))


def parse_subscriptions(html: str) -> list[Podcast]:
    """Parse the /podcasts page."""
    soup = BeautifulSoup(html, "html.parser")
    podcasts: list[Podcast] = []
    for cell in soup.select(SEL_FEEDCELL):
        href = cell.get("href", "")
        if not href:
            continue
        title_div = cell.select_one(".title")
        title = title_div.get_text(strip=True) if title_div else cell.get_text(strip=True)
        img = cell.select_one("img.art")
        podcasts.append(
            Podcast(
                title=title,
                feed_path=href,
                artwork_url=_absolute(img.get("src") if img else None),
                has_unplayed=cell.select_one(SEL_UNPLAYED_INDICATOR) is not None,
            )
        )
    return podcasts


def parse_episodes(html: str) -> list[Episode]:
    """Parse a podcast page into its episode list."""
    soup = BeautifulSoup(html, "html.parser")
    episodes: list[Episode] = []
    for cell in soup.select(SEL_EPISODE_CELL):
        href = cell.get("href", "")
        if not href:
            continue
        title_div = cell.select_one(".title")
        caption_div = cell.select_one(".caption2")
        desc_div = cell.select_one(".lighttext")
        episodes.append(
            Episode(
                title=title_div.get_text(strip=True) if title_div else "",
                episode_path=href,
                caption=caption_div.get_text(strip=True) if caption_div else "",
                description=desc_div.get_text(strip=True) if desc_div else "",
                is_played="userdeletedepisode" in cell.get("class", []),
            )
        )
    return episodes


def parse_episode_detail(html: str, episode_path: str) -> Episode:
    """Parse an episode page: audio URL, item id and sync metadata."""
    soup = BeautifulSoup(html, "html.parser")
    audio = soup.select_one(SEL_AUDIO_PLAYER)
    if not audio:
        raise OvercastParseError(f"No audio player found on {episode_path}")

    source = soup.select_one(SEL_AUDIO_SOURCE)
    audio_url = source.get("src") if source else None
    mime_type = source.get("type") if source else None

    og_title = soup.find("meta", property="og:title")
    og_image = soup.find("meta", property="og:image")
    title_text = og_title.get("content", "") if og_title else ""
    parts = title_text.split(" \u2014 ", 1)  # "<episode> — <podcast>"
    ep_title = parts[0].strip() if parts else title_text
    pod_title = parts[1].strip() if len(parts) > 1 else None

    try:
        return Episode(
            title=ep_title,
            episode_path=episode_path,
            podcast_title=pod_title,
            audio_url=audio_url,
            mime_type=mime_type or None,
            item_id=audio.get("data-item-id"),
            start_time=int(audio.get("data-start-time", "0") or 0),
            sync_version=int(audio.get("data-sync-version", "0") or 0),
            speed_id=int(audio.get("data-speed-id", str(DEFAULT_SPEED_ID)) or 0),
            saved_for_user=audio.get("data-saved-for-user") == "1",
            artwork_url=og_image.get("content") if og_image else None,
        )
    except ValueError as err:
        raise OvercastParseError(f"Bad audio player data on {episode_path}") from err


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
_T = TypeVar("_T")
Executor = Callable[..., Awaitable[Any]]

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


def _is_login_redirect(resp: aiohttp.ClientResponse) -> bool:
    if resp.url.path == "/login":
        return True
    if resp.status in (301, 302, 303, 307, 308):
        return "/login" in resp.headers.get("Location", "")
    return False


def _cookie_from(resp: aiohttp.ClientResponse, session: aiohttp.ClientSession) -> str | None:
    """Find the o= session cookie in a response or the session's jar."""
    for name, cookie in resp.cookies.items():
        if name == "o" and cookie.value and cookie.value != "-":
            return cookie.value
    for cookie in session.cookie_jar:
        if cookie.key == "o" and cookie.value and cookie.value != "-":
            return cookie.value
    return None


class OvercastClient:
    """Async client for the Overcast web interface."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        cookie: str | None = None,
        executor: Executor | None = None,
    ) -> None:
        self._session = session
        self._cookie = cookie
        self._executor = executor

    @property
    def cookie(self) -> str | None:
        return self._cookie

    @cookie.setter
    def cookie(self, value: str) -> None:
        self._cookie = value

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": USER_AGENT}
        if self._cookie:
            headers["Cookie"] = f"o={self._cookie}"
        return headers

    async def _parse(self, func: Callable[..., _T], *args: Any) -> _T:
        """Run a BeautifulSoup parser off the event loop."""
        if self._executor is not None:
            return await self._executor(func, *args)
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    async def _get_page(self, url: str) -> str:
        """GET an authenticated page and return its HTML."""
        try:
            async with self._session.get(
                url, headers=self._headers(), timeout=REQUEST_TIMEOUT
            ) as resp:
                if _is_login_redirect(resp):
                    raise OvercastAuthError("Session expired, redirected to login")
                if resp.status >= 400:
                    raise OvercastConnectionError(f"HTTP {resp.status} from {url}")
                return await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OvercastConnectionError(str(err) or type(err).__name__) from err

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def login_email(self, email: str, password: str) -> str:
        """Log in with email/password. Returns the session cookie value."""
        try:
            async with self._session.post(
                LOGIN_URL,
                data={"email": email, "password": password},
                headers={"User-Agent": USER_AGENT},
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status >= 500:
                    raise OvercastConnectionError(f"HTTP {resp.status} from Overcast")
                cookie_val = _cookie_from(resp, self._session)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OvercastConnectionError(f"Cannot connect to Overcast: {err}") from err

        if not cookie_val:
            raise OvercastAuthError("Invalid email or password")

        self._cookie = cookie_val
        return cookie_val

    async def start_qr_login(self) -> str:
        """Start a QR login and return its token.

        GET /login sets an HttpOnly qr= cookie. The session that polls
        /main/login_qr_verify must send that cookie back, so the same
        session (with a real cookie jar) has to be used for the whole login.
        """
        try:
            async with self._session.get(
                LOGIN_URL,
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status >= 400:
                    raise OvercastConnectionError(f"HTTP {resp.status} from Overcast")
                qr_token = None
                if (qr_cookie := resp.cookies.get("qr")) is not None:
                    qr_token = qr_cookie.value
                if not qr_token or qr_token == "-":
                    text = await resp.text()
                    soup = BeautifulSoup(text, "html.parser")
                    qr_input = soup.find("input", {"name": "token"})
                    if qr_input and qr_input.get("value"):
                        qr_token = qr_input["value"]
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OvercastConnectionError(f"Cannot connect to Overcast: {err}") from err

        if not qr_token or qr_token == "-":
            raise OvercastConnectionError("Could not obtain QR login token")
        return qr_token

    async def poll_qr_login(self, qr_token: str) -> str | None:
        """Check a QR login once. Returns the cookie if approved, None if pending."""
        try:
            async with self._session.post(
                QR_VERIFY_URL,
                data={"token": qr_token, "then": "podcasts"},
                headers={"User-Agent": USER_AGENT},
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                body = await resp.text()
                # An empty body means the app has not approved it yet.
                if not body.strip():
                    return None
                cookie_val = _cookie_from(resp, self._session)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("QR poll failed, will retry: %s", err)
            return None

        if cookie_val:
            self._cookie = cookie_val
        return cookie_val

    async def validate_session(self) -> bool:
        """Return False if the cookie is rejected.

        Raises OvercastConnectionError when Overcast cannot be reached, so
        callers can tell "logged out" from "offline".
        """
        if not self._cookie:
            return False
        try:
            async with self._session.get(
                PODCASTS_URL,
                headers=self._headers(),
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if _is_login_redirect(resp):
                    return False
                if resp.status >= 500:
                    raise OvercastConnectionError(f"HTTP {resp.status} from Overcast")
                return resp.status == 200
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OvercastConnectionError(str(err) or type(err).__name__) from err

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    async def get_subscriptions(self) -> list[Podcast]:
        """Fetch the subscription list from /podcasts."""
        text = await self._get_page(PODCASTS_URL)
        if 'name="password"' in text:
            raise OvercastAuthError("Session expired")
        podcasts = await self._parse(parse_subscriptions, text)
        _LOGGER.debug("Fetched %d subscriptions from Overcast", len(podcasts))
        return podcasts

    async def get_episodes(self, feed_path: str) -> list[Episode]:
        """Fetch episodes for a podcast."""
        text = await self._get_page(_page_url(feed_path, FEED_PATH_RE))
        episodes = await self._parse(parse_episodes, text)
        _LOGGER.debug("Fetched %d episodes for %s", len(episodes), feed_path)
        return episodes

    async def get_episode_detail(self, episode_path: str) -> Episode:
        """Fetch full episode detail including audio URL and sync metadata."""
        text = await self._get_page(_page_url(episode_path, EPISODE_PATH_RE))
        episode = await self._parse(parse_episode_detail, text, episode_path)
        if not episode.saved_for_user:
            _LOGGER.debug(
                "Episode %s is not in the Overcast library; progress may not stick",
                episode_path,
            )
        return episode

    # ------------------------------------------------------------------
    # Progress
    # ------------------------------------------------------------------

    async def set_progress(
        self,
        item_id: str,
        position: int,
        speed_id: int = DEFAULT_SPEED_ID,
        sync_version: int = 0,
    ) -> int:
        """POST progress to Overcast. Returns the new syncVersion."""
        if not item_id.isdigit():
            raise OvercastParseError(f"Unexpected item id: {item_id!r}")
        url = f"{PODCASTS_URL}/set_progress/{item_id}"
        data = {"p": str(position), "speed": str(speed_id), "v": str(sync_version)}
        try:
            async with self._session.post(
                url,
                data=data,
                headers={**self._headers(), "Origin": OVERCAST_BASE_URL},
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if _is_login_redirect(resp):
                    raise OvercastAuthError("Session expired, redirected to login")
                if resp.status >= 400:
                    raise OvercastConnectionError(f"HTTP {resp.status} from set_progress")
                body = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OvercastConnectionError(str(err) or type(err).__name__) from err

        try:
            new_version = int(body.strip())
        except ValueError:
            _LOGGER.warning("Unexpected set_progress response: %s", body[:200])
            new_version = sync_version

        _LOGGER.debug(
            "set_progress(%s): p=%d, v=%d -> %d", item_id, position, sync_version, new_version
        )
        return new_version

    async def mark_episode_played(self, item_id: str, speed_id: int, sync_version: int) -> int:
        """Mark an episode as finished in Overcast."""
        return await self.set_progress(
            item_id, PROGRESS_FINISHED_SENTINEL, speed_id, sync_version
        )
