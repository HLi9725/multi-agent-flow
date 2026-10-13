# -*- coding: utf-8 -*-
"""MCP 协议适配层。

协议版本字符串保持 2025-11-25。默认传输是标准库 compat。
PyPI 包名仍是 mcp，核对版本为 2.3.0。当前解释器没有装上该包，SDK 后端未启用。
"""

from .protocol import MCP_PROTOCOL_VERSION
from .schema import SCHEMA_VERSION

__all__ = ["MCP_PROTOCOL_VERSION", "SCHEMA_VERSION"]
