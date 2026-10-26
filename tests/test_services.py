"""Service schemas and the mark_played URL guard at the service level."""

import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.overcast.const import PROGRESS_FINISHED_SENTINEL
from custom_components.overcast.services import MARK_PLAYED_SCHEMA, PLAY_LATEST_SCHEMA

from .common import progress_posts, setup_entry


def test_play_latest_schema_coerces() -> None:
    data = PLAY_LATEST_SCHEMA(
        {"podcasts": "Up First", "target_player": "media_player.kitchen", "speed": "1.25"}
    )
    assert data["podcasts"] == ["Up First"]
    assert data["speed"] == 1.25
    assert data["mark_played"] is True
    assert data["resume_position"] is True


@pytest.mark.parametrize(
    "bad",
    [
        {"podcasts": [], "target_player": "media_player.kitchen"},
        {"podcasts": ["x"], "target_player": "light.kitchen"},
        {"podcasts": ["x"], "target_player": "media_player.kitchen", "speed": 9},
        {"podcasts": ["x"], "target_player": "media_player.kitchen", "speed": "fast"},
        {"target_player": "media_player.kitchen"},
    ],
)
def test_play_latest_schema_rejects(bad) -> None:
    with pytest.raises(vol.Invalid):
        PLAY_LATEST_SCHEMA(bad)


def test_mark_played_schema_requires_url() -> None:
    with pytest.raises(vol.Invalid):
        MARK_PLAYED_SCHEMA({})


@pytest.mark.parametrize(
    "url",
    [
        "https://overcast.fm.evil.com/+x",
        "https://overcast.fm@evil.com/+x",
        "@evil.com/+x",
        "http://overcast.fm/+x",
    ],
)
async def test_mark_played_rejects_other_hosts(
    hass: HomeAssistant, aioclient_mock, url
) -> None:
    await setup_entry(hass, aioclient_mock)
    calls_before = aioclient_mock.call_count
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "overcast", "mark_played", {"episode_url": url}, blocking=True
        )
    assert aioclient_mock.call_count == calls_before
    assert all(u.host == "overcast.fm" for _m, u, _d, _h in aioclient_mock.mock_calls)


async def test_mark_played_valid_url(hass: HomeAssistant, aioclient_mock) -> None:
    await setup_entry(hass, aioclient_mock)
    await hass.services.async_call(
        "overcast",
        "mark_played",
        {"episode_url": "https://overcast.fm/+new1"},
        blocking=True,
    )
    assert progress_posts(aioclient_mock) == [("12345", str(PROGRESS_FINISHED_SENTINEL))]


async def test_service_without_entry_raises(hass: HomeAssistant) -> None:
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "overcast", {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "overcast",
            "mark_played",
            {"episode_url": "https://overcast.fm/+new1"},
            blocking=True,
        )


async def test_unknown_podcast_raises(hass: HomeAssistant, aioclient_mock) -> None:
    await setup_entry(hass, aioclient_mock)
    hass.states.async_set("media_player.kitchen", "idle")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "overcast",
            "play_latest",
            {"podcasts": ["No Such Show"], "target_player": "media_player.kitchen"},
            blocking=True,
        )


async def test_exact_title_beats_substring(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await setup_entry(hass, aioclient_mock)
    coordinator = entry.runtime_data
    assert coordinator.find_podcast_by_name("up first").feed_path == "/p100-AbCdE"
    assert coordinator.find_podcast_by_name("Extra").title == "Up First Extra"
    assert coordinator.find_podcast_by_name("nothing") is None
