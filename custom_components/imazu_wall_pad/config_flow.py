"""Config flow for Imazu Wall Pad integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_CONNECTION_TYPE,
    CONF_DEVICE,
    CONNECTION_SERIAL,
    CONNECTION_TCP,
    DEFAULT_BAUDRATE,
    DEFAULT_DEVICE,
    DEFAULT_PORT,
    DOMAIN,
)
from .helper import device_to_id, format_host

_LOGGER = logging.getLogger(__name__)

DEFAULT_HOST = "192.168.0.99"

TCP_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): cv.string,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): cv.port,
    }
)

SERIAL_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE, default=DEFAULT_DEVICE): cv.string,
    }
)


async def async_validate_tcp_connection(host: str, port: int) -> dict[str, str]:
    """Validate that a TCP connection to the EW11 bridge can be opened."""
    errors: dict[str, str] = {}
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 - close errors must not mask success
            pass
    except Exception as e:  # noqa: BLE001
        _LOGGER.error("Failed to connect to %s:%d: %s", host, port, e)
        errors["base"] = "cannot_connect"
    return errors


async def async_validate_serial_connection(device: str) -> dict[str, str]:
    """Validate if a connection to the serial port can be established."""
    errors: dict[str, str] = {}

    from .serial_client import SerialClient

    client = SerialClient(device, DEFAULT_BAUDRATE)
    if not await client.async_connect():
        errors["base"] = "cannot_connect"
    client.disconnect()

    return errors


class WallPadConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Imazu Wall Pad."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user pick a connection type."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["tcp", "serial"],
        )

    async def async_step_tcp(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a TCP (EW11 RS485-to-WiFi bridge) connection."""
        if user_input is None:
            return self.async_show_form(
                step_id="tcp", data_schema=TCP_CONFIG_SCHEMA
            )

        host = user_input[CONF_HOST]
        port = user_input[CONF_PORT]

        if errors := await async_validate_tcp_connection(host, port):
            return self.async_show_form(
                step_id="tcp", data_schema=TCP_CONFIG_SCHEMA, errors=errors
            )

        await self.async_set_unique_id(format_host(host))
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"{host}:{port}",
            data={
                CONF_CONNECTION_TYPE: CONNECTION_TCP,
                CONF_HOST: host,
                CONF_PORT: port,
            },
        )

    async def async_step_serial(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a local serial (USB-to-RS485) connection."""
        if user_input is None:
            return self.async_show_form(
                step_id="serial", data_schema=SERIAL_CONFIG_SCHEMA
            )

        device = user_input[CONF_DEVICE]

        if errors := await async_validate_serial_connection(device):
            return self.async_show_form(
                step_id="serial",
                data_schema=SERIAL_CONFIG_SCHEMA,
                errors=errors,
            )

        device_id = device_to_id(device)
        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=device,
            data={
                CONF_CONNECTION_TYPE: CONNECTION_SERIAL,
                CONF_DEVICE: device,
            },
        )
