"""Config flow for Imazu Wall Pad integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_CONNECTION_TYPE,
    CONF_DEVICE,
    CONNECTION_SERIAL,
    DEFAULT_BAUDRATE,
    DEFAULT_DEVICE,
    DOMAIN,
)
from .helper import device_to_id

_LOGGER = logging.getLogger(__name__)

SERIAL_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE, default=DEFAULT_DEVICE): cv.string,
    }
)


async def async_validate_serial_connection(device: str) -> dict[str, str]:
    """Validate if a connection to the serial port can be established."""
    errors = {}

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
        """Handle the initial step - Serial connection setup."""
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=SERIAL_CONFIG_SCHEMA
            )

        device = user_input[CONF_DEVICE]

        if errors := await async_validate_serial_connection(device):
            return self.async_show_form(
                step_id="user",
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
