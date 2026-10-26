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
    aioclient_mock.post("https://overcast.fm/podcasts/set_progress/12345", text="8")
