# -*- coding: utf-8 -*-
"""MCP 协议适配层。

协议版本常量未对照当次 PyPI 版本号。4A 使用标准库 JSON-RPC STDIO 兼容层，
不依赖官方 mcp 包。
"""

from .protocol import MCP_PROTOCOL_VERSION
from .schema import SCHEMA_VERSION

__all__ = ["MCP_PROTOCOL_VERSION", "SCHEMA_VERSION"]
