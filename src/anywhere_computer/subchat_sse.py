"""Bounded SSE framing for a future HTTP subchat transport.

Implements UTF-8/BOM, CR/LF/CRLF, one optional field-value space and multiline data.
Strict malformed-UTF-8 rejection and resource bounds are explicit local policies,
not claims of a complete browser EventSource implementation. No reconnect occurs.
"""
from __future__ import annotations

import codecs
from dataclasses import dataclass


class StreamLimitError(ValueError):
    """A declared resource budget was exceeded; never a generation completion."""


@dataclass(frozen=True)
class Event:
    data: str
    event: str
    last_event_id: str


class SSEDecoder:
    def __init__(self, *, max_line_bytes: int = 65536,
                 max_event_bytes: int = 524288, max_total_bytes: int = 4194304,
                 max_events: int = 4096) -> None:
        values = (max_line_bytes, max_event_bytes, max_total_bytes, max_events)
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError('Positive integer resource budgets are required')
        self.max_line_bytes = max_line_bytes
        self.max_event_bytes = max_event_bytes
        self.max_total_bytes = max_total_bytes
        self.max_events = max_events
        self.total_bytes = 0
        self.event_count = 0
        self.discarded_incomplete = False
        self._decoder = codecs.getincrementaldecoder('utf-8-sig')('strict')
        self._line: list[str] = []
        self._line_bytes = 0
        self._data: list[str] = []
        self._event_bytes = 0
        self._event_type = ''
        self._last_id = ''
        self._after_cr = False
        self._closed = False

    def feed(self, chunk: bytes) -> list[Event]:
        if self._closed:
            raise ValueError('Decoder is closed')
        if not isinstance(chunk, bytes):
            raise TypeError('Byte chunks are required')
        try:
            self.total_bytes += len(chunk)
            if self.total_bytes > self.max_total_bytes:
                raise StreamLimitError('Total byte budget exceeded')
            return self._consume(self._decoder.decode(chunk, final=False))
        except Exception:
            self._closed = True
            raise

    def _consume(self, text: str) -> list[Event]:
        output: list[Event] = []
        for char in text:
            if self._after_cr:
                self._after_cr = False
                if char == '\n':
                    continue
            if char in {'\r', '\n'}:
                line = ''.join(self._line)
                self._line.clear()
                self._line_bytes = 0
                output.extend(self._accept_line(line))
                self._after_cr = char == '\r'
            else:
                self._line_bytes += len(char.encode('utf-8'))
                if self._line_bytes > self.max_line_bytes:
                    raise StreamLimitError('Line byte budget exceeded')
                self._line.append(char)
        return output

    def _accept_line(self, line: str) -> list[Event]:
        if not line:
            event = None
            if self._data:
                self.event_count += 1
                if self.event_count > self.max_events:
                    raise StreamLimitError('Event count budget exceeded')
                event = Event('\n'.join(self._data), self._event_type or 'message', self._last_id)
            self._data.clear()
            self._event_type = ''
            self._event_bytes = 0
            return [event] if event is not None else []
        if line.startswith(':'):
            return []
        field, separator, value = line.partition(':')
        if not separator:
            value = ''
        if value.startswith(' '):
            value = value[1:]
        if field == 'data':
            self._event_bytes += len(value.encode('utf-8')) + 1
            if self._event_bytes > self.max_event_bytes:
                raise StreamLimitError('Event byte budget exceeded')
            self._data.append(value)
        elif field == 'event':
            self._event_type = value
        elif field == 'id' and '\x00' not in value:
            self._last_id = value
        # retry and unknown fields do not authorize reconnect or new POSTs.
        return []

    def finish(self) -> list[Event]:
        if self._closed:
            raise ValueError('Decoder is closed')
        try:
            output = self._consume(self._decoder.decode(b'', final=True))
            # EOF is not an implicit blank line. Do not emit a partial frame.
            self.discarded_incomplete = bool(self._line or self._data or self._event_type)
            return output
        finally:
            self._closed = True
            self._line.clear()
            self._data.clear()
            self._event_type = ''
