import unreal
import sys
import os

# Add the plugin Python directory to the path
plugin_path = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, plugin_path)

# Import and start the MCP log forwarder
try:
    import mcp_log_forwarder
    unreal.log("MCP Log Forwarder startup script executed successfully")
except Exception as e:
    unreal.log_error(f"Failed to start MCP Log Forwarder: {e}")


print("ran the ini for mcp")