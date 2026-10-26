"""The Overcast integration."""

from __future__ import annotations

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import CONF_COOKIE, DOMAIN
from .coordinator import OvercastConfigEntry, OvercastCoordinator
from .overcast_api import OvercastClient, OvercastConnectionError
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services once, so they exist even before an entry loads."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OvercastConfigEntry) -> bool:
    """Set up Overcast from a config entry."""
    # Own session with no cookie jar: the cookie is sent by hand on every
    # request, and Set-Cookie replies must not land in HA's shared jar.
    session = async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar())
    client = OvercastClient(
        session, cookie=entry.data[CONF_COOKIE], executor=hass.async_add_executor_job
    )

    try:
        valid = await client.validate_session()
    except OvercastConnectionError as err:
        raise ConfigEntryNotReady(f"Cannot reach Overcast: {err}") from err
    if not valid:
        raise ConfigEntryAuthFailed("Overcast session expired")

    coordinator = OvercastCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OvercastConfigEntry) -> bool:
    """Unload an Overcast config entry."""
    entry.runtime_data.stop_sync()
    return True
