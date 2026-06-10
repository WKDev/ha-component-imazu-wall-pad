"""Button platform for Imazu Wall Pad — elevator (EV) call.

Neither the entrance 일괄소등/EV호출 switch (hardwired to the EV module) nor the
wallpad's own EV호출 (which uses its 단지망 uplink) puts a call command on the home
RS485 bus. The EV module (device 0x34) does, however, accept a CHANGE addressed to
it: sending `01 34 02 41 10 06 00` triggers a real elevator call (verified — status
goes to 0x06 and the floor counts down to arrival). This button sends that frame.
"""

from __future__ import annotations

import asyncio

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ImazuGateway, ImazuWallPadConfigEntry
from .const import BRAND_NAME, DOMAIN, MANUFACTURER, MODEL, SW_VERSION

# device 0x34 (EV), cmd 0x02 (CHANGE), value_type 0x41, sub 0x10, value 0x06 (call)
EV_CALL_DATA = "01340241100600"
# The wallpad master polls the bus continuously; on that busy bus a single write
# can collide with a poll, so send a few times. The call is idempotent within the
# call window (the elevator is already on its way), so repeating is harmless.
_EV_CALL_REPEATS = 4
_EV_CALL_GAP = 0.3


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ImazuWallPadConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the elevator-call button for this gateway."""
    gateway: ImazuGateway = entry.runtime_data
    async_add_entities([WPElevatorCallButton(gateway)])


class WPElevatorCallButton(ButtonEntity):
    """A button that calls the elevator via the EV module."""

    _attr_icon = "mdi:elevator"

    def __init__(self, gateway: ImazuGateway) -> None:
        """Initialize the elevator-call button."""
        self.gateway = gateway
        connection_id = gateway.connection_id
        self.entity_id = f"button.{BRAND_NAME}_{connection_id}_ev_call"
        self._attr_unique_id = f"{BRAND_NAME}_{connection_id}_ev_call"
        self._attr_name = f"{BRAND_NAME.title()} EV 호출"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"ev_{connection_id}")},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=f"{BRAND_NAME} EV".title(),
            sw_version=SW_VERSION,
            via_device=(DOMAIN, connection_id),
        )

    @property
    def available(self) -> bool:
        """Return True if the gateway connection is up."""
        return self.gateway.connected

    async def async_press(self) -> None:
        """Send the elevator-call command."""
        data = bytes.fromhex(EV_CALL_DATA)
        for i in range(_EV_CALL_REPEATS):
            await self.gateway.async_send(data)
            if i < _EV_CALL_REPEATS - 1:
                await asyncio.sleep(_EV_CALL_GAP)
