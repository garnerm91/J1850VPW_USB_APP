"""Serial transport for the J1850VPW_USB bridge.

Framing:  [ STX 0x02 | LEN | CMD | data... | ETX 0x03 ]
          LEN = 1 (CMD) + len(data).   Device replies ACK 0x06 / NACK 0x15.

A background thread reads the port and pushes SerialEvent objects into a
queue.Queue that the GUI thread drains. Nothing here touches DearPyGui.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import serial
from serial.tools import list_ports

log = logging.getLogger(__name__)

STX, ETX, ACK, NACK = 0x02, 0x03, 0x06, 0x15
CMD_RX_MODE = 0x01
CMD_SEND_FRAME = 0x02
CMD_RX_FRAME = 0x10   # device -> PC: received J1850 frame (last data byte = CRC)
PARTIAL_FRAME_TIMEOUT = 0.5  # seconds before a stalled partial frame is discarded


@dataclass(frozen=True)
class SerialEvent:
    """kind: 'frame' | 'ack' | 'nack' | 'noise' | 'disconnected'"""
    kind: str
    ts: float
    data: bytes = b""
    cmd: Optional[int] = None
    message: str = ""


def build_packet(cmd: int, data: bytes = b"") -> bytes:
    """Wrap a command + payload in STX/LEN/.../ETX framing."""
    data = bytes(data)
    if len(data) + 1 > 0xFF:
        raise ValueError(f"Payload too long ({len(data)} bytes, max 254)")
    return bytes([STX, len(data) + 1, cmd]) + data + bytes([ETX])


class FrameParser:
    """Incremental stream parser (pure logic, no I/O -> easy to test).

    Uses LEN to delimit frames, so 0x02/0x03/0x06/0x15 may safely appear
    inside payload data. Resynchronises on malformed input.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    @property
    def pending(self) -> bool:
        return bool(self._buf)

    def flush(self) -> bytes:
        out = bytes(self._buf)
        self._buf.clear()
        return out

    def feed(self, chunk: bytes) -> List[Tuple[str, Optional[int], bytes]]:
        """Returns a list of (kind, cmd, data) tuples."""
        buf = self._buf
        buf.extend(chunk)
        events: List[Tuple[str, Optional[int], bytes]] = []
        noise = bytearray()

        def emit(kind, cmd=None, data=b""):
            if noise:
                events.append(("noise", None, bytes(noise)))
                noise.clear()
            events.append((kind, cmd, data))

        while buf:
            b = buf[0]
            if b == ACK:
                emit("ack"); del buf[0]
            elif b == NACK:
                emit("nack"); del buf[0]
            elif b == STX:
                if len(buf) < 2:
                    break                      # need LEN
                n = buf[1]
                if n == 0:                     # invalid: LEN must include CMD
                    noise.append(buf[0]); del buf[0]; continue
                total = n + 3                  # STX + LEN + n bytes + ETX
                if len(buf) < total:
                    break                      # wait for the rest
                if buf[total - 1] != ETX:      # bad framing -> resync
                    noise.append(buf[0]); del buf[0]; continue
                emit("frame", buf[2], bytes(buf[3:total - 1]))
                del buf[:total]
            else:
                noise.append(b); del buf[0]
        if noise:
            events.append(("noise", None, bytes(noise)))
        return events


class SerialHandler:
    def __init__(self, event_queue: "queue.Queue[SerialEvent]") -> None:
        self.events = event_queue
        self._ser: Optional[serial.Serial] = None
        self._thread: Optional[threading.Thread] = None
        self._stop: Optional[threading.Event] = None
        self._lock = threading.Lock()
        self.port_name = ""
        self.baud = 0

    # ---------------------------------------------------------------- ports
    @staticmethod
    def list_ports() -> List[Tuple[str, str]]:
        """[(device, description), ...] sorted by device name."""
        return sorted((p.device, p.description or "") for p in list_ports.comports())

    # ----------------------------------------------------------- connection
    @property
    def is_connected(self) -> bool:
        ser, th = self._ser, self._thread
        return bool(ser and ser.is_open and th and th.is_alive())

    def connect(self, port: str, baud: int) -> None:
        self.disconnect()
        ser = serial.Serial(port=port, baudrate=baud, timeout=0.05, write_timeout=1.0)
        ser.reset_input_buffer()
        stop = threading.Event()
        th = threading.Thread(target=self._reader, args=(ser, stop),
                              name="serial-reader", daemon=True)
        with self._lock:
            self._ser, self._thread, self._stop = ser, th, stop
        self.port_name, self.baud = port, baud
        th.start()

    def disconnect(self) -> None:
        with self._lock:
            ser, th, stop = self._ser, self._thread, self._stop
            self._ser = self._thread = self._stop = None
        if stop:
            stop.set()
        if th and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=1.0)
        if ser:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                log.exception("Error closing port")

    # --------------------------------------------------------------- output
    def send(self, cmd: int, data: bytes = b"") -> bytes:
        """Frame and transmit. Returns the full packet written."""
        packet = build_packet(cmd, data)
        with self._lock:
            ser = self._ser
            if ser is None or not ser.is_open:
                raise ConnectionError("Not connected")
            ser.write(packet)
            ser.flush()
        return packet

    # ---------------------------------------------------------- reader thread
    def _emit(self, kind: str, **kw) -> None:
        self.events.put(SerialEvent(kind=kind, ts=time.time(), **kw))

    def _reader(self, ser: serial.Serial, stop: threading.Event) -> None:
        parser = FrameParser()
        last_rx = time.monotonic()
        while not stop.is_set():
            try:
                chunk = ser.read(ser.in_waiting or 1)
            except (serial.SerialException, OSError) as exc:
                self._emit("disconnected", message=str(exc))
                return
            if chunk:
                last_rx = time.monotonic()
                for kind, cmd, data in parser.feed(chunk):
                    self._emit(kind, cmd=cmd, data=data)
            elif parser.pending and time.monotonic() - last_rx > PARTIAL_FRAME_TIMEOUT:
                self._emit("noise", data=parser.flush())  # stalled partial frame
