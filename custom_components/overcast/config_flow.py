"""Config flow for the Overcast integration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any
from urllib.parse import quote

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_AUTH_METHOD,
    CONF_COOKIE,
    CONF_EMAIL,
    CONF_MARK_PLAYED,
    DEFAULT_MARK_PLAYED,
    DOMAIN,
    QR_POLL_INITIAL_INTERVAL,
    QR_POLL_MID_INTERVAL,
    QR_POLL_SLOW_INTERVAL,
    QR_POLL_TIMEOUT,
)
from .coordinator import OvercastConfigEntry
from .overcast_api import OvercastAuthError, OvercastClient, OvercastConnectionError

_LOGGER = logging.getLogger(__name__)

QR_UNIQUE_ID = "overcast_qr"


def qr_deep_link(token: str) -> str:
    """Return the link the Overcast app opens to approve a login."""
    return f"overcast:///auth?t={quote(token, safe='')}&l=browser"


def qr_data_uri(text: str) -> str:
    """Render text as a PNG QR code data URI. CPU-bound; run it in an executor."""
    import segno  # noqa: PLC0415 - only needed during QR login

    return segno.make(text, error="m").png_data_uri(scale=6, border=2)


def _email_schema(default: str = "") -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_EMAIL, default=default): TextSelector(
                TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
            ),
            vol.Required("password"): TextSelector(
                TextSelectorConfig(
                    type=TextSelectorType.PASSWORD, autocomplete="current-password"
                )
            ),
        }
    )


class OvercastConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Overcast."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._qr_session: aiohttp.ClientSession | None = None
        self._qr_task: asyncio.Task[str | None] | None = None
        self._qr_link: str | None = None
        self._qr_image: str | None = None
        self._qr_cookie: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: OvercastConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return OvercastOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose how to log in."""
        return self.async_show_menu(step_id="user", menu_options=["email", "qr"])

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth when the session cookie stops working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose how to log in again."""
        return self.async_show_menu(
            step_id="reauth_confirm", menu_options=["email", "qr"]
        )

    # ------------------------------------------------------------------
    # Email and password
    # ------------------------------------------------------------------

    async def async_step_email(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in with email and password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            session = async_create_clientsession(self.hass, auto_cleanup=False)
            try:
                cookie = await OvercastClient(session).login_email(
                    email, user_input["password"]
                )
            except OvercastAuthError:
                errors["base"] = "invalid_auth"
            except OvercastConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during Overcast login")
                errors["base"] = "unknown"
            else:
                return await self._async_finish(cookie, "email", email)
            finally:
                session.detach()

        default = ""
        if self.source == SOURCE_REAUTH:
            default = self._get_reauth_entry().data.get(CONF_EMAIL, "")
        return self.async_show_form(
            step_id="email",
            data_schema=self.add_suggested_values_to_schema(
                _email_schema(default), user_input or {}
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # QR login
    # ------------------------------------------------------------------

    async def async_step_qr(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show a QR code and wait in the background for the app to approve it."""
        if self._qr_task is None:
            # One session with a real cookie jar for the whole login: GET
            # /login sets an HttpOnly qr= cookie that the polls must send back.
            self._qr_session = async_create_clientsession(self.hass, auto_cleanup=False)
            client = OvercastClient(self._qr_session)
            try:
                token = await client.start_qr_login()
            except OvercastConnectionError:
                self._close_qr_session()
                return self.async_abort(reason="cannot_connect")
            self._qr_link = qr_deep_link(token)
            self._qr_image = await self.hass.async_add_executor_job(
                qr_data_uri, self._qr_link
            )
            self._qr_task = self.hass.async_create_task(
                self._async_wait_for_qr(client, token), eager_start=False
            )

        if not self._qr_task.done():
            return self.async_show_progress(
                step_id="qr",
                progress_action="qr_scan",
                description_placeholders={
                    "qr_image": self._qr_image or "",
                    "qr_link": self._qr_link or "",
                },
                progress_task=self._qr_task,
            )

        try:
            self._qr_cookie = self._qr_task.result()
        except asyncio.CancelledError:
            self._qr_cookie = None
        except Exception:
            _LOGGER.exception("Unexpected error during Overcast QR login")
            self._qr_cookie = None
        self._qr_task = None
        self._close_qr_session()

        if self._qr_cookie:
            return self.async_show_progress_done(next_step_id="qr_finish")
        return self.async_show_progress_done(next_step_id="qr_timeout")

    async def async_step_qr_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create or update the entry after the app approved the login."""
        assert self._qr_cookie is not None
        return await self._async_finish(self._qr_cookie, "qr", None)

    async def async_step_qr_timeout(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The QR code was not approved in time."""
        return self.async_abort(reason="qr_timeout")

    async def _async_wait_for_qr(self, client: OvercastClient, token: str) -> str | None:
        """Poll until the Overcast app approves the login or time runs out."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + QR_POLL_TIMEOUT
        attempt = 0
        while loop.time() < deadline:
            if cookie := await client.poll_qr_login(token):
                return cookie
            attempt += 1
            if attempt <= 60:
                interval = QR_POLL_INITIAL_INTERVAL
            elif attempt <= 120:
                interval = QR_POLL_MID_INTERVAL
            else:
                interval = QR_POLL_SLOW_INTERVAL
            await asyncio.sleep(interval)
        return None

    @callback
    def _close_qr_session(self) -> None:
        if self._qr_session is not None:
            self._qr_session.detach()
            self._qr_session = None

    @callback
    def async_remove(self) -> None:
        """Release the QR session when the flow is closed early."""
        if self._qr_task is not None and not self._qr_task.done():
            self._qr_task.cancel()
        self._close_qr_session()
        super().async_remove()

    # ------------------------------------------------------------------
    # Finish
    # ------------------------------------------------------------------

    async def _async_finish(
        self, cookie: str, auth_method: str, email: str | None
    ) -> ConfigFlowResult:
        data: dict[str, Any] = {CONF_COOKIE: cookie, CONF_AUTH_METHOD: auth_method}
        if email:
            data[CONF_EMAIL] = email

        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            if email:
                # An email login proves which account it is; a QR login does not.
                await self.async_set_unique_id(f"overcast_{email}")
                self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(entry, data_updates=data)

        await self.async_set_unique_id(f"overcast_{email}" if email else QR_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Overcast ({email})" if email else "Overcast (QR login)",
            data=data,
        )


class OvercastOptionsFlow(OptionsFlow):
    """Options for the Overcast integration."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MARK_PLAYED,
                        default=self.config_entry.options.get(
                            CONF_MARK_PLAYED, DEFAULT_MARK_PLAYED
                        ),
                    ): bool,
                }
            ),
        )
