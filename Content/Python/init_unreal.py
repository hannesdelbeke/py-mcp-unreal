import os
import sys
import unreal


def _startup():
    plugin_dir = os.path.dirname(__file__)
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)

    try:
        # Preferred module name: unreal_py_mcp. Keep fallback for older filename.
        try:
            import unreal_py_mcp  # noqa: F401
        except Exception:
            import mcp_log_forwarder as unreal_py_mcp  # noqa: F401
        unreal.log("UnrealPyMCP: startup OK")
    except Exception as e:
        unreal.log_error(f"UnrealPyMCP: startup failed: {e}")
        try:
            import traceback
            unreal.log_error(traceback.format_exc())
        except Exception:
            pass


_startup()
