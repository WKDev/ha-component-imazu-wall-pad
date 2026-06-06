"""Serial client for Imazu Wall Pad."""

from __future__ import annotations

import asyncio
import logging

import serial_asyncio

from wp_imazu.packet import parse_packet

from .const import DEFAULT_BAUDRATE

_LOGGER = logging.getLogger(__name__)

_PACKET_HEADER = b"\xf7"
_PACKET_TAIL = b"\xee"
_PACKET_HEADER_BYTE = 0xF7
_PACKET_TAIL_BYTE = 0xEE

# The wallpad master polls the bus continuously (~50ms between transactions).
# Writing blindly collides with a poll and the command is dropped, so we wait
# for an idle gap before transmitting and retry until the device acknowledges.
_IDLE_GAP = 0.02  # bus considered idle after this many seconds with no RX
_GAP_WAIT_TIMEOUT = 1.5  # max time to wait for an idle gap before sending anyway
_ACK_TIMEOUT = 0.4  # time to wait for a confirming STATUS after a send
_SEND_RETRIES = 2  # extra attempts (so up to _SEND_RETRIES + 1 transmissions)
_POST_SEND_DELAY = 0.05  # half-duplex settle delay after an unverified send

_CMD_CHANGE = 0x02
_CMD_STATUS = 0x04


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
        # Monotonic time of the most recent byte received, used for idle-gap
        # detection. 0.0 until the first read.
        self._last_rx: float = 0.0
        # Pending acknowledgement matchers: (predicate(raw_packet), future).
        self._ack_waiters: list[tuple] = []

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

                # Mark bus activity for idle-gap detection (see async_send).
                self._last_rx = asyncio.get_running_loop().time()
                buffer.extend(data)

                # Process complete packets from buffer
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

                    # Resolve any pending send acknowledgement waiters.
                    self._resolve_ack_waiters(packet_data)

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

    def _resolve_ack_waiters(self, packet_data: bytes) -> None:
        """Resolve any pending send waiters whose predicate matches the packet."""
        if not self._ack_waiters:
            return
        for matcher, future in list(self._ack_waiters):
            if future.done():
                continue
            try:
                if matcher(packet_data):
                    future.set_result(True)
            except Exception:  # noqa: BLE001 - a bad matcher must not break reads
                continue

    async def _wait_for_idle_gap(
        self, gap: float = _IDLE_GAP, timeout: float = _GAP_WAIT_TIMEOUT
    ) -> bool:
        """Wait until the bus has been idle for ``gap`` seconds.

        Returns True if an idle gap was found, False if it timed out (in which
        case the caller should send anyway rather than block indefinitely).
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if loop.time() - self._last_rx >= gap:
                return True
            await asyncio.sleep(0.003)
        _LOGGER.debug("No idle gap within %.2fs; sending anyway", timeout)
        return False

    @staticmethod
    def _build_ack_matcher(data: bytes):
        """Build a predicate that recognises the STATUS confirming ``data``.

        Only CHANGE commands are verified. A command is considered acknowledged
        when the same device reports a STATUS for the same sub whose payload
        contains the requested value. Returns None when no verification applies.
        """
        # data layout: 01 <device> <cmd> <value_type> <sub> <change> 00
        if len(data) < 6 or data[2] != _CMD_CHANGE:
            return None
        device, sub, want = data[1], data[4], data[5]

        def match(pkt: bytes) -> bool:
            # pkt layout: f7 <len> 01 <device> <cmd> <vt> <sub> <state...> cs ee
            if len(pkt) < 10:
                return False
            if pkt[3] != device or pkt[4] != _CMD_STATUS or pkt[6] != sub:
                return False
            # value_type can differ between CHANGE and STATUS (e.g. fan speed),
            # so confirm by the requested value appearing in the state region.
            return want in pkt[7:-2]

        return match

    async def _await_ack(self, matcher, timeout: float) -> bool:
        """Wait up to ``timeout`` for a packet matching ``matcher``."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        waiter = (matcher, future)
        self._ack_waiters.append(waiter)
        try:
            await asyncio.wait_for(future, timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            if waiter in self._ack_waiters:
                self._ack_waiters.remove(waiter)

    async def async_send(
        self, data: bytes, *, retries: int = _SEND_RETRIES, verify: bool = True
    ) -> None:
        """Send a framed packet, injecting into a bus idle gap and retrying.

        The wallpad polls continuously, so each transmission waits for an idle
        gap to avoid colliding with a poll. CHANGE commands are idempotent
        absolute sets, so we retry (up to ``retries`` extra times) until a
        confirming STATUS is observed.
        """
        if not self._connected or not self._writer:
            _LOGGER.warning("Cannot send: not connected")
            return

        packet = _make_packet(data)
        matcher = self._build_ack_matcher(data) if verify else None

        async with self._send_lock:
            for attempt in range(retries + 1):
                await self._wait_for_idle_gap()
                try:
                    self._writer.write(packet)
                    await self._writer.drain()
                except Exception as e:  # noqa: BLE001
                    _LOGGER.error("Failed to send packet: %s", e)
                    return
                _LOGGER.debug(
                    "Sent packet (try %d/%d): %s",
                    attempt + 1,
                    retries + 1,
                    packet.hex(),
                )

                if matcher is None:
                    # Unverified (e.g. SCAN): single send with half-duplex settle.
                    await asyncio.sleep(_POST_SEND_DELAY)
                    return

                if await self._await_ack(matcher, _ACK_TIMEOUT):
                    _LOGGER.debug("Command acknowledged: %s", packet.hex())
                    return

            _LOGGER.warning(
                "Command not acknowledged after %d attempts: %s",
                retries + 1,
                packet.hex(),
            )

    async def async_send_wait(self, packet: bytes) -> None:
        """Send a packet and wait for response."""
        await self.async_send(packet)
        # Wait for response processing
        await asyncio.sleep(0.1)
