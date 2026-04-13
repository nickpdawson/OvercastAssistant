"""The Overcast integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_COOKIE,
    DEFAULT_SPEED_ID,
    DOMAIN,
    EPISODE_REFRESH_SECONDS,
    PROGRESS_FINISHED_SENTINEL,
    SUBSCRIPTION_REFRESH_SECONDS,
    SYNC_INTERVAL_SECONDS,
)
from .const import SPEED_MAP

from .overcast_api import (
    Episode,
    OvercastAuthError,
    OvercastClient,
    OvercastConnectionError,
    OvercastParseError,
    Podcast,
    SyncState,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = []

# Reverse lookup: float rate → speed ID
_RATE_TO_SPEED_ID = {v: k for k, v in SPEED_MAP.items()}


def _speed_float_to_id(rate: float) -> int:
    """Convert a playback rate (e.g. 1.5) to an Overcast speed ID."""
    if rate in _RATE_TO_SPEED_ID:
        return _RATE_TO_SPEED_ID[rate]
    # Find closest match
    closest = min(SPEED_MAP.keys(), key=lambda k: abs(SPEED_MAP[k] - rate))
    return closest

type OvercastConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: OvercastConfigEntry) -> bool:
    """Set up Overcast from a config entry."""
    session = aiohttp_client.async_get_clientsession(hass)
    client = OvercastClient(session, cookie=entry.data[CONF_COOKIE])

    # Validate session
    if not await client.validate_session():
        raise ConfigEntryAuthFailed("Overcast session expired — please re-authenticate")

    coordinator = OvercastCoordinator(hass, client, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Register services (idempotent)
    _register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: OvercastConfigEntry) -> bool:
    """Unload an Overcast config entry."""
    coordinator: OvercastCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    coordinator.stop_sync()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN)

    return unload_ok


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------
class OvercastCoordinator(DataUpdateCoordinator):
    """Manages all Overcast data fetching and progress sync."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: OvercastClient,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SUBSCRIPTION_REFRESH_SECONDS),
        )
        self.client = client
        self.entry = entry

        # Cached state
        self.subscriptions: list[Podcast] = []
        self.episodes: dict[str, list[Episode]] = {}  # feed_path → episodes
        self._episode_cache_time: dict[str, float] = {}

        # Active sync contexts: player_entity_id → SyncState
        self._sync_states: dict[str, SyncState] = {}
        self._sync_unsub: list[Any] = []

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch subscription list from Overcast."""
        try:
            self.subscriptions = await self.client.get_subscriptions()
        except OvercastAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except OvercastConnectionError as err:
            raise UpdateFailed(str(err)) from err

        return {"subscriptions": self.subscriptions}

    async def async_get_episodes(self, feed_path: str, force: bool = False) -> list[Episode]:
        """Get episodes for a podcast, with caching."""
        import time

        now = time.time()
        cached_time = self._episode_cache_time.get(feed_path, 0)

        if not force and feed_path in self.episodes and (now - cached_time) < EPISODE_REFRESH_SECONDS:
            return self.episodes[feed_path]

        try:
            episodes = await self.client.get_episodes(feed_path)
        except OvercastAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except OvercastConnectionError as err:
            raise UpdateFailed(str(err)) from err

        self.episodes[feed_path] = episodes
        self._episode_cache_time[feed_path] = now
        return episodes

    async def async_get_episode_detail(self, episode_path: str) -> Episode:
        """Fetch full episode detail (audio URL, itemID, sync metadata)."""
        try:
            return await self.client.get_episode_detail(episode_path)
        except OvercastAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err

    # ------------------------------------------------------------------
    # Progress sync
    # ------------------------------------------------------------------

    def start_sync(self, episode: Episode, player_entity_id: str) -> None:
        """Start tracking playback progress for an episode on a player."""
        if not episode.item_id:
            _LOGGER.warning("Cannot sync — episode has no item_id: %s", episode.episode_path)
            return

        if not episode.saved_for_user:
            _LOGGER.warning(
                "Episode %s not in Overcast library — will attempt sync anyway",
                episode.episode_path,
            )

        state = SyncState(
            item_id=episode.item_id,
            sync_version=episode.sync_version,
            speed_id=episode.speed_id,
            episode_path=episode.episode_path,
            player_entity_id=player_entity_id,
            last_position=episode.start_time,
        )
        self._sync_states[player_entity_id] = state

        # Start the polling loop if not already running
        if not self._sync_unsub:
            unsub = async_track_time_interval(
                self.hass,
                self._async_sync_tick,
                timedelta(seconds=SYNC_INTERVAL_SECONDS),
            )
            self._sync_unsub.append(unsub)

        _LOGGER.info(
            "Started sync for %s on %s (itemID=%s, startTime=%d)",
            episode.episode_path, player_entity_id, episode.item_id, episode.start_time,
        )

    def stop_sync(self, player_entity_id: str | None = None) -> None:
        """Stop sync for a player, or all syncs if no player specified."""
        if player_entity_id:
            self._sync_states.pop(player_entity_id, None)
        else:
            self._sync_states.clear()

        if not self._sync_states:
            for unsub in self._sync_unsub:
                unsub()
            self._sync_unsub.clear()

    async def _async_sync_tick(self, _now: Any = None) -> None:
        """Poll player positions and sync to Overcast."""
        for entity_id, state in list(self._sync_states.items()):
            player_state = self.hass.states.get(entity_id)
            if not player_state:
                continue

            ha_state = player_state.state

            if ha_state == "playing":
                position = player_state.attributes.get("media_position")
                if position is None:
                    continue

                position = int(position)

                # Only sync if position changed meaningfully (>=10s)
                if abs(position - state.last_position) < 10:
                    continue

                try:
                    new_version = await self.client.set_progress(
                        state.item_id, position, state.speed_id, state.sync_version,
                    )
                    state.sync_version = new_version
                    state.last_position = position
                except OvercastAuthError:
                    _LOGGER.error("Auth failed during sync — stopping")
                    self.stop_sync(entity_id)
                except OvercastConnectionError as err:
                    _LOGGER.warning("Sync failed for %s: %s", entity_id, err)

            elif ha_state == "idle" and state.last_position > 0:
                # Player went idle after playing — mark finished
                try:
                    new_version = await self.client.mark_episode_played(
                        state.item_id, state.speed_id, state.sync_version,
                    )
                    _LOGGER.info(
                        "Marked episode %s as played (player went idle)",
                        state.episode_path,
                    )
                except (OvercastAuthError, OvercastConnectionError) as err:
                    _LOGGER.warning("Failed to mark played: %s", err)

                self.stop_sync(entity_id)

            elif ha_state == "paused":
                # Send current position on pause
                position = player_state.attributes.get("media_position")
                if position is not None:
                    position = int(position)
                    try:
                        new_version = await self.client.set_progress(
                            state.item_id, position, state.speed_id, state.sync_version,
                        )
                        state.sync_version = new_version
                        state.last_position = position
                    except (OvercastAuthError, OvercastConnectionError) as err:
                        _LOGGER.warning("Sync on pause failed: %s", err)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def find_podcast_by_name(self, name: str) -> Podcast | None:
        """Fuzzy-match a podcast by title (case-insensitive contains)."""
        name_lower = name.lower()
        for podcast in self.subscriptions:
            if name_lower in podcast.title.lower():
                return podcast
        return None

    async def async_play_episode_on_player(
        self,
        episode: Episode,
        player_entity_id: str,
        resume: bool = True,
        speed_override: float | None = None,
    ) -> None:
        """Play an episode on a media player and start sync."""
        if not episode.audio_url:
            _LOGGER.error("No audio URL for episode %s", episode.episode_path)
            return

        # Build play_media service data
        service_data: dict[str, Any] = {
            "entity_id": player_entity_id,
            "media_content_id": episode.audio_url,
            "media_content_type": "music",  # audio/mpeg works as "music" for Sonos
        }

        await self.hass.services.async_call(
            "media_player", "play_media", service_data, blocking=True,
        )

        # Seek to resume position if needed
        if resume and episode.start_time > 0:
            await self.hass.services.async_call(
                "media_player",
                "media_seek",
                {"entity_id": player_entity_id, "seek_position": episode.start_time},
                blocking=True,
            )

        # Apply speed override for sync
        if speed_override is not None:
            episode.speed_id = _speed_float_to_id(speed_override)

        # Start progress sync
        self.start_sync(episode, player_entity_id)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
def _register_services(hass: HomeAssistant) -> None:
    """Register Overcast services."""

    async def handle_play_latest(call: ServiceCall) -> None:
        """Handle overcast.play_latest."""
        podcast_names: list[str] = call.data["podcasts"]
        player_entity_id: str = call.data["target_player"]
        mark_played: bool = call.data.get("mark_played", True)
        resume: bool = call.data.get("resume_position", True)
        speed_raw = call.data.get("speed")
        speed: float | None = float(speed_raw) if speed_raw is not None else None

        # Get the first coordinator
        coordinators = list(hass.data.get(DOMAIN, {}).values())
        if not coordinators:
            _LOGGER.error("No Overcast integration configured")
            return
        coordinator: OvercastCoordinator = coordinators[0]

        episodes_to_play: list[Episode] = []

        for name in podcast_names:
            podcast = coordinator.find_podcast_by_name(name)
            if not podcast:
                _LOGGER.warning("Podcast not found: %s", name)
                continue

            episodes = await coordinator.async_get_episodes(podcast.feed_path)
            unplayed = [e for e in episodes if not e.is_played]
            if not unplayed:
                _LOGGER.info("No unplayed episodes for %s", name)
                continue

            # Get detail for the first unplayed (most recent)
            detail = await coordinator.async_get_episode_detail(unplayed[0].episode_path)
            episodes_to_play.append(detail)

        if not episodes_to_play:
            _LOGGER.warning("No episodes to play")
            return

        # Play first episode immediately
        first = episodes_to_play[0]
        await coordinator.async_play_episode_on_player(
            first, player_entity_id, resume=resume, speed_override=speed,
        )

        # Queue remaining episodes (basic sequential approach — play next on idle)
        if len(episodes_to_play) > 1:
            coordinator._queued_episodes = episodes_to_play[1:]
            coordinator._queue_player = player_entity_id
            coordinator._queue_resume = resume

    async def handle_mark_played(call: ServiceCall) -> None:
        """Handle overcast.mark_played."""
        episode_url: str = call.data["episode_url"]

        coordinators = list(hass.data.get(DOMAIN, {}).values())
        if not coordinators:
            return
        coordinator: OvercastCoordinator = coordinators[0]

        # Extract episode path from URL
        if episode_url.startswith("https://overcast.fm"):
            episode_path = episode_url.replace("https://overcast.fm", "")
        else:
            episode_path = episode_url

        detail = await coordinator.async_get_episode_detail(episode_path)
        if detail.item_id:
            await coordinator.client.mark_episode_played(
                detail.item_id, detail.speed_id, detail.sync_version,
            )
            _LOGGER.info("Marked %s as played", episode_path)
        else:
            _LOGGER.error("Could not get item_id for %s", episode_path)

    async def handle_refresh(call: ServiceCall) -> None:
        """Handle overcast.refresh."""
        for coordinator in hass.data.get(DOMAIN, {}).values():
            await coordinator.async_refresh()

    if not hass.services.has_service(DOMAIN, "play_latest"):
        hass.services.async_register(DOMAIN, "play_latest", handle_play_latest)
    if not hass.services.has_service(DOMAIN, "mark_played"):
        hass.services.async_register(DOMAIN, "mark_played", handle_mark_played)
    if not hass.services.has_service(DOMAIN, "refresh"):
        hass.services.async_register(DOMAIN, "refresh", handle_refresh)
