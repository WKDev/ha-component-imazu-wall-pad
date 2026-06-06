"""Serial client for Imazu Wall Pad."""

from __future__ import annotations

import asyncio
import logging

import serial_asyncio

from wp_imazu.packet import parse_packet

from .const import DEFAULT_BAUDRATE

_LOGGER = logging.getLogger(__name__)


class SerialClient:
    """Serial client for Imazu Wall Pad communication."""

    def __init__(self, device: str, baudrate: int = DEFAULT_BAUDRATE) -> None:
        """Initialize the serial client."""
        self.device = device
        self.baudrate = baudrate
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._read_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self.async_packet_handler = None

    @property
    def connected(self) -> bool:
        """Return True if serial port is connected."""
        return self._connected

    async def async_connect(self) -> bool:
        """Connect to the serial port."""
        try:
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self.device,
                baudrate=self.baudrate,
                bytesize=8,
                parity="N",
                stopbits=1,
            )
            self._connected = True
            self._read_task = asyncio.create_task(self._async_read_loop())
            _LOGGER.info("Connected to serial port %s", self.device)
            return True
        except Exception as e:
            _LOGGER.error("Failed to connect to serial port %s: %s", self.device, e)
            self._connected = False
            return False

    def disconnect(self) -> None:
        """Disconnect from the serial port."""
        self._connected = False
        if self._read_task:
            self._read_task.cancel()
            self._read_task = None
        if self._writer:
            self._writer.close()
            self._writer = None
        self._reader = None
        _LOGGER.info("Disconnected from serial port %s", self.device)

    async def _async_read_loop(self) -> None:
        """Read data from serial port continuously."""
        buffer = bytearray()
        while self._connected and self._reader:
            try:
                data = await self._reader.read(1024)
                if not data:
                    _LOGGER.warning("Serial connection closed")
                    self._connected = False
                    break

                buffer.extend(data)

                # Process complete packets from buffer
                while len(buffer) >= 7:
                    # Find packet start (0xF7)
                    start_idx = buffer.find(0xF7)
                    if start_idx == -1:
                        buffer.clear()
                        break
                    if start_idx > 0:
                        buffer = buffer[start_idx:]

                    # Check if we have enough data for length byte
                    if len(buffer) < 2:
                        break

                    packet_len = buffer[1]
                    if len(buffer) < packet_len:
                        break

                    # Extract packet
                    packet_data = bytes(buffer[:packet_len])
                    buffer = buffer[packet_len:]

                    # Parse and handle packet
                    try:
                        packets = parse_packet(packet_data.hex())
                        for packet in packets:
                            if self.async_packet_handler:
                                await self.async_packet_handler(packet)
                    except Exception as e:
                        _LOGGER.debug("Failed to parse packet: %s", e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.error("Error reading from serial port: %s", e)
                await asyncio.sleep(1)

    async def async_send(self, packet: bytes) -> None:
        """Send a packet to the serial port."""
        if not self._connected or not self._writer:
            _LOGGER.warning("Cannot send: not connected")
            return

        async with self._send_lock:
            try:
                self._writer.write(packet)
                await self._writer.drain()
                # Half-duplex delay
                await asyncio.sleep(0.05)
            except Exception as e:
                _LOGGER.error("Failed to send packet: %s", e)

    async def async_send_wait(self, packet: bytes) -> None:
        """Send a packet and wait for response."""
        await self.async_send(packet)
        # Wait for response processing
        await asyncio.sleep(0.1)
