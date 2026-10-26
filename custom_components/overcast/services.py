"""Overcast service actions."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_EPISODE_URL,
    ATTR_MARK_PLAYED,
    ATTR_PODCASTS,
    ATTR_RESUME_POSITION,
    ATTR_SPEED,
    ATTR_TARGET_PLAYER,
    DOMAIN,
    SERVICE_MARK_PLAYED,
    SERVICE_PLAY_LATEST,
    SERVICE_REFRESH,
)
from .coordinator import OvercastConfigEntry, OvercastCoordinator
from .overcast_api import Episode, OvercastInvalidURLError, episode_path_from_url

_LOGGER = logging.getLogger(__name__)

PLAY_LATEST_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_PODCASTS): vol.All(cv.ensure_list, [cv.string], vol.Length(min=1)),
        vol.Required(ATTR_TARGET_PLAYER): cv.entity_domain("media_player"),
        vol.Optional(ATTR_MARK_PLAYED, default=True): cv.boolean,
        vol.Optional(ATTR_RESUME_POSITION, default=True): cv.boolean,
        vol.Optional(ATTR_SPEED): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=3.0)),
    }
)

MARK_PLAYED_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_EPISODE_URL): cv.string,
    }
)

REFRESH_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})


def _loaded_entries(hass: HomeAssistant) -> list[OvercastConfigEntry]:
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]


def _get_coordinator(hass: HomeAssistant, call: ServiceCall) -> OvercastCoordinator:
    """Pick the Overcast account a service call is for."""
    if entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID):
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(f"No Overcast config entry with id {entry_id}")
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(f"Overcast entry {entry.title} is not loaded")
        return entry.runtime_data

    entries = _loaded_entries(hass)
    if not entries:
        raise ServiceValidationError("No Overcast account is set up and loaded")
    if len(entries) > 1:
        raise ServiceValidationError(
            "More than one Overcast account is set up. Pass config_entry_id."
        )
    return entries[0].runtime_data


async def _async_play_latest(call: ServiceCall) -> None:
    hass = call.hass
    coordinator = _get_coordinator(hass, call)
    player: str = call.data[ATTR_TARGET_PLAYER]
    speed: float | None = call.data.get(ATTR_SPEED)

    if hass.states.get(player) is None:
        raise ServiceValidationError(f"Media player {player} does not exist")

    episodes_to_play: list[Episode] = []
    missing: list[str] = []
    for name in call.data[ATTR_PODCASTS]:
        podcast = coordinator.find_podcast_by_name(name)
        if podcast is None:
            missing.append(name)
            continue
        episodes = await coordinator.async_get_episodes(podcast.feed_path)
        unplayed = [e for e in episodes if not e.is_played]
        if not unplayed:
            _LOGGER.info("No unplayed episodes for %s", podcast.title)
            continue
        episodes_to_play.append(unplayed[0])

    if missing and not episodes_to_play:
        raise ServiceValidationError(
            f"No Overcast subscription matches: {', '.join(missing)}"
        )
    if missing:
        _LOGGER.warning("No Overcast subscription matches: %s", ", ".join(missing))
    if not episodes_to_play:
        _LOGGER.info("No unplayed episodes to play")
        return

    await coordinator.async_play_queue(
        player,
        [episode.episode_path for episode in episodes_to_play],
        resume=call.data[ATTR_RESUME_POSITION],
        mark_played=call.data[ATTR_MARK_PLAYED],
        speed=speed,
    )


async def _async_mark_played(call: ServiceCall) -> None:
    coordinator = _get_coordinator(call.hass, call)
    try:
        episode_path = episode_path_from_url(call.data[ATTR_EPISODE_URL])
    except OvercastInvalidURLError as err:
        raise ServiceValidationError(
            f"episode_url must look like https://overcast.fm/+<id>: {err}"
        ) from err
    detail = await coordinator.async_get_episode_detail(episode_path)
    await coordinator.async_mark_played(detail)


async def _async_refresh(call: ServiceCall) -> None:
    if call.data.get(ATTR_CONFIG_ENTRY_ID):
        coordinators = [_get_coordinator(call.hass, call)]
    else:
        coordinators = [entry.runtime_data for entry in _loaded_entries(call.hass)]
    for coordinator in coordinators:
        coordinator.episodes.clear()
        await coordinator.async_refresh()


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Overcast service actions."""
    hass.services.async_register(
        DOMAIN, SERVICE_PLAY_LATEST, _async_play_latest, schema=PLAY_LATEST_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_MARK_PLAYED, _async_mark_played, schema=MARK_PLAYED_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, _async_refresh, schema=REFRESH_SCHEMA
    )
