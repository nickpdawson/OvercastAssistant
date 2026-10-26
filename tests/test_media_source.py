"""Media browser listing and resolving."""

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.components.media_player import MediaClass
from homeassistant.components.media_source import (
    Unresolvable,
    async_browse_media,
    async_resolve_media,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .common import setup_entry

PLAYER = "media_player.living_room"


async def _setup(hass, aioclient_mock):
    assert await async_setup_component(hass, "media_source", {})
    return await setup_entry(hass, aioclient_mock)


async def test_browse(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)

    root = await async_browse_media(hass, "media-source://overcast")
    assert root.children_media_class == MediaClass.PODCAST
    assert [c.title for c in root.children][:2] == ["Up First Extra", "Up First"]

    podcast = await async_browse_media(hass, root.children[1].media_content_id)
    assert podcast.children_media_class == MediaClass.EPISODE
    old, new, _second = podcast.children
    assert new.media_class == MediaClass.EPISODE
    assert new.media_content_type == "audio/mpeg"
    assert new.title == "• New episode"
    assert old.title == "Old episode"
    assert new.thumbnail == "https://public.overcast-cdn.com/art/100.jpg"


async def test_resolve_uses_source_type_and_starts_sync(
    hass: HomeAssistant, aioclient_mock
) -> None:
    entry = await _setup(hass, aioclient_mock)
    seek = async_mock_service(hass, "media_player", "media_seek")

    media = await async_resolve_media(hass, "media-source://overcast/%2F%2Bnew1", PLAYER)
    assert media.url == "https://cdn.example.com/ep.m4a"
    assert media.mime_type == "audio/x-m4a"
    assert entry.runtime_data.is_syncing(PLAYER)

    hass.states.async_set(
        PLAYER,
        "playing",
        {
            "media_content_id": media.url,
            "media_position": 0,
            "media_position_updated_at": dt_util.utcnow(),
            "media_duration": 1800,
        },
    )
    await hass.async_block_till_done()
    assert [c.data["seek_position"] for c in seek] == [120]


async def test_resolve_rejects_non_episode_ids(hass: HomeAssistant, aioclient_mock) -> None:
    await _setup(hass, aioclient_mock)
    with pytest.raises(Unresolvable):
        await async_resolve_media(hass, "media-source://overcast/%40evil.com%2F%2Bx", None)
