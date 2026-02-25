"""UnrealPyMCP module entrypoint.

This wrapper keeps backwards compatibility by re-exporting the existing
implementation from mcp_log_forwarder.py while enabling the preferred module
name `unreal_py_mcp`.
"""

from mcp_log_forwarder import *  # noqa: F401,F403
