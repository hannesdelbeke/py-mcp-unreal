# Unreal MCP Log Forwarder Plugin

## Plugin Status
- ✅ Plugin structure created
- ✅ Git repository initialized
- ✅ MCP server implemented (mcp_log_forwarder.py)
- 🔧 Auto-startup fix applied - CanContainContent added

## Log Source
This plugin resolves the current Unreal log file dynamically (tailing the last N lines):

- Preferred: `<Project>/Saved/Logs/*.log`
- Fallbacks on Windows:
  - `%LOCALAPPDATA%\\UnrealEngine\\*\\Saved\\Logs\\*.log`
  - `%LOCALAPPDATA%\\<ProjectName>\\Saved\\Logs\\*.log`

Overrides:
- Env var: `UNREAL_MCP_LOG_PATH` (absolute path to a specific log file)
- Tool arg: `path` (per-call override)

## Solution Applied
Added `CanContainContent: true` to plugin configuration - this is the critical requirement for Unreal Engine to auto-execute Python scripts in plugin's Content/Python folder.

## Required Steps
1. **Restart Unreal Editor** - Plugin configuration changes require a full restart
2. **Check Output Log** - Look for debug messages from the init_unreal.py script
3. **Test MCP Server** - Verify server starts on port 3001

## Test Results (Pre-Fix)
- Plugin is loaded in Unreal Editor
- MCP server not accessible on port 3001
- Python startup script not executing automatically

## Next Steps After Fix
1. Restart Unreal Editor
2. Test MCP server connection
3. Verify opencode integration
