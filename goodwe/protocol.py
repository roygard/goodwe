from __future__ import annotations

import asyncio
import io
import logging
from asyncio.futures import Future
from typing import Tuple, Optional, Callable

from .exceptions import MaxRetriesException, RequestFailedException, RequestRejectedException
from .modbus import create_modbus_request, create_modbus_multi_request, validate_modbus_response, MODBUS_READ_CMD, \
    MODBUS_WRITE_CMD, MODBUS_WRITE_MULTI_CMD

logger = logging.getLogger(__name__)


class InverterProtocol:

    def __init__(self, host: str, port: int, timeout: int, retries: int):
        self._host: str = host
        self._port: int = port
        self.timeout: int = timeout
        self.retries: int = retries
        self.protocol: asyncio.Protocol | None = None
        self.response_future: Future | None = None
        self.command: ProtocolCommand | None = None

    async def send_request(self, command: ProtocolCommand) -> Future:
        raise NotImplementedError()


class UdpInverterProtocol(InverterProtocol, asyncio.DatagramProtocol):
    def __init__(self, host: str, port: int, timeout: int = 1, retries: int = 3):
        super().__init__(host, port, timeout, retries)
        self._transport: asyncio.transports.DatagramTransport | None = None
        self._retry: int = 0

    async def _connect(self) -> None:
        if not self._transport or self._transport.is_closing():
            self._transport, self.protocol = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: self,
                remote_addr=(self._host, self._port),
            )

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        """On connection made"""
        self._transport = transport

    def connection_lost(self, exc: Optional[Exception]) -> None:
        """On connection lost"""
        if exc is not None:
            logger.debug("Socket closed with error: %s.", exc)
        self._close_transport()

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        """On datagram received"""
        try:
            if self.command.validator(data):
                logger.debug("Received: %s", data.hex())
                self.response_future.set_result(data)
            else:
                logger.debug("Received invalid response: %s", data.hex())
                self._retry += 1
                self._send_request(self.command, self.response_future)
        except RequestRejectedException as ex:
            logger.debug("Received exception response: %s", data.hex())
            self.response_future.set_exception(ex)
        self._close_transport()

    def error_received(self, exc: Exception) -> None:
        """On error received"""
        logger.debug("Received error: %s", exc)
        self.response_future.set_exception(exc)
        self._close_transport()

    async def send_request(self, command: ProtocolCommand) -> Future:
        """Send message via transport"""
        await self._connect()
        response_future = asyncio.get_running_loop().create_future()
        self._retry = 0
        self._send_request(command, response_future)
        await response_future
        return response_future

    def _send_request(self, command: ProtocolCommand, response_future: Future) -> None:
        """Send message via transport"""
        self.command = command
        self.response_future = response_future
        logger.debug("Sending: %s%s", self.command,
                     f' - retry #{self._retry}/{self.retries}' if self._retry > 0 else '')
        self._transport.sendto(self.command.request)
        asyncio.get_running_loop().call_later(self.timeout, self._retry_mechanism)

    def _retry_mechanism(self) -> None:
        """Retry mechanism to prevent hanging transport"""
        if self.response_future.done():
            self._close_transport()
        elif self._retry < self.retries:
            logger.debug("Failed to receive response to %s in time (%ds).", self.command, self.timeout)
            self._retry += 1
            self._send_request(self.command, self.response_future)
        else:
            logger.debug("Max number of retries (%d) reached, request %s failed.", self.retries, self.command)
            self.response_future.set_exception(MaxRetriesException)
            self._close_transport()

    def _close_transport(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None
        # Cancel Future on connection close
        if self.response_future and not self.response_future.done():
            self.response_future.cancel()


class TcpInverterProtocol(InverterProtocol, asyncio.Protocol):
    def __init__(self, host: str, port: int, timeout: int = 1, retries: int = 0):
        super().__init__(host, port, timeout, retries)
        self._transport: asyncio.transports.Transport | None = None
        self._retry: int = 0

    async def _connect(self) -> None:
        if not self._transport or self._transport.is_closing():
            self._transport, self.protocol = await asyncio.get_running_loop().create_connection(
                lambda: self,
                host=self._host, port=self._port,
            )

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        """On connection made"""
        logger.debug("Connection opened.")
        pass

    def eof_received(self) -> None:
        logger.debug("Connection closed.")
        self._close_transport()

    def connection_lost(self, exc: Optional[Exception]) -> None:
        """On connection lost"""
        if exc is not None:
            logger.debug("Connection closed with error: %s.", exc)
        self._close_transport()

    def data_received(self, data: bytes) -> None:
        """On data received"""
        try:
            if self.command.validator(data):
                logger.debug("Received: %s", data.hex())
                self._retry = 0
                self.response_future.set_result(data)
            else:
                logger.debug("Received invalid response: %s", data.hex())
                self.response_future.set_exception(RequestRejectedException())
                self._close_transport()
        except RequestRejectedException as ex:
            logger.debug("Received exception response: %s", data.hex())
            self.response_future.set_exception(ex)
            # self._close_transport()

    def error_received(self, exc: Exception) -> None:
        """On error received"""
        logger.debug("Received error: %s", exc)
        self.response_future.set_exception(exc)
        self._close_transport()

    async def send_request(self, command: ProtocolCommand) -> Future:
        """Send message via transport"""
        try:
            await self._connect()
            response_future = asyncio.get_running_loop().create_future()
            self._send_request(command, response_future)
            await response_future
            return response_future
        except asyncio.CancelledError:
            if self._retry < self.retries:
                logger.debug("Connection broken error")
                self._retry += 1
                self._close_transport()
                return await self.send_request(command)
            else:
                return self._max_retries_reached()
        except ConnectionRefusedError as exc:
            if self._retry < self.retries:
                logger.debug("Connection refused error: %s", exc)
                self._retry += 1
                return await self.send_request(command)
            else:
                return self._max_retries_reached()

    def _send_request(self, command: ProtocolCommand, response_future: Future) -> None:
        """Send message via transport"""
        self.command = command
        self.response_future = response_future
        logger.debug("Sending: %s%s", self.command,
                     f' - retry #{self._retry}/{self.retries}' if self._retry > 0 else '')
        self._transport.write(self.command.request)
        asyncio.get_running_loop().call_later(self.timeout, self._timeout_mechanism)

    def _timeout_mechanism(self) -> None:
        """Retry mechanism to prevent hanging transport"""
        if self.response_future.done():
            self._retry = 0
        else:
            self._close_transport()

    def _max_retries_reached(self) -> Future:
        logger.debug("Max number of retries (%d) reached, request %s failed.", self.retries, self.command)
        self._close_transport()
        self.response_future = asyncio.get_running_loop().create_future()
        self.response_future.set_exception(MaxRetriesException)
        return self.response_future

    def _close_transport(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None
        # Cancel Future on connection lost
        if self.response_future and not self.response_future.done():
            self.response_future.cancel()


class ProtocolResponse:
    """Definition of response to protocol command"""

    def __init__(self, raw_data: bytes, command: Optional[ProtocolCommand]):
        self.raw_data: bytes = raw_data
        self.command: ProtocolCommand = command
        self._bytes: io.BytesIO = io.BytesIO(self.response_data())

    def __repr__(self):
        return self.raw_data.hex()

    def response_data(self) -> bytes:
        if self.command is not None:
            return self.command.trim_response(self.raw_data)
        else:
            return self.raw_data

    def seek(self, address: int) -> None:
        if self.command is not None:
            self._bytes.seek(self.command.get_offset(address))
        else:
            self._bytes.seek(address)

    def read(self, size: int) -> bytes:
        return self._bytes.read(size)


class ProtocolCommand:
    """Definition of inverter protocol command"""

    def __init__(self, request: bytes, validator: Callable[[bytes], bool]):
        self.request: bytes = request
        self.validator: Callable[[bytes], bool] = validator

    def __eq__(self, other):
        if not isinstance(other, ProtocolCommand):
            # don't attempt to compare against unrelated types
            return NotImplemented
        return self.request == other.request

    def __hash__(self):
        return hash(self.request)

    def __repr__(self):
        return self.request.hex()

    def trim_response(self, raw_response: bytes):
        """Trim raw response from header and checksum data"""
        return raw_response

    def get_offset(self, address: int):
        """Calculate relative offset to start of the response bytes"""
        return address

    async def execute(self, protocol: InverterProtocol) -> ProtocolResponse:
        """
        Execute the protocol command on the specified connection.

        Return ProtocolResponse with raw response data
        """
        try:
            response_future = await protocol.send_request(self)
            result = response_future.result()
            if result is not None:
                return ProtocolResponse(result, self)
            else:
                raise RequestFailedException(
                    "No response received to '" + self.request.hex() + "' request."
                )
        except (asyncio.CancelledError, ConnectionRefusedError):
            raise RequestFailedException(
                "No valid response received to '" + self.request.hex() + "' request."
            ) from None


class Aa55ProtocolCommand(ProtocolCommand):
    """
    Inverter communication protocol seen mostly on older generations of inverters.
    Quite probably it is some variation of the protocol used on RS-485 serial link,
    extended/adapted to UDP transport layer.

    Each request starts with header of 0xAA, 0x55, then 0xC0, 0x7F (probably some sort of address/command)
    followed by actual payload data.
    It is suffixed with 2 bytes of plain checksum of header+payload.

    Response starts again with 0xAA, 0x55, then 0x7F, 0xC0.
    5-6th bytes are some response type, byte 7 is length of the response payload.
    The last 2 bytes are again plain checksum of header+payload.
    """

    def __init__(self, payload: str, response_type: str):
        super().__init__(
            bytes.fromhex(
                "AA55C07F"
                + payload
                + self._checksum(bytes.fromhex("AA55C07F" + payload)).hex()
            ),
            lambda x: self._validate_response(x, response_type),
        )

    @staticmethod
    def _checksum(data: bytes) -> bytes:
        checksum = 0
        for each in data:
            checksum += each
        return checksum.to_bytes(2, byteorder="big", signed=False)

    @staticmethod
    def _validate_response(data: bytes, response_type: str) -> bool:
        """
        Validate the response.
        data[0:3] is header
        data[4:5] is response type
        data[6] is response payload length
        data[-2:] is checksum (plain sum of response data incl. header)
        """
        if len(data) <= 8 or len(data) != data[6] + 9:
            logger.debug("Response has unexpected length: %d, expected %d.", len(data), data[6] + 9)
            return False
        elif response_type:
            data_rt_int = int.from_bytes(data[4:6], byteorder="big", signed=True)
            if int(response_type, 16) != data_rt_int:
                logger.debug("Response type unexpected: %04x, expected %s.", data_rt_int, response_type)
                return False
        checksum = 0
        for each in data[:-2]:
            checksum += each
        if checksum != int.from_bytes(data[-2:], byteorder="big", signed=True):
            logger.debug("Response checksum does not match.")
            return False
        return True

    def trim_response(self, raw_response: bytes):
        """Trim raw response from header and checksum data"""
        return raw_response[7:-2]


class Aa55ReadCommand(Aa55ProtocolCommand):
    """
    Inverter modbus READ command for retrieving <count> modbus registers starting at register # <offset>
    """

    def __init__(self, offset: int, count: int):
        super().__init__("011A03" + "{:04x}".format(offset) + "{:02x}".format(count), "019A")


class Aa55WriteCommand(Aa55ProtocolCommand):
    """
    Inverter aa55 WRITE command setting single register # <register> value <value>
    """

    def __init__(self, register: int, value: int):
        super().__init__("023905" + "{:04x}".format(register) + "01" + "{:04x}".format(value), "02B9")


class Aa55WriteMultiCommand(Aa55ProtocolCommand):
    """
    Inverter aa55 WRITE command setting multiple register # <register> value <value>
    """

    def __init__(self, offset: int, values: bytes):
        super().__init__("02390B" + "{:04x}".format(offset) + "{:02x}".format(len(values)) + values.hex(),
                         "02B9")


class ModbusProtocolCommand(ProtocolCommand):
    """
    Inverter communication protocol seen on newer generation of inverters, based on Modbus
    protocol over UDP transport layer.
    The modbus communication is rather simple, there are "registers" at specified addresses/offsets,
    each represented by 2 bytes. The protocol may query/update individual or range of these registers.
    Each register represents some measured value or operational settings.
    It's inverter implementation specific which register means what.
    Some values may span more registers (i.e. 4bytes measurement value over 2 registers).

    Every request usually starts with communication address (usually 0xF7, but can be changed).
    Second byte is the modbus command - 0x03 read multiple, 0x06 write single, 0x10 write multiple.
    Bytes 3-4 represent the register address (or start of range)
    Bytes 5-6 represent the command parameter (range size or actual value for write).
    Last 2 bytes of request is the CRC-16 (modbus flavor) of the request.

    Responses seem to always start with 0xAA, 0x55, then the comm_addr and modbus command.
    (If the command fails, the highest bit of command is set to 1 ?)
    For read requests, next byte is response payload length, then the actual payload.
    Last 2 bytes of response is again the CRC-16 of the response.
    """

    def __init__(self, request: bytes, cmd: int, offset: int, value: int):
        super().__init__(
            request,
            lambda x: validate_modbus_response(x, cmd, offset, value),
        )
        self.first_address: int = offset
        self.value = value

    def trim_response(self, raw_response: bytes):
        """Trim raw response from header and checksum data"""
        return raw_response[5:-2]

    def get_offset(self, address: int):
        """Calculate relative offset to start of the response bytes"""
        return (address - self.first_address) * 2


class ModbusReadCommand(ModbusProtocolCommand):
    """
    Inverter modbus READ command for retrieving <count> modbus registers starting at register # <offset>
    """

    def __init__(self, comm_addr: int, offset: int, count: int):
        super().__init__(
            create_modbus_request(comm_addr, MODBUS_READ_CMD, offset, count),
            MODBUS_READ_CMD, offset, count)

    def __repr__(self):
        if self.value > 1:
            return f'READ {self.value} registers from {self.first_address} ({self.request.hex()})'
        else:
            return f'READ register {self.first_address} ({self.request.hex()})'


class ModbusWriteCommand(ModbusProtocolCommand):
    """
    Inverter modbus WRITE command setting single modbus register # <register> value <value>
    """

    def __init__(self, comm_addr: int, register: int, value: int):
        super().__init__(
            create_modbus_request(comm_addr, MODBUS_WRITE_CMD, register, value),
            MODBUS_WRITE_CMD, register, value)

    def __repr__(self):
        return f'WRITE {self.value} to register {self.first_address} ({self.request.hex()})'


class ModbusWriteMultiCommand(ModbusProtocolCommand):
    """
    Inverter modbus WRITE command setting multiple modbus register # <register> value <value>
    """

    def __init__(self, comm_addr: int, offset: int, values: bytes):
        super().__init__(
            create_modbus_multi_request(comm_addr, MODBUS_WRITE_MULTI_CMD, offset, values),
            MODBUS_WRITE_MULTI_CMD, offset, len(values) // 2)
