import os
import sys
import unreal


def _startup():
    plugin_dir = os.path.dirname(__file__)
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)

    try:
        import mcp_log_forwarder  # noqa: F401
        unreal.log("UnrealMCPLogForwarder: startup OK")
    except Exception as e:
        unreal.log_error(f"UnrealMCPLogForwarder: startup failed: {e}")
        try:
            import traceback
            unreal.log_error(traceback.format_exc())
        except Exception:
            pass


_startup()
