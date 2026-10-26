"""Progress sync, mark-played on idle, and the play_latest queue."""

from datetime import timedelta

from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.overcast.const import PROGRESS_FINISHED_SENTINEL

from .common import progress_posts, setup_entry

PLAYER = "media_player.living_room"
EP_URL = "https://cdn.example.com/ep.m4a"
ATP_URL = "https://cdn.example.com/atp.mp3"
FINISHED = str(PROGRESS_FINISHED_SENTINEL)


def _set_player(hass, state, position=None, duration=1800, content=EP_URL):
    attrs = {"media_duration": duration, "media_content_id": content}
    if position is not None:
        attrs["media_position"] = position
        attrs["media_position_updated_at"] = dt_util.utcnow()
    hass.states.async_set(PLAYER, state, attrs)


async def _play_latest(hass, podcasts, **extra):
    play = async_mock_service(hass, "media_player", "play_media")
    async_mock_service(hass, "media_player", "media_seek")
    _set_player(hass, "idle")
    await hass.services.async_call(
        "overcast",
        "play_latest",
        {"podcasts": podcasts, "target_player": PLAYER, **extra},
        blocking=True,
    )
    await hass.async_block_till_done()
    return play


async def test_idle_near_end_marks_played_and_plays_next(
    hass: HomeAssistant, aioclient_mock
) -> None:
    entry = await setup_entry(hass, aioclient_mock)
    play = await _play_latest(hass, ["Up First", "Accidental Tech Podcast"])
    coordinator = entry.runtime_data

    assert [c.data["media_content_id"] for c in play] == [EP_URL]
    assert coordinator.queued_episodes(PLAYER) == ["/+atp1"]

    _set_player(hass, "playing", position=1770)
    await hass.async_block_till_done()
    _set_player(hass, "idle")
    await hass.async_block_till_done()

    assert ("12345", FINISHED) in progress_posts(aioclient_mock)
    assert [c.data["media_content_id"] for c in play] == [EP_URL, ATP_URL]
    assert coordinator.is_syncing(PLAYER)
    assert coordinator.queued_episodes(PLAYER) == []


async def test_manual_stop_saves_position_and_clears_queue(
    hass: HomeAssistant, aioclient_mock
) -> None:
    entry = await setup_entry(hass, aioclient_mock)
    play = await _play_latest(hass, ["Up First", "Accidental Tech Podcast"])

    _set_player(hass, "playing", position=300)
    await hass.async_block_till_done()
    _set_player(hass, "idle")
    await hass.async_block_till_done()

    posts = progress_posts(aioclient_mock)
    assert ("12345", FINISHED) not in posts
    assert posts[-1][0] == "12345"
    assert 300 <= int(posts[-1][1]) <= 302
    assert len(play) == 1
    assert not entry.runtime_data.is_syncing(PLAYER)
    assert entry.runtime_data.queued_episodes(PLAYER) == []


async def test_mark_played_false_never_marks(hass: HomeAssistant, aioclient_mock) -> None:
    await setup_entry(hass, aioclient_mock)
    await _play_latest(hass, ["Up First"], mark_played=False)

    _set_player(hass, "playing", position=1790)
    await hass.async_block_till_done()
    _set_player(hass, "idle")
    await hass.async_block_till_done()

    assert ("12345", FINISHED) not in progress_posts(aioclient_mock)


async def test_player_off_near_end_does_not_mark(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await setup_entry(hass, aioclient_mock)
    await _play_latest(hass, ["Up First"])

    _set_player(hass, "playing", position=1790)
    await hass.async_block_till_done()
    _set_player(hass, "off")
    await hass.async_block_till_done()

    assert ("12345", FINISHED) not in progress_posts(aioclient_mock)
    assert not entry.runtime_data.is_syncing(PLAYER)


async def test_unavailable_stops_sync(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await setup_entry(hass, aioclient_mock)
    await _play_latest(hass, ["Up First"])
    _set_player(hass, "playing", position=500)
    await hass.async_block_till_done()
    hass.states.async_set(PLAYER, "unavailable")
    await hass.async_block_till_done()

    assert not entry.runtime_data.is_syncing(PLAYER)
    assert ("12345", FINISHED) not in progress_posts(aioclient_mock)


async def test_unchanged_position_is_not_posted_twice(
    hass: HomeAssistant, aioclient_mock
) -> None:
    entry = await setup_entry(hass, aioclient_mock)
    await _play_latest(hass, ["Up First"])
    coordinator = entry.runtime_data

    _set_player(hass, "playing", position=400)
    await hass.async_block_till_done()
    _set_player(hass, "paused", position=400)
    await hass.async_block_till_done()
    before = len(progress_posts(aioclient_mock))

    await coordinator._async_sync_tick(dt_util.utcnow())
    await coordinator._async_sync_tick(dt_util.utcnow() + timedelta(seconds=30))
    assert len(progress_posts(aioclient_mock)) == before
    assert progress_posts(aioclient_mock)[-1] == ("12345", "400")


async def test_other_media_on_player_stops_sync(hass: HomeAssistant, aioclient_mock) -> None:
    entry = await setup_entry(hass, aioclient_mock)
    await _play_latest(hass, ["Up First", "Accidental Tech Podcast"])
    _set_player(hass, "playing", position=100)
    await hass.async_block_till_done()
    _set_player(hass, "playing", position=5, content="spotify:track:abc")
    await hass.async_block_till_done()

    assert not entry.runtime_data.is_syncing(PLAYER)
    assert entry.runtime_data.queued_episodes(PLAYER) == []
