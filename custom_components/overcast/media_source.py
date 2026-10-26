"""Overcast media source for the Home Assistant media browser."""

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
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_MARK_PLAYED, DEFAULT_MARK_PLAYED, DOMAIN
from .coordinator import OvercastCoordinator

_LOGGER = logging.getLogger(__name__)

# Sonos only lists browse items whose content type starts with "audio/"
# (it hides MediaType.PODCAST as incompatible). The real type of each
# file is only known after its episode page is fetched, so the listing
# says audio/mpeg and resolve returns the type from the page.
EPISODE_CONTENT_TYPE = "audio/mpeg"
UNPLAYED_MARK = "• "  # bullet before unplayed episode titles


def _encode_id(path: str) -> str:
    """Encode an Overcast path for use as a media source identifier.

    Slashes in Overcast paths (e.g. /itunes981330533/slug) conflict with
    HA's URI parsing, so they are percent-encoded.
    """
    return quote(path, safe="")


def _decode_id(identifier: str) -> str:
    """Decode a media source identifier back to an Overcast path."""
    return unquote(identifier)


async def async_get_media_source(hass: HomeAssistant) -> OvercastMediaSource:
    """Set up the Overcast media source."""
    return OvercastMediaSource(hass)


class OvercastMediaSource(MediaSource):
    """Provide Overcast podcasts as a media source."""

    name = "Overcast"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize."""
        super().__init__(DOMAIN)
        self.hass = hass

    def _get_coordinator(self) -> OvercastCoordinator | None:
        """Return the coordinator of the first loaded Overcast account."""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                return entry.runtime_data
        return None

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve an episode to a playable URL and start progress sync."""
        coordinator = self._get_coordinator()
        if not coordinator:
            raise Unresolvable("Overcast integration not configured")

        identifier = _decode_id(item.identifier) if item.identifier else ""
        if not identifier.startswith("/+"):
            raise Unresolvable(f"Invalid episode identifier: {identifier}")

        try:
            episode = await coordinator.async_get_episode_detail(identifier)
        except HomeAssistantError as err:
            raise Unresolvable(str(err)) from err
        if not episode.audio_url:
            raise Unresolvable(f"No audio URL for episode: {identifier}")

        if item.target_media_player:
            coordinator.async_track_browser_play(
                episode,
                item.target_media_player,
                mark_played=coordinator.config_entry.options.get(
                    CONF_MARK_PLAYED, DEFAULT_MARK_PLAYED
                ),
            )

        return PlayMedia(
            url=episode.audio_url,
            mime_type=episode.mime_type or EPISODE_CONTENT_TYPE,
        )

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse subscriptions and their episodes."""
        coordinator = self._get_coordinator()
        if not coordinator:
            raise BrowseError("Overcast integration not configured")

        identifier = _decode_id(item.identifier) if item.identifier else ""
        try:
            if not identifier:
                return await self._build_root(coordinator)
            if identifier.startswith("/p") or identifier.startswith("/itunes"):
                return await self._build_podcast(coordinator, identifier)
        except HomeAssistantError as err:
            raise BrowseError(str(err)) from err

        raise BrowseError(f"Unknown identifier: {identifier}")

    async def _build_root(self, coordinator: OvercastCoordinator) -> BrowseMediaSource:
        """List all subscriptions."""
        if not coordinator.subscriptions:
            await coordinator.async_refresh()
            if not coordinator.last_update_success:
                raise BrowseError("Could not load Overcast subscriptions")

        children = [
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
            for podcast in coordinator.subscriptions
        ]

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier="",
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.PODCAST,
            title="Overcast",
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.PODCAST,
        )

    async def _build_podcast(
        self, coordinator: OvercastCoordinator, feed_path: str
    ) -> BrowseMediaSource:
        """List the episodes of one podcast."""
        episodes = await coordinator.async_get_episodes(feed_path)
        podcast = next(
            (p for p in coordinator.subscriptions if p.feed_path == feed_path), None
        )
        artwork = podcast.artwork_url if podcast else None

        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=_encode_id(episode.episode_path),
                media_class=MediaClass.EPISODE,
                media_content_type=EPISODE_CONTENT_TYPE,
                title=episode.title if episode.is_played else UNPLAYED_MARK + episode.title,
                can_play=True,
                can_expand=False,
                thumbnail=artwork,
            )
            for episode in episodes
        ]

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=_encode_id(feed_path),
            media_class=MediaClass.PODCAST,
            media_content_type=MediaType.PODCAST,
            title=podcast.title if podcast else "Podcast",
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.EPISODE,
            thumbnail=artwork,
        )
