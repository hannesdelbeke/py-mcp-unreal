import unreal

# Direct test script to verify Python execution
unreal.log("=== UNREAL MCP LOG FORWARDER DEBUG ===")
unreal.log("Python script is executing in Unreal")

try:
    import sys
    import os
    unreal.log(f"Python version: {sys.version}")
    unreal.log(f"Current working directory: {os.getcwd()}")
    
    # Try to import our main module
    plugin_dir = os.path.dirname(__file__)
    unreal.log(f"Plugin Python directory: {plugin_dir}")
    
    # List files in the directory
    import glob
    py_files = glob.glob(os.path.join(plugin_dir, "*.py"))
    unreal.log(f"Python files found: {py_files}")
    
    # Try to import the forwarder
    sys.path.insert(0, plugin_dir)
    import mcp_log_forwarder
    unreal.log("SUCCESS: mcp_log_forwarder imported and started")
    
except Exception as e:
    unreal.log_error(f"ERROR: {str(e)}")
    import traceback
    unreal.log_error(traceback.format_exc())

unreal.log("=== END DEBUG SCRIPT ===")