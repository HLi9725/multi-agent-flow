# -*- coding: utf-8 -*-
"""JSON-RPC 2.0、STDIO 双帧与 Streamable HTTP 头常量。

2026-10-13 已对照规范页
https://modelcontextprotocol.io/specification/2025-11-25/basic/transports 。
会话头与协议版本头只在本文件定义。协议版本字符串保持 2025-11-25。
STDIO 按架构 10.2 同时接受 Content-Length 与单行 UTF-8 JSON。
PyPI 包名仍是 mcp，核对时的发行版本为 2.3.0。当前解释器没有装上该包，SDK 未启用。
"""

from __future__ import annotations

import json
import weakref
from typing import Any, BinaryIO, Dict, Mapping, Tuple

MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_SESSION_HEADER = "MCP-Session-Id"
MCP_PROTOCOL_HEADER = "MCP-Protocol-Version"
MCP_HTTP_PATH = "/mcp"
FRAME_CONTENT_LENGTH = "content-length"
FRAME_NEWLINE = "line"

_MAX_HEADER = 256
_MAX_BODY = 1024 * 1024
_BUFFERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class FrameError(Exception):
    """帧无法解析。消息必须为空，避免把正文带回调用方。"""

    def __init__(self, frame: str | None = None) -> None:
        super().__init__()
        if frame not in {None, FRAME_CONTENT_LENGTH, FRAME_NEWLINE}:
            frame = None
        self.frame = frame


def encode_message(payload: Mapping[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > _MAX_BODY:
        raise FrameError()
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def encode_line_message(payload: Mapping[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > _MAX_BODY:
        raise FrameError()
    return body + b"\n"


def encode_framed(payload: Mapping[str, Any], kind: str) -> bytes:
    if kind == FRAME_NEWLINE:
        return encode_line_message(payload)
    return encode_message(payload)


def read_message(stream: BinaryIO) -> Dict[str, Any] | None:
    inbound = read_framed(stream)
    if inbound is None:
        return None
    return inbound[0]


def read_inbound(stream: BinaryIO) -> Tuple[Dict[str, Any], str] | None:
    return read_framed(stream)


def read_framed(stream: BinaryIO) -> Tuple[Dict[str, Any], str] | None:
    first = _read_exact_some(stream, 1)
    if not first:
        return None
    if first in (b"C", b"c"):
        return _read_content_length(stream, first), FRAME_CONTENT_LENGTH
    return _read_line(stream, first), FRAME_NEWLINE


class _CleanEOF(Exception):
    pass


def _buffer(stream: BinaryIO) -> bytearray:
    try:
        found = _BUFFERS.get(stream)
    except TypeError:
        found = getattr(stream, "_yy_mcp_pushback", None)
        if found is None:
            found = bytearray()
            setattr(stream, "_yy_mcp_pushback", found)
        return found
    if found is None:
        found = bytearray()
        _BUFFERS[stream] = found
    return found


def _read_exact_some(stream: BinaryIO, size: int) -> bytes:
    if size <= 0:
        return b""
    pending = _buffer(stream)
    if len(pending) >= size:
        out = bytes(pending[:size])
        del pending[:size]
        return out
    need = size - len(pending)
    try:
        chunk = stream.read(need)
    except Exception:
        pending.clear()
        raise FrameError() from None
    if not chunk:
        if not pending:
            return b""
        out = bytes(pending)
        pending.clear()
        return out
    if not pending:
        return chunk
    out = bytes(pending) + chunk
    pending.clear()
    return out


def _unread(stream: BinaryIO, data: bytes) -> None:
    if data:
        _buffer(stream)[:0] = data


def _read_content_length(stream: BinaryIO, prefix: bytes) -> Dict[str, Any]:
    length = _read_length(stream, prefix)
    body = b""
    while len(body) < length:
        chunk = _read_exact_some(stream, length - len(body))
        if not chunk:
            raise FrameError(FRAME_CONTENT_LENGTH)
        body += chunk
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise FrameError(FRAME_CONTENT_LENGTH) from None
    if not isinstance(payload, dict):
        raise FrameError(FRAME_CONTENT_LENGTH)
    return payload


def _read_length(stream: BinaryIO, prefix: bytes) -> int:
    data = bytearray(prefix)
    while b"\r\n\r\n" not in data:
        chunk = _read_exact_some(stream, 1)
        if not chunk:
            if not data:
                raise _CleanEOF()
            raise FrameError()
        data.extend(chunk)
        if len(data) > _MAX_HEADER:
            data.clear()
            raise FrameError(FRAME_CONTENT_LENGTH)
    header, _sep, _rest = bytes(data).partition(b"\r\n\r\n")
    try:
        text = header.decode("ascii")
    except UnicodeError:
        raise FrameError(FRAME_CONTENT_LENGTH) from None
    length = None
    for line in text.split("\r\n"):
        if not line:
            continue
        if ":" not in line:
            raise FrameError(FRAME_CONTENT_LENGTH)
        name, value = line.split(":", 1)
        if name.strip().lower() != "content-length":
            raise FrameError(FRAME_CONTENT_LENGTH)
        try:
            parsed = int(value.strip())
        except ValueError:
            raise FrameError(FRAME_CONTENT_LENGTH) from None
        if length is not None:
            raise FrameError(FRAME_CONTENT_LENGTH)
        length = parsed
    if length is None or length < 2 or length > _MAX_BODY:
        raise FrameError(FRAME_CONTENT_LENGTH)
    return length


def _read_one(stream: BinaryIO) -> bytes:
    """一次只取一个字节。管道上的 read(n) 会一直等到 n 个字节或对端关闭。"""
    return _read_exact_some(stream, 1)


def _discard_rest_of_line(stream: BinaryIO, chunk: bytes = b"", newline_at: int = -1) -> None:
    if newline_at >= 0:
        _unread(stream, chunk[newline_at + 1 :])
        return
    pending = chunk
    while True:
        if not pending:
            pending = _read_one(stream)
            if not pending:
                return
        found = pending.find(b"\n")
        if found >= 0:
            _unread(stream, pending[found + 1 :])
            return
        pending = b""


def _read_line(stream: BinaryIO, first: bytes) -> Dict[str, Any]:
    if first == b"\n":
        raise FrameError(FRAME_NEWLINE)
    data = bytearray(first)
    while True:
        chunk = _read_one(stream)
        if not chunk:
            data.clear()
            raise FrameError(FRAME_NEWLINE)
        newline = chunk.find(b"\n")
        if newline == -1:
            if len(data) + len(chunk) > _MAX_BODY:
                data.clear()
                _discard_rest_of_line(stream)
                raise FrameError(FRAME_NEWLINE)
            data.extend(chunk)
            continue
        if len(data) + newline > _MAX_BODY:
            data.clear()
            _discard_rest_of_line(stream, chunk, newline)
            raise FrameError(FRAME_NEWLINE)
        data.extend(chunk[:newline])
        _unread(stream, chunk[newline + 1 :])
        break
    if data.endswith(b"\r"):
        del data[-1]
    if not data or len(data) > _MAX_BODY:
        data.clear()
        raise FrameError(FRAME_NEWLINE)
    try:
        payload = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        data.clear()
        raise FrameError(FRAME_NEWLINE) from None
    data.clear()
    if not isinstance(payload, dict):
        raise FrameError(FRAME_NEWLINE)
    return payload
