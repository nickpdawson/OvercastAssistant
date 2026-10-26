"""Data coordinator and progress sync for Overcast."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import time
from typing import TypeVar

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_DURATION,
    ATTR_MEDIA_POSITION,
    ATTR_MEDIA_POSITION_UPDATED_AT,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_IDLE,
    STATE_OFF,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_STANDBY,
    STATE_UNAVAILABLE,
)
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    EPISODE_REFRESH_SECONDS,
    MARK_PLAYED_WINDOW_SECONDS,
    PLAY_START_GRACE_SECONDS,
    PLAY_START_TIMEOUT_SECONDS,
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

# Player states that end a listening session.
_STOPPED_STATES = {STATE_IDLE, STATE_OFF, STATE_STANDBY, STATE_UNAVAILABLE}


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
    audio_url: str | None
    mark_played: bool
    started: float  # time.monotonic() when sync started
    last_position: float = 0.0  # last position seen on the player
    last_synced: int | None = None  # last position sent to Overcast
    duration: float | None = None
    seen_playing: bool = False
    content_id: str | None = None
    pending_seek: int | None = None
    hold_until: float = 0.0  # no periodic POSTs before this (after a seek)
    unsub: CALLBACK_TYPE | None = None


@dataclass
class PlayQueue:
    """Episodes still to play on one player after the current one ends."""

    episode_paths: list[str]
    resume: bool
    mark_played: bool
    speed: float | None


def _position_at(state: State, when: datetime) -> float | None:
    """Estimate a player's position at a moment, from a state snapshot."""
    position = state.attributes.get(ATTR_MEDIA_POSITION)
    if position is None:
        return None
    try:
        position = float(position)
    except (TypeError, ValueError):
        return None
    updated = state.attributes.get(ATTR_MEDIA_POSITION_UPDATED_AT)
    if state.state == STATE_PLAYING and isinstance(updated, datetime):
        position += max(0.0, (when - updated).total_seconds())
    duration = state.attributes.get(ATTR_MEDIA_DURATION)
    if isinstance(duration, (int, float)) and duration > 0:
        position = min(position, float(duration))
    return position


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
        self._queues: dict[str, PlayQueue] = {}
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
    # Playback and queue
    # ------------------------------------------------------------------

    async def async_play_queue(
        self,
        player_entity_id: str,
        episode_paths: list[str],
        *,
        resume: bool,
        mark_played: bool,
        speed: float | None,
    ) -> None:
        """Play the first episode now and the rest as each one finishes."""
        self.stop_sync(player_entity_id)
        self._queues.pop(player_entity_id, None)
        queue = PlayQueue(list(episode_paths), resume, mark_played, speed)
        first = queue.episode_paths.pop(0)
        if queue.episode_paths:
            self._queues[player_entity_id] = queue
        detail = await self.async_get_episode_detail(first)
        await self.async_play_episode_on_player(
            detail,
            player_entity_id,
            resume=resume,
            speed_override=speed,
            mark_played=mark_played,
        )

    async def _async_play_next(self, player_entity_id: str) -> None:
        """Start the next queued episode on a player, if any."""
        while (queue := self._queues.get(player_entity_id)) and queue.episode_paths:
            path = queue.episode_paths.pop(0)
            if not queue.episode_paths:
                self._queues.pop(player_entity_id, None)
            try:
                detail = await self.async_get_episode_detail(path)
                await self.async_play_episode_on_player(
                    detail,
                    player_entity_id,
                    resume=queue.resume,
                    speed_override=queue.speed,
                    mark_played=queue.mark_played,
                )
            except HomeAssistantError as err:
                _LOGGER.warning("Could not play queued episode %s: %s", path, err)
                continue
            return
        self._queues.pop(player_entity_id, None)

    def queued_episodes(self, player_entity_id: str) -> list[str]:
        """Return the episode paths still queued for a player."""
        queue = self._queues.get(player_entity_id)
        return list(queue.episode_paths) if queue else []

    async def async_play_episode_on_player(
        self,
        episode: Episode,
        player_entity_id: str,
        resume: bool = True,
        speed_override: float | None = None,
        mark_played: bool = True,
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

        self.start_sync(
            episode,
            player_entity_id,
            mark_played=mark_played,
            start_position=episode.start_time if resume else 0,
        )

    @callback
    def async_track_browser_play(
        self, episode: Episode, player_entity_id: str, *, mark_played: bool
    ) -> None:
        """Track an episode the media browser is about to play.

        HA starts playback itself after resolving the media, at position 0,
        so seek to the saved Overcast position once the player is playing.
        """
        self._queues.pop(player_entity_id, None)
        self.start_sync(
            episode,
            player_entity_id,
            mark_played=mark_played,
            start_position=episode.start_time,
            seek_on_start=True,
        )

    # ------------------------------------------------------------------
    # Progress sync
    # ------------------------------------------------------------------

    def start_sync(
        self,
        episode: Episode,
        player_entity_id: str,
        *,
        mark_played: bool,
        start_position: int = 0,
        seek_on_start: bool = False,
    ) -> None:
        """Track playback of an episode on a player and report it to Overcast.

        seek_on_start asks the player to seek to start_position once it is
        seen playing; used when HA started playback itself (media browser).
        """
        if not episode.item_id:
            _LOGGER.warning("Cannot sync, episode has no item id: %s", episode.episode_path)
            return

        self._drop_sync(player_entity_id)
        state = SyncState(
            item_id=episode.item_id,
            sync_version=episode.sync_version,
            speed_id=episode.speed_id,
            episode_path=episode.episode_path,
            player_entity_id=player_entity_id,
            audio_url=episode.audio_url,
            mark_played=mark_played,
            started=time.monotonic(),
            last_position=float(start_position),
            last_synced=start_position,
            pending_seek=start_position if seek_on_start and start_position > 0 else None,
        )

        @callback
        def _state_changed(event: Event[EventStateChangedData]) -> None:
            if self._sync_states.get(player_entity_id) is not state:
                return
            self.config_entry.async_create_task(
                self.hass,
                self._async_handle_state_change(
                    state, event.data["old_state"], event.data["new_state"]
                ),
                f"overcast sync {player_entity_id}",
            )

        state.unsub = async_track_state_change_event(
            self.hass, [player_entity_id], _state_changed
        )
        self._sync_states[player_entity_id] = state

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
            start_position,
        )

    def _drop_sync(self, player_entity_id: str) -> None:
        if (state := self._sync_states.pop(player_entity_id, None)) and state.unsub:
            state.unsub()
        if not self._sync_states and self._sync_unsub is not None:
            self._sync_unsub()
            self._sync_unsub = None

    def stop_sync(self, player_entity_id: str | None = None) -> None:
        """Stop sync for one player, or for all players and clear all queues."""
        if player_entity_id:
            self._drop_sync(player_entity_id)
            return
        for entity_id in list(self._sync_states):
            self._drop_sync(entity_id)
        self._queues.clear()

    def is_syncing(self, player_entity_id: str) -> bool:
        """Return True while progress for a player is being tracked."""
        return player_entity_id in self._sync_states

    def _observe(self, state: SyncState, player: State, when: datetime) -> bool:
        """Record what the player reports. Returns False if it moved on to other media."""
        if player.state not in (STATE_PLAYING, STATE_PAUSED):
            return True
        content_id = player.attributes.get(ATTR_MEDIA_CONTENT_ID)
        in_grace = time.monotonic() - state.started < PLAY_START_GRACE_SECONDS
        if not state.seen_playing:
            # Right after play_media the player can still report the old media.
            if in_grace and not (content_id and content_id == state.audio_url):
                return True
            state.seen_playing = True
            state.content_id = content_id
        elif content_id and state.content_id and content_id != state.content_id:
            return False

        duration = player.attributes.get(ATTR_MEDIA_DURATION)
        if isinstance(duration, (int, float)) and duration > 0:
            state.duration = float(duration)
        position = _position_at(player, when)
        if position is not None:
            state.last_position = position
        return True

    async def _async_post_progress(self, state: SyncState, position: int) -> None:
        """Send a position to Overcast unless it is the one already sent."""
        if state.last_synced is not None and position == state.last_synced:
            return
        try:
            state.sync_version = await self.client.set_progress(
                state.item_id, position, state.speed_id, state.sync_version
            )
            state.last_synced = position
        except OvercastAuthError:
            _LOGGER.warning("Overcast session expired during sync; starting reauth")
            self.config_entry.async_start_reauth(self.hass)
            self.stop_sync()
        except OvercastError as err:
            _LOGGER.warning("Progress sync failed for %s: %s", state.player_entity_id, err)

    def is_near_end(self, state: SyncState) -> bool:
        """True when the last known position is within the mark-played window of the end."""
        return (
            state.duration is not None
            and state.duration - state.last_position <= MARK_PLAYED_WINDOW_SECONDS
        )

    async def _async_handle_state_change(
        self, state: SyncState, old: State | None, new: State | None
    ) -> None:
        if self._sync_states.get(state.player_entity_id) is not state:
            return
        when = new.last_changed if new is not None else dt_util.utcnow()
        if old is not None and old.state == STATE_PLAYING:
            # The last playing snapshot, extrapolated to the moment it
            # stopped, is the best position estimate; players often reset
            # media_position when they go idle.
            if not self._observe(state, old, when):
                await self._async_media_replaced(state)
                return

        new_state = new.state if new is not None else STATE_UNAVAILABLE
        if new_state in _STOPPED_STATES:
            await self._async_player_stopped(state, new_state)
            return
        if new is None:
            return
        if not self._observe(state, new, when):
            await self._async_media_replaced(state)
            return
        if new_state == STATE_PLAYING and state.seen_playing and state.pending_seek:
            seek, state.pending_seek = state.pending_seek, None
            # Until the player reports the new position, ticks would
            # send the pre-seek position and overwrite Overcast's.
            state.hold_until = time.monotonic() + PLAY_START_GRACE_SECONDS
            await self.hass.services.async_call(
                "media_player",
                "media_seek",
                {"entity_id": state.player_entity_id, "seek_position": seek},
                blocking=False,
            )
        elif new_state == STATE_PAUSED and state.seen_playing and state.pending_seek is None:
            await self._async_post_progress(state, int(state.last_position))

    async def _async_media_replaced(self, state: SyncState) -> None:
        """The player switched to other media: keep progress, drop sync and queue."""
        _LOGGER.debug("%s switched away from %s", state.player_entity_id, state.episode_path)
        self._drop_sync(state.player_entity_id)
        self._queues.pop(state.player_entity_id, None)
        await self._async_post_progress(state, int(state.last_position))

    async def _async_player_stopped(self, state: SyncState, player_state: str) -> None:
        """Handle the end of a listening session.

        Only an idle player whose last position was within a minute of the
        end counts as finished. A manual stop, or a player going off or
        unavailable, saves the position and never marks the episode played.
        """
        if not state.seen_playing and player_state == STATE_IDLE:
            # Still starting up; the tick gives up if it never plays.
            return

        self._drop_sync(state.player_entity_id)
        finished = player_state == STATE_IDLE and state.seen_playing and self.is_near_end(state)

        if finished and state.mark_played:
            try:
                state.sync_version = await self.client.mark_episode_played(
                    state.item_id, state.speed_id, state.sync_version
                )
                _LOGGER.debug("Marked %s played", state.episode_path)
            except OvercastAuthError:
                self.config_entry.async_start_reauth(self.hass)
            except OvercastError as err:
                _LOGGER.warning("Failed to mark %s played: %s", state.episode_path, err)
        elif state.seen_playing:
            await self._async_post_progress(state, int(state.last_position))

        if finished:
            await self._async_play_next(state.player_entity_id)
        else:
            self._queues.pop(state.player_entity_id, None)

    async def _async_sync_tick(self, now: datetime | None = None) -> None:
        """Poll player positions and send changes to Overcast."""
        when = now or dt_util.utcnow()
        for entity_id, state in list(self._sync_states.items()):
            player = self.hass.states.get(entity_id)
            if player is None:
                continue
            if player.state in _STOPPED_STATES:
                await self._async_player_stopped(state, player.state)
                continue
            if not self._observe(state, player, when):
                await self._async_media_replaced(state)
                continue
            if not state.seen_playing:
                if time.monotonic() - state.started > PLAY_START_TIMEOUT_SECONDS:
                    _LOGGER.debug("%s never started playing; stopping sync", entity_id)
                    self._drop_sync(entity_id)
                    self._queues.pop(entity_id, None)
                continue
            if state.pending_seek is not None or time.monotonic() < state.hold_until:
                continue
            position = int(state.last_position)
            if player.state == STATE_PLAYING:
                if state.last_synced is None or abs(position - state.last_synced) >= 10:
                    await self._async_post_progress(state, position)
            elif player.state == STATE_PAUSED:
                await self._async_post_progress(state, position)
