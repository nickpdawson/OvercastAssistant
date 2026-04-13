"""Overcast web scraping client.

All Overcast data is fetched by scraping the web interface at overcast.fm.
There is no official API. HTML selectors are isolated here so breakage
from site changes is contained to this file.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from bs4 import BeautifulSoup, Tag

from .const import (
    DEFAULT_SPEED_ID,
    LOGIN_URL,
    OPML_EXPORT_URL,
    OVERCAST_BASE_URL,
    PODCASTS_URL,
    PROGRESS_FINISHED_SENTINEL,
    QR_POLL_INITIAL_INTERVAL,
    QR_POLL_MID_INTERVAL,
    QR_POLL_SLOW_INTERVAL,
    QR_POLL_TIMEOUT,
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

    # Populated lazily from episode detail page
    item_id: str | None = None
    audio_url: str | None = None
    start_time: int = 0
    sync_version: int = 0
    speed_id: int = DEFAULT_SPEED_ID
    saved_for_user: bool = True
    artwork_url: str | None = None
    podcast_title: str | None = None


@dataclass
class SyncState:
    """Per-episode sync tracking."""

    item_id: str
    sync_version: int
    speed_id: int
    episode_path: str
    player_entity_id: str | None = None
    last_position: int = 0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class OvercastAuthError(Exception):
    """Authentication failed or session expired."""


class OvercastConnectionError(Exception):
    """Cannot reach overcast.fm."""


class OvercastParseError(Exception):
    """HTML structure changed — scraping failed."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class OvercastClient:
    """Async client for Overcast web scraping."""

    def __init__(self, session: aiohttp.ClientSession, cookie: str | None = None) -> None:
        self._session = session
        self._cookie = cookie

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

    def _check_auth_redirect(self, resp: aiohttp.ClientResponse) -> None:
        """Raise if response is a redirect to the login page."""
        if resp.url.path == "/login" or str(resp.url).endswith("/login"):
            raise OvercastAuthError("Session expired — redirected to login")

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def login_email(self, email: str, password: str) -> str:
        """Log in with email/password. Returns the session cookie value."""
        try:
            resp = await self._session.post(
                LOGIN_URL,
                data={"email": email, "password": password},
                headers={"User-Agent": USER_AGENT},
                allow_redirects=False,
            )
        except aiohttp.ClientError as err:
            raise OvercastConnectionError(f"Cannot connect to Overcast: {err}") from err

        # Extract the 'o' cookie from Set-Cookie headers
        cookie_val = None
        for cookie in resp.cookies.values():
            if cookie.key == "o" and cookie.value and cookie.value != "-":
                cookie_val = cookie.value
                break

        if not cookie_val:
            # Check if we got redirected to /podcasts (success without Set-Cookie in some cases)
            if resp.status in (301, 302, 303) and "/podcasts" in (resp.headers.get("Location", "")):
                # Try to get cookie from cookie jar
                for c in self._session.cookie_jar:
                    if c.key == "o" and c.value and c.value != "-":
                        cookie_val = c.value
                        break

        if not cookie_val:
            raise OvercastAuthError("Invalid email or password")

        self._cookie = cookie_val
        return cookie_val

    async def start_qr_login(self) -> str:
        """Initiate QR login. Returns the QR token."""
        try:
            resp = await self._session.get(
                LOGIN_URL,
                headers={"User-Agent": USER_AGENT},
            )
        except aiohttp.ClientError as err:
            raise OvercastConnectionError(f"Cannot connect to Overcast: {err}") from err

        # Extract qr cookie from response
        qr_token = None
        for cookie in resp.cookies.values():
            if cookie.key == "qr":
                qr_token = cookie.value
                break

        if not qr_token or qr_token == "-":
            # Parse from page if not in cookies
            text = await resp.text()
            soup = BeautifulSoup(text, "html.parser")
            # Look for the QR token in page scripts or forms
            qr_input = soup.find("input", {"name": "token"})
            if qr_input and qr_input.get("value"):
                qr_token = qr_input["value"]

        if not qr_token or qr_token == "-":
            raise OvercastConnectionError("Could not obtain QR login token")

        return qr_token

    async def poll_qr_login(self, qr_token: str) -> str | None:
        """Poll for QR login approval. Returns cookie if approved, None if pending."""
        try:
            resp = await self._session.post(
                QR_VERIFY_URL,
                data={"token": qr_token, "then": "podcasts"},
                headers={"User-Agent": USER_AGENT},
                allow_redirects=False,
            )
        except aiohttp.ClientError:
            return None

        body = await resp.text()

        # Empty body means still waiting
        if not body or not body.strip():
            return None

        # Non-empty body = redirect URL = success
        cookie_val = None
        for cookie in resp.cookies.values():
            if cookie.key == "o" and cookie.value and cookie.value != "-":
                cookie_val = cookie.value
                break

        if not cookie_val:
            for c in self._session.cookie_jar:
                if c.key == "o" and c.value and c.value != "-":
                    cookie_val = c.value
                    break

        if cookie_val:
            self._cookie = cookie_val
            return cookie_val

        return None

    # ------------------------------------------------------------------
    # Subscription list
    # ------------------------------------------------------------------

    async def get_subscriptions(self) -> list[Podcast]:
        """Fetch the subscription list from /podcasts."""
        try:
            resp = await self._session.get(PODCASTS_URL, headers=self._headers())
        except aiohttp.ClientError as err:
            raise OvercastConnectionError(str(err)) from err

        self._check_auth_redirect(resp)
        text = await resp.text()

        if "/login" in str(resp.url) or 'name="password"' in text:
            raise OvercastAuthError("Session expired")

        soup = BeautifulSoup(text, "html.parser")
        cells = soup.select(SEL_FEEDCELL)

        podcasts: list[Podcast] = []
        for cell in cells:
            href = cell.get("href", "")
            if not href:
                continue

            title_div = cell.select_one(".title")
            title = title_div.get_text(strip=True) if title_div else cell.get_text(strip=True)

            img = cell.select_one("img.art")
            artwork = img.get("src") if img else None

            has_unplayed = cell.select_one(SEL_UNPLAYED_INDICATOR) is not None

            podcasts.append(Podcast(
                title=title,
                feed_path=href,
                artwork_url=artwork,
                has_unplayed=has_unplayed,
            ))

        _LOGGER.debug("Fetched %d subscriptions from Overcast", len(podcasts))
        return podcasts

    # ------------------------------------------------------------------
    # Episode list
    # ------------------------------------------------------------------

    async def get_episodes(self, feed_path: str) -> list[Episode]:
        """Fetch episodes for a podcast."""
        url = f"{OVERCAST_BASE_URL}{feed_path}"
        try:
            resp = await self._session.get(url, headers=self._headers())
        except aiohttp.ClientError as err:
            raise OvercastConnectionError(str(err)) from err

        self._check_auth_redirect(resp)
        text = await resp.text()
        soup = BeautifulSoup(text, "html.parser")
        cells = soup.select(SEL_EPISODE_CELL)

        episodes: list[Episode] = []
        for cell in cells:
            href = cell.get("href", "")
            if not href:
                continue

            is_played = "userdeletedepisode" in cell.get("class", [])

            title_div = cell.select_one(".title")
            title = title_div.get_text(strip=True) if title_div else ""

            caption_div = cell.select_one(".caption2")
            caption = caption_div.get_text(strip=True) if caption_div else ""

            desc_div = cell.select_one(".lighttext")
            description = desc_div.get_text(strip=True) if desc_div else ""

            episodes.append(Episode(
                title=title,
                episode_path=href,
                caption=caption,
                description=description,
                is_played=is_played,
            ))

        _LOGGER.debug("Fetched %d episodes for %s", len(episodes), feed_path)
        return episodes

    # ------------------------------------------------------------------
    # Episode detail
    # ------------------------------------------------------------------

    async def get_episode_detail(self, episode_path: str) -> Episode:
        """Fetch full episode detail including audio URL and sync metadata."""
        url = f"{OVERCAST_BASE_URL}{episode_path}"
        try:
            resp = await self._session.get(url, headers=self._headers())
        except aiohttp.ClientError as err:
            raise OvercastConnectionError(str(err)) from err

        self._check_auth_redirect(resp)
        text = await resp.text()
        soup = BeautifulSoup(text, "html.parser")

        audio = soup.select_one(SEL_AUDIO_PLAYER)
        if not audio:
            raise OvercastParseError(f"No audio player found on {episode_path}")

        source = soup.select_one(SEL_AUDIO_SOURCE)
        audio_url = source.get("src") if source else None

        # Extract og:title for episode + podcast name
        og_title = soup.find("meta", property="og:title")
        og_image = soup.find("meta", property="og:image")

        title_text = og_title.get("content", "") if og_title else ""
        parts = title_text.split(" \u2014 ", 1)  # em dash separator
        ep_title = parts[0].strip() if parts else title_text
        pod_title = parts[1].strip() if len(parts) > 1 else None

        episode = Episode(
            title=ep_title,
            episode_path=episode_path,
            podcast_title=pod_title,
            audio_url=audio_url,
            item_id=audio.get("data-item-id"),
            start_time=int(audio.get("data-start-time", "0")),
            sync_version=int(audio.get("data-sync-version", "0")),
            speed_id=int(audio.get("data-speed-id", str(DEFAULT_SPEED_ID))),
            saved_for_user=audio.get("data-saved-for-user") == "1",
            artwork_url=og_image.get("content") if og_image else None,
        )

        if not episode.saved_for_user:
            _LOGGER.warning(
                "Episode %s is not saved in Overcast library — progress sync may not persist",
                episode_path,
            )

        return episode

    # ------------------------------------------------------------------
    # Progress sync
    # ------------------------------------------------------------------

    async def set_progress(
        self,
        item_id: str,
        position: int,
        speed_id: int = DEFAULT_SPEED_ID,
        sync_version: int = 0,
    ) -> int:
        """POST progress to Overcast. Returns the new syncVersion."""
        url = f"{PODCASTS_URL}/set_progress/{item_id}"
        data = {
            "p": str(position),
            "speed": str(speed_id),
            "v": str(sync_version),
        }
        try:
            resp = await self._session.post(
                url,
                data=data,
                headers={
                    **self._headers(),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": OVERCAST_BASE_URL,
                },
            )
        except aiohttp.ClientError as err:
            raise OvercastConnectionError(str(err)) from err

        self._check_auth_redirect(resp)

        body = await resp.text()
        try:
            new_version = int(body.strip())
        except ValueError:
            _LOGGER.warning("Unexpected set_progress response: %s", body[:200])
            new_version = sync_version

        _LOGGER.debug(
            "set_progress(%s): p=%d, v=%d → %d",
            item_id, position, sync_version, new_version,
        )
        return new_version

    async def mark_episode_played(self, item_id: str, speed_id: int, sync_version: int) -> int:
        """Mark an episode as finished in Overcast."""
        return await self.set_progress(
            item_id, PROGRESS_FINISHED_SENTINEL, speed_id, sync_version,
        )

    # ------------------------------------------------------------------
    # OPML export
    # ------------------------------------------------------------------

    async def get_opml(self) -> str:
        """Fetch extended OPML export."""
        try:
            resp = await self._session.get(OPML_EXPORT_URL, headers=self._headers())
        except aiohttp.ClientError as err:
            raise OvercastConnectionError(str(err)) from err

        self._check_auth_redirect(resp)
        return await resp.text()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    async def validate_session(self) -> bool:
        """Check if the current session cookie is still valid."""
        if not self._cookie:
            return False
        try:
            resp = await self._session.get(
                PODCASTS_URL,
                headers=self._headers(),
                allow_redirects=False,
            )
        except aiohttp.ClientError:
            return False

        # Valid session: 200 on /podcasts
        # Expired session: 302 to /login
        if resp.status in (301, 302, 303):
            location = resp.headers.get("Location", "")
            if "/login" in location:
                return False

        return resp.status == 200
