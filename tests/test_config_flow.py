"""Config flow: email login, QR login and reauth."""

from unittest.mock import patch

import aiohttp
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)
from yarl import URL

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.overcast.config_flow import qr_data_uri, qr_deep_link
from custom_components.overcast.const import CONF_COOKIE, DOMAIN

from .common import mock_entry

LOGIN = "https://overcast.fm/login"
QR_VERIFY = "https://overcast.fm/main/login_qr_verify"


@pytest.fixture(autouse=True)
def no_setup():
    with patch("custom_components.overcast.async_setup_entry", return_value=True) as m:
        yield m


@pytest.fixture(autouse=True)
def fast_qr_poll():
    with (
        patch("custom_components.overcast.config_flow.QR_POLL_INITIAL_INTERVAL", 0),
        patch("custom_components.overcast.config_flow.QR_POLL_MID_INTERVAL", 0),
        patch("custom_components.overcast.config_flow.QR_POLL_SLOW_INTERVAL", 0),
    ):
        yield


async def _menu(hass, choice, source=config_entries.SOURCE_USER, entry=None):
    if entry is None:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": source})
    else:
        result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": choice}
    )


async def test_email_login_creates_entry(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(
        LOGIN, status=302, headers={"Location": "/podcasts"}, cookies={"o": "abc123"}
    )
    result = await _menu(hass, "email")
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "me@example.com", "password": "pw"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Overcast (me@example.com)"
    assert result["data"][CONF_COOKIE] == "abc123"
    assert result["result"].unique_id == "overcast_me@example.com"


async def test_email_login_bad_password(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(LOGIN, status=200, text="<form name=password>")
    result = await _menu(hass, "email")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "me@example.com", "password": "nope"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_email_login_offline(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.post(LOGIN, exc=aiohttp.ClientConnectionError())
    result = await _menu(hass, "email")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "me@example.com", "password": "pw"}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_email_reauth_updates_cookie(hass: HomeAssistant, aioclient_mock) -> None:
    entry = mock_entry()
    entry.add_to_hass(hass)
    aioclient_mock.post(
        LOGIN, status=302, headers={"Location": "/podcasts"}, cookies={"o": "fresh"}
    )
    result = await _menu(hass, "email", entry=entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "me@example.com", "password": "pw"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_COOKIE] == "fresh"


async def test_email_reauth_other_account_aborts(
    hass: HomeAssistant, aioclient_mock
) -> None:
    entry = mock_entry()
    entry.add_to_hass(hass)
    aioclient_mock.post(
        LOGIN, status=302, headers={"Location": "/podcasts"}, cookies={"o": "other"}
    )
    result = await _menu(hass, "email", entry=entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": "someone-else@example.com", "password": "pw"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_COOKIE] == "session-cookie"


async def test_qr_login(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(LOGIN, text="<html></html>", cookies={"qr": "tok/123"})
    polls = 0

    async def verify(method, url, data):
        nonlocal polls
        polls += 1
        assert data == {"token": "tok/123", "then": "podcasts"}
        if polls < 3:
            return AiohttpClientMockResponse(method, URL(url), text="")
        return AiohttpClientMockResponse(
            method, URL(url), text="/podcasts", cookies={"o": "qr-cookie"}
        )

    aioclient_mock.post(QR_VERIFY, side_effect=verify)

    result = await _menu(hass, "qr")
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "qr_scan"
    placeholders = result["description_placeholders"]
    assert placeholders["qr_link"] == "overcast:///auth?t=tok%2F123&l=browser"
    assert placeholders["qr_image"].startswith("data:image/png;base64,")

    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_COOKIE] == "qr-cookie"
    assert polls == 3


async def test_qr_login_times_out(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(LOGIN, text="<html></html>", cookies={"qr": "tok"})
    aioclient_mock.post(QR_VERIFY, text="")
    with patch("custom_components.overcast.config_flow.QR_POLL_TIMEOUT", 0):
        result = await _menu(hass, "qr")
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "qr_timeout"


def test_qr_helpers() -> None:
    assert qr_deep_link("abc") == "overcast:///auth?t=abc&l=browser"
    assert qr_data_uri("overcast:///auth?t=abc&l=browser").startswith("data:image/png")
