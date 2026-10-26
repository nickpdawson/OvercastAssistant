"""Helpers shared by the Overcast tests."""

from pathlib import Path

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.overcast.const import CONF_AUTH_METHOD, CONF_COOKIE, CONF_EMAIL, DOMAIN

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


def mock_entry(**data) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Overcast (me@example.com)",
        unique_id="overcast_me@example.com",
        data={
            CONF_COOKIE: "session-cookie",
            CONF_AUTH_METHOD: "email",
            CONF_EMAIL: "me@example.com",
            **data,
        },
    )


def mock_overcast(aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get("https://overcast.fm/podcasts", text=load("podcasts.html"))
    aioclient_mock.get("https://overcast.fm/p100-AbCdE", text=load("episodes.html"))
    aioclient_mock.get("https://overcast.fm/+new1", text=load("episode.html"))
    aioclient_mock.get("https://overcast.fm/+new2", text=load("episode.html"))
    aioclient_mock.get("https://overcast.fm/p300-XyZ", text=load("episodes_atp.html"))
    aioclient_mock.get("https://overcast.fm/+atp1", text=load("episode_atp.html"))
    aioclient_mock.post("https://overcast.fm/podcasts/set_progress/12345", text="8")
    aioclient_mock.post("https://overcast.fm/podcasts/set_progress/999", text="2")


async def setup_entry(hass, aioclient_mock: AiohttpClientMocker) -> MockConfigEntry:
    mock_overcast(aioclient_mock)
    entry = mock_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def progress_posts(aioclient_mock: AiohttpClientMocker) -> list[tuple[str, str]]:
    """Return (item_id, p) for every set_progress call made."""
    return [
        (url.path.rsplit("/", 1)[-1], data["p"])
        for method, url, data, _headers in aioclient_mock.mock_calls
        if method.lower() == "post" and "/set_progress/" in url.path
    ]
