"""Serial/TCP client for Imazu Wall Pad."""

from __future__ import annotations

import asyncio
import logging

from wp_imazu.packet import parse_packet

from .const import DEFAULT_BAUDRATE

_LOGGER = logging.getLogger(__name__)

_PACKET_HEADER = b"\xf7"
_PACKET_TAIL = b"\xee"
_PACKET_HEADER_BYTE = 0xF7
_PACKET_TAIL_BYTE = 0xEE

# Half-duplex settle delay between writes so back-to-back frames (e.g. the
# startup SCAN burst) don't run into each other on the bus.
_POST_SEND_DELAY = 0.05

_DEV_THERMOSTAT = 0x18
_CMD_STATUS = 0x04
# This wallpad reports each heating zone as an 8-byte record
# [mode, temp, target, 00, 00, 00, 00, 00]; absent zones read [00, ff, ff, …].
_THERMO_RECORD = 8
_THERMO_MODES = (0x01, 0x04, 0x07)  # heat / off / away


def _normalize_thermostat(raw: bytes) -> bytes:
    """Rewrite this wallpad's padded thermostat STATUS into wp_imazu's form.

    wp_imazu only understands 3-byte ``[mode, temp, target]`` zone records, but
    this wallpad pads each zone to 8 bytes and includes empty zones. Trim the
    padding, drop empty zones, and re-frame so the library parses (and expands
    the aggregate into one ThermostatPacket per zone). Any other frame — or one
    already in 3-byte form — is returned unchanged.
    """
    # raw: f7 len 01 18 04 46 <sub> <change> <state…> cs ee
    if len(raw) < 11 or raw[3] != _DEV_THERMOSTAT or raw[4] != _CMD_STATUS:
        return raw
    state = raw[8:-2]
    if not state or len(state) % _THERMO_RECORD != 0:
        return raw  # not the padded layout (e.g. a standard 3-byte report)

    zones = bytearray()
    for i in range(0, len(state), _THERMO_RECORD):
        record = state[i : i + _THERMO_RECORD]
        if record[0] in _THERMO_MODES:  # real zone; skip empty (00/ff ff) ones
            zones += record[:3]
    if not zones:
        return raw
    return _make_packet(bytes(raw[2:8]) + bytes(zones))


def _make_packet(data: bytes) -> bytes:
    """Create a packet with header, length, checksum, and tail."""
    # Packet structure: [0xF7] [length] [data] [checksum] [0xEE]
    # Length = header(1) + length(1) + data(n) + checksum(1) + tail(1)
    length = 1 + 1 + len(data) + 1 + 1
    packet = bytearray(_PACKET_HEADER)
    packet.append(length)
    packet.extend(data)

    # Calculate checksum (XOR of all bytes except checksum and tail)
    checksum = 0
    for b in packet:
        checksum ^= b
    packet.append(checksum)
    packet.extend(_PACKET_TAIL)

    return bytes(packet)


def _parse_tcp_target(device: str) -> tuple[str, int] | None:
    """Return (host, port) if ``device`` names a TCP endpoint, else None.

    Accepts ``tcp://host:port``, ``socket://host:port`` (pyserial style), or a
    bare ``host:port`` where the port is numeric. A real serial path such as
    ``/dev/ttyUSB0`` or ``COM3`` returns None and is handled as a serial port.
    """
    target = device
    for scheme in ("tcp://", "socket://"):
        if target.startswith(scheme):
            target = target[len(scheme) :]
            break
    else:
        # Bare "host:port" only — leave OS paths (/dev/..., COM3) as serial.
        if device.startswith("/") or "://" in device:
            return None

    host, sep, port = target.rpartition(":")
    if not sep or not host or not port.isdigit():
        return None
    return host, int(port)


class SerialClient:
    """Imazu Wall Pad client over a serial port or TCP bridge.

    Despite the name this client speaks the Imazu ``f7 … ee`` framing over
    either a local serial port or a TCP socket (e.g. an EW11 RS485-to-WiFi
    bridge). The transport only differs in how the StreamReader/StreamWriter
    pair is opened; framing and the read loop are shared.

    Sends are fire-and-forget. CHANGE commands are idempotent absolute sets and,
    with the wallpad detached so the bus has a single master, a single write
    lands reliably (measured 100%), so no gap-wait/ack/retry machinery is needed.
    """

    def __init__(self, device: str, baudrate: int = DEFAULT_BAUDRATE) -> None:
        """Initialize the client."""
        self.device = device
        self.baudrate = baudrate
        self._tcp_target = _parse_tcp_target(device)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._read_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self.async_packet_handler = None

    @property
    def connected(self) -> bool:
        """Return True if the connection is open."""
        return self._connected

    async def async_connect(self) -> bool:
        """Connect to the serial port or TCP bridge (e.g. EW11)."""
        try:
            if self._tcp_target is not None:
                host, port = self._tcp_target
                self._reader, self._writer = await asyncio.open_connection(host, port)
                _LOGGER.info("Connected to TCP bridge %s:%d", host, port)
            else:
                import serial_asyncio  # local serial path only

                (
                    self._reader,
                    self._writer,
                ) = await serial_asyncio.open_serial_connection(
                    url=self.device,
                    baudrate=self.baudrate,
                    bytesize=8,
                    parity="N",
                    stopbits=1,
                )
                _LOGGER.info("Connected to serial port %s", self.device)
            self._connected = True
            self._read_task = asyncio.create_task(self._async_read_loop())
            return True
        except Exception as e:
            _LOGGER.error("Failed to connect to %s: %s", self.device, e)
            self._connected = False
            return False

    def disconnect(self) -> None:
        """Disconnect from the serial port or TCP bridge."""
        self._connected = False
        if self._read_task:
            self._read_task.cancel()
            self._read_task = None
        if self._writer:
            self._writer.close()
            self._writer = None
        self._reader = None
        _LOGGER.info("Disconnected from %s", self.device)

    async def _async_read_loop(self) -> None:
        """Read data continuously and dispatch complete packets."""
        buffer = bytearray()
        while self._connected and self._reader:
            try:
                data = await self._reader.read(1024)
                if not data:
                    _LOGGER.warning("Connection closed")
                    self._connected = False
                    break

                buffer.extend(data)

                # Process complete packets from buffer.
                # Packet format: 0xF7 ... 0xEE (header to tail)
                while True:
                    # Find packet start (0xF7)
                    start_idx = buffer.find(_PACKET_HEADER_BYTE)
                    if start_idx == -1:
                        buffer.clear()
                        break
                    if start_idx > 0:
                        buffer = buffer[start_idx:]

                    # Find packet end (0xEE) after the header
                    end_idx = buffer.find(_PACKET_TAIL_BYTE, 1)
                    if end_idx == -1:
                        # No complete packet yet, wait for more data
                        break

                    # Extract complete packet (including header and tail)
                    packet_data = bytes(buffer[: end_idx + 1])
                    buffer = buffer[end_idx + 1 :]

                    # Parse and handle packet (normalising this wallpad's padded
                    # thermostat frames into the form wp_imazu understands).
                    try:
                        packets = parse_packet(_normalize_thermostat(packet_data).hex())
                        for packet in packets:
                            if self.async_packet_handler:
                                await self.async_packet_handler(packet)
                    except Exception as e:
                        _LOGGER.debug("Failed to parse packet: %s", e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.error("Error reading from connection: %s", e)
                await asyncio.sleep(1)

    async def async_send(self, data: bytes) -> None:
        """Frame and send a single packet (fire-and-forget)."""
        if not self._connected or not self._writer:
            _LOGGER.warning("Cannot send: not connected")
            return

        packet = _make_packet(data)
        async with self._send_lock:
            try:
                self._writer.write(packet)
                await self._writer.drain()
            except Exception as e:  # noqa: BLE001
                _LOGGER.error("Failed to send packet: %s", e)
                return
            _LOGGER.debug("Sent packet: %s", packet.hex())
            # Brief half-duplex settle so consecutive sends don't collide.
            await asyncio.sleep(_POST_SEND_DELAY)

    async def async_send_wait(self, packet: bytes) -> None:
        """Send a packet and wait briefly for the response to be processed."""
        await self.async_send(packet)
        await asyncio.sleep(0.1)
