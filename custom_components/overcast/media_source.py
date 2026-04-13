"""Overcast media source for Home Assistant Media Browser."""

from __future__ import annotations

import logging
from urllib.parse import quote, unquote

from homeassistant.components.media_player import BrowseError, MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _encode_id(path: str) -> str:
    """Encode a path for use as a media source identifier.

    Slashes in Overcast paths (e.g. /itunes981330533/slug) conflict with
    HA's URI parsing, so we percent-encode them.
    """
    return quote(path, safe="")


def _decode_id(identifier: str) -> str:
    """Decode a media source identifier back to an Overcast path."""
    return unquote(identifier)


async def async_get_media_source(hass: HomeAssistant) -> OvercastMediaSource:
    """Set up Overcast media source."""
    return OvercastMediaSource(hass)


class OvercastMediaSource(MediaSource):
    """Provide Overcast podcasts as a media source."""

    name = "Overcast"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize."""
        super().__init__(DOMAIN)
        self.hass = hass

    def _get_coordinator(self):
        """Get the first available coordinator."""
        entries = self.hass.data.get(DOMAIN, {})
        if not entries:
            return None
        return next(iter(entries.values()))

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve an episode to a playable URL."""
        coordinator = self._get_coordinator()
        if not coordinator:
            raise Unresolvable("Overcast integration not configured")

        identifier = _decode_id(item.identifier) if item.identifier else ""
        if not identifier or not identifier.startswith("/+"):
            raise Unresolvable(f"Invalid episode identifier: {identifier}")

        episode = await coordinator.async_get_episode_detail(identifier)
        if not episode.audio_url:
            raise Unresolvable(f"No audio URL for episode: {identifier}")

        return PlayMedia(
            url=episode.audio_url,
            mime_type="audio/mpeg",
        )

    async def async_browse_media(
        self,
        item: MediaSourceItem,
    ) -> BrowseMediaSource:
        """Browse Overcast media hierarchy."""
        coordinator = self._get_coordinator()
        if not coordinator:
            raise BrowseError("Overcast integration not configured")

        identifier = _decode_id(item.identifier) if item.identifier else ""

        if not identifier:
            return await self._build_root(coordinator)

        if identifier.startswith("/p") or identifier.startswith("/itunes"):
            return await self._build_podcast(coordinator, identifier)

        raise BrowseError(f"Unknown identifier: {identifier}")

    async def _build_root(self, coordinator) -> BrowseMediaSource:
        """Build the root listing of all subscriptions."""
        if not coordinator.subscriptions:
            await coordinator.async_refresh()

        children = []
        for podcast in coordinator.subscriptions:
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=_encode_id(podcast.feed_path),
                    media_class=MediaClass.PODCAST,
                    media_content_type=MediaType.PODCAST,
                    title=podcast.title,
                    can_play=False,
                    can_expand=True,
                    thumbnail=podcast.artwork_url,
                )
            )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier="",
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.PODCAST,
            title="Overcast",
            can_play=False,
            can_expand=True,
            children=children,
        )

    async def _build_podcast(self, coordinator, feed_path: str) -> BrowseMediaSource:
        """Build the episode listing for a podcast."""
        episodes = await coordinator.async_get_episodes(feed_path)

        podcast_title = "Podcast"
        for p in coordinator.subscriptions:
            if p.feed_path == feed_path:
                podcast_title = p.title
                break

        children = []
        for episode in episodes:
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=_encode_id(episode.episode_path),
                    media_class=MediaClass.PODCAST,
                    media_content_type=MediaType.PODCAST,
                    title=episode.title,
                    can_play=True,
                    can_expand=False,
                )
            )

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=_encode_id(feed_path),
            media_class=MediaClass.PODCAST,
            media_content_type=MediaType.PODCAST,
            title=podcast_title,
            can_play=False,
            can_expand=True,
            children=children,
        )
