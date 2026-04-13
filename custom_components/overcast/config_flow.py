"""Config flow for Overcast integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant

from .const import (
    CONF_AUTH_METHOD,
    CONF_COOKIE,
    CONF_EMAIL,
    DOMAIN,
    QR_POLL_INITIAL_INTERVAL,
    QR_POLL_MID_INTERVAL,
    QR_POLL_SLOW_INTERVAL,
    QR_POLL_TIMEOUT,
)
from .overcast_api import OvercastAuthError, OvercastClient, OvercastConnectionError

_LOGGER = logging.getLogger(__name__)


class OvercastConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Overcast."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._qr_token: str | None = None
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — choose auth method."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["email", "qr"],
        )

    async def async_step_email(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle email/password login."""
        errors: dict[str, str] = {}

        if user_input is not None:
            async with aiohttp.ClientSession() as session:
                client = OvercastClient(session)
                try:
                    cookie = await client.login_email(
                        user_input["email"], user_input["password"]
                    )
                except OvercastAuthError:
                    errors["base"] = "invalid_auth"
                except OvercastConnectionError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error during Overcast login")
                    errors["base"] = "unknown"
                else:
                    return await self._create_or_update_entry(
                        cookie=cookie,
                        auth_method="email",
                        email=user_input["email"],
                    )

        return self.async_show_form(
            step_id="email",
            data_schema=vol.Schema({
                vol.Required("email"): str,
                vol.Required("password"): str,
            }),
            errors=errors,
        )

    async def async_step_qr(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle QR code login."""
        errors: dict[str, str] = {}

        if self._qr_token is None:
            # Initiate QR login
            async with aiohttp.ClientSession() as session:
                client = OvercastClient(session)
                try:
                    self._qr_token = await client.start_qr_login()
                except OvercastConnectionError:
                    errors["base"] = "cannot_connect"
                    return self.async_show_form(
                        step_id="qr",
                        errors=errors,
                    )

        # Poll for QR approval
        qr_url = f"overcast:///login?qr={self._qr_token}"

        if user_input is not None or self._qr_token:
            cookie = await self._poll_qr_login(self._qr_token)
            if cookie:
                return await self._create_or_update_entry(
                    cookie=cookie,
                    auth_method="qr",
                )
            errors["base"] = "qr_timeout"
            self._qr_token = None

        return self.async_show_form(
            step_id="qr",
            description_placeholders={"qr_url": qr_url},
            errors=errors,
        )

    async def _poll_qr_login(self, qr_token: str) -> str | None:
        """Poll the QR login endpoint until approved or timeout."""
        async with aiohttp.ClientSession() as session:
            client = OvercastClient(session)
            elapsed = 0
            attempt = 0

            while elapsed < QR_POLL_TIMEOUT:
                result = await client.poll_qr_login(qr_token)
                if result:
                    return result

                attempt += 1
                if attempt <= 60:
                    interval = QR_POLL_INITIAL_INTERVAL
                elif attempt <= 120:
                    interval = QR_POLL_MID_INTERVAL
                else:
                    interval = QR_POLL_SLOW_INTERVAL

                await asyncio.sleep(interval)
                elapsed += interval

        return None

    async def _create_or_update_entry(
        self,
        cookie: str,
        auth_method: str,
        email: str | None = None,
    ) -> ConfigFlowResult:
        """Create or update a config entry."""
        data = {
            CONF_COOKIE: cookie,
            CONF_AUTH_METHOD: auth_method,
        }
        if email:
            data[CONF_EMAIL] = email

        if self._reauth_entry:
            self.hass.config_entries.async_update_entry(
                self._reauth_entry,
                data={**self._reauth_entry.data, **data},
            )
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        await self.async_set_unique_id(f"overcast_{email or 'qr'}")
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"Overcast ({email or 'QR Login'})",
            data=data,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_user()
