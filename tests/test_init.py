"""Entry setup and error handling."""

import aiohttp

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .common import load, mock_entry, mock_overcast


async def test_setup_and_unload(hass: HomeAssistant, aioclient_mock) -> None:
    mock_overcast(aioclient_mock)
    entry = mock_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    titles = [p.title for p in entry.runtime_data.subscriptions]
    assert titles == ["Up First", "Up First Extra", "Accidental Tech Podcast"]
    assert entry.runtime_data.subscriptions[1].artwork_url == "https://overcast.fm/img/200.jpg"
    assert hass.services.has_service("overcast", "play_latest")

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_network_error_at_boot_retries(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get("https://overcast.fm/podcasts", exc=aiohttp.ClientConnectionError())
    entry = mock_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress()


async def test_server_error_at_boot_retries(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get("https://overcast.fm/podcasts", status=503)
    entry = mock_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_expired_session_starts_reauth(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(
        "https://overcast.fm/podcasts",
        status=302,
        headers={"Location": "/login"},
    )
    entry = mock_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"


def test_parse_episode_detail() -> None:
    from custom_components.overcast.overcast_api import parse_episode_detail

    ep = parse_episode_detail(load("episode.html"), "/+new1")
    assert ep.item_id == "12345"
    assert ep.start_time == 120
    assert ep.mime_type == "audio/x-m4a"
    assert ep.title == "New episode"
    assert ep.podcast_title == "Up First"
