# -*- coding: utf-8 -*-
"""JSON-RPC 2.0 与 STDIO 帧。

未对照当次 PyPI 版本号。协议版本字符串按架构第 7 节锁定。
帧在核对官方 STDIO 章节前使用 Content-Length，核对后只改本文件。
"""

from __future__ import annotations

import json
from typing import Any, BinaryIO, Dict, Mapping

MCP_PROTOCOL_VERSION = "2025-11-25"

_MAX_HEADER = 256
_MAX_BODY = 1024 * 1024


class FrameError(Exception):
    """帧无法解析。消息必须为空，避免把正文带回调用方。"""


def encode_message(payload: Mapping[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > _MAX_BODY:
        raise FrameError()
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def read_message(stream: BinaryIO) -> Dict[str, Any] | None:
    try:
        length = _read_length(stream)
    except _CleanEOF:
        return None
    body = _read_exact(stream, length)
    try:
        text = body.decode("utf-8")
        payload = json.loads(text)
    except (UnicodeError, json.JSONDecodeError):
        raise FrameError() from None
    if not isinstance(payload, dict):
        raise FrameError()
    return payload


class _CleanEOF(Exception):
    pass


def _read_length(stream: BinaryIO) -> int:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = stream.read(1)
        if not chunk:
            if not data:
                raise _CleanEOF()
            raise FrameError()
        data += chunk
        if len(data) > _MAX_HEADER:
            raise FrameError()
    header = data.split(b"\r\n\r\n", 1)[0]
    try:
        text = header.decode("ascii")
    except UnicodeError:
        raise FrameError() from None
    length = None
    for line in text.split("\r\n"):
        if not line:
            continue
        if ":" not in line:
            raise FrameError()
        name, value = line.split(":", 1)
        if name.strip().lower() != "content-length":
            raise FrameError()
        try:
            parsed = int(value.strip())
        except ValueError:
            raise FrameError() from None
        if length is not None:
            raise FrameError()
        length = parsed
    if length is None or length < 2 or length > _MAX_BODY:
        raise FrameError()
    return length


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    buf = b""
    while len(buf) < size:
        chunk = stream.read(size - len(buf))
        if not chunk:
            raise FrameError()
        buf += chunk
    return buf
