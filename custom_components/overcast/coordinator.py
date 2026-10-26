"""Data coordinator and progress sync for Overcast."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import timedelta
import logging
import time
from typing import Any, TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    EPISODE_REFRESH_SECONDS,
    SPEED_MAP,
    SUBSCRIPTION_REFRESH_SECONDS,
    SYNC_INTERVAL_SECONDS,
)
from .overcast_api import (
    Episode,
    OvercastAuthError,
    OvercastClient,
    OvercastError,
    OvercastInvalidURLError,
    Podcast,
)

type OvercastConfigEntry = ConfigEntry[OvercastCoordinator]

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

_RATE_TO_SPEED_ID = {v: k for k, v in SPEED_MAP.items()}


def speed_float_to_id(rate: float) -> int:
    """Convert a playback rate (e.g. 1.5) to the nearest Overcast speed ID."""
    if rate in _RATE_TO_SPEED_ID:
        return _RATE_TO_SPEED_ID[rate]
    return min(SPEED_MAP, key=lambda k: abs(SPEED_MAP[k] - rate))


@dataclass
class SyncState:
    """Progress tracking for one episode on one player."""

    item_id: str
    sync_version: int
    speed_id: int
    episode_path: str
    player_entity_id: str
    last_position: int = 0


class OvercastCoordinator(DataUpdateCoordinator[list[Podcast]]):
    """Fetches Overcast data and syncs playback progress."""

    config_entry: OvercastConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: OvercastConfigEntry,
        client: OvercastClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=SUBSCRIPTION_REFRESH_SECONDS),
        )
        self.client = client
        self.episodes: dict[str, list[Episode]] = {}  # feed_path -> episodes
        self._episode_cache_time: dict[str, float] = {}
        self._sync_states: dict[str, SyncState] = {}
        self._sync_unsub: CALLBACK_TYPE | None = None

    @property
    def subscriptions(self) -> list[Podcast]:
        """Return the subscription list from the last refresh."""
        return self.data or []

    async def _async_update_data(self) -> list[Podcast]:
        """Fetch the subscription list from Overcast."""
        try:
            return await self.client.get_subscriptions()
        except OvercastAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except OvercastError as err:
            raise UpdateFailed(str(err)) from err

    async def _guard(self, call: Awaitable[_T]) -> _T:
        """Turn client errors into Home Assistant errors outside of updates.

        ConfigEntryAuthFailed only works inside a coordinator update or
        entry setup, so here an expired session starts reauth directly.
        """
        try:
            return await call
        except OvercastAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                "Overcast session expired. Re-authenticate the Overcast integration."
            ) from err
        except OvercastInvalidURLError as err:
            raise HomeAssistantError(str(err)) from err
        except OvercastError as err:
            raise HomeAssistantError(f"Overcast request failed: {err}") from err

    async def async_get_episodes(self, feed_path: str, force: bool = False) -> list[Episode]:
        """Get episodes for a podcast, cached for a few minutes."""
        now = time.monotonic()
        cached_time = self._episode_cache_time.get(feed_path)
        if (
            not force
            and cached_time is not None
            and feed_path in self.episodes
            and now - cached_time < EPISODE_REFRESH_SECONDS
        ):
            return self.episodes[feed_path]

        episodes = await self._guard(self.client.get_episodes(feed_path))
        self.episodes[feed_path] = episodes
        self._episode_cache_time[feed_path] = now
        return episodes

    async def async_get_episode_detail(self, episode_path: str) -> Episode:
        """Fetch full episode detail (audio URL, item id, sync metadata)."""
        return await self._guard(self.client.get_episode_detail(episode_path))

    async def async_mark_played(self, episode: Episode) -> None:
        """Mark an episode played in Overcast."""
        if not episode.item_id:
            raise HomeAssistantError(f"No Overcast item id for {episode.episode_path}")
        await self._guard(
            self.client.mark_episode_played(
                episode.item_id, episode.speed_id, episode.sync_version
            )
        )

    def find_podcast_by_name(self, name: str) -> Podcast | None:
        """Match a podcast by title: exact (case-insensitive) first, then substring."""
        name_lower = name.strip().lower()
        for podcast in self.subscriptions:
            if podcast.title.lower() == name_lower:
                return podcast
        for podcast in self.subscriptions:
            if name_lower in podcast.title.lower():
                return podcast
        return None

    # ------------------------------------------------------------------
    # Progress sync
    # ------------------------------------------------------------------

    def start_sync(self, episode: Episode, player_entity_id: str) -> None:
        """Start tracking playback progress for an episode on a player."""
        if not episode.item_id:
            _LOGGER.warning("Cannot sync, episode has no item id: %s", episode.episode_path)
            return

        self._sync_states[player_entity_id] = SyncState(
            item_id=episode.item_id,
            sync_version=episode.sync_version,
            speed_id=episode.speed_id,
            episode_path=episode.episode_path,
            player_entity_id=player_entity_id,
            last_position=episode.start_time,
        )
        if self._sync_unsub is None:
            self._sync_unsub = async_track_time_interval(
                self.hass,
                self._async_sync_tick,
                timedelta(seconds=SYNC_INTERVAL_SECONDS),
                name="overcast progress sync",
            )
        _LOGGER.debug(
            "Started sync for %s on %s (item %s, start %d)",
            episode.episode_path,
            player_entity_id,
            episode.item_id,
            episode.start_time,
        )

    def stop_sync(self, player_entity_id: str | None = None) -> None:
        """Stop sync for one player, or for all players."""
        if player_entity_id:
            self._sync_states.pop(player_entity_id, None)
        else:
            self._sync_states.clear()
        if not self._sync_states and self._sync_unsub is not None:
            self._sync_unsub()
            self._sync_unsub = None

    async def _async_post_progress(self, state: SyncState, position: int) -> None:
        try:
            state.sync_version = await self.client.set_progress(
                state.item_id, position, state.speed_id, state.sync_version
            )
            state.last_position = position
        except OvercastAuthError:
            _LOGGER.warning("Overcast session expired during sync; starting reauth")
            self.config_entry.async_start_reauth(self.hass)
            self.stop_sync()
        except OvercastError as err:
            _LOGGER.warning("Progress sync failed for %s: %s", state.player_entity_id, err)

    async def _async_sync_tick(self, _now: Any = None) -> None:
        """Poll player positions and sync to Overcast."""
        for entity_id, state in list(self._sync_states.items()):
            player_state = self.hass.states.get(entity_id)
            if not player_state:
                continue

            if player_state.state == "playing":
                position = player_state.attributes.get("media_position")
                if position is None or abs(int(position) - state.last_position) < 10:
                    continue
                await self._async_post_progress(state, int(position))

            elif player_state.state == "idle" and state.last_position > 0:
                try:
                    await self.client.mark_episode_played(
                        state.item_id, state.speed_id, state.sync_version
                    )
                except OvercastError as err:
                    _LOGGER.warning("Failed to mark played: %s", err)
                self.stop_sync(entity_id)

            elif player_state.state == "paused":
                position = player_state.attributes.get("media_position")
                if position is not None:
                    await self._async_post_progress(state, int(position))

    async def async_play_episode_on_player(
        self,
        episode: Episode,
        player_entity_id: str,
        resume: bool = True,
        speed_override: float | None = None,
    ) -> None:
        """Play an episode on a media player and start progress sync."""
        if not episode.audio_url:
            raise HomeAssistantError(f"No audio URL for episode {episode.episode_path}")

        await self.hass.services.async_call(
            "media_player",
            "play_media",
            {
                "entity_id": player_entity_id,
                "media_content_id": episode.audio_url,
                # "music" is what Sonos accepts for a plain audio URL
                "media_content_type": "music",
            },
            blocking=True,
        )

        if resume and episode.start_time > 0:
            await self.hass.services.async_call(
                "media_player",
                "media_seek",
                {"entity_id": player_entity_id, "seek_position": episode.start_time},
                blocking=True,
            )

        if speed_override is not None:
            episode.speed_id = speed_float_to_id(speed_override)

        self.start_sync(episode, player_entity_id)
