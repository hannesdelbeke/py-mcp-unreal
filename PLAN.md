# MCP Log Forwarder Plugin Plan and Progress

## Overview
This Unreal Engine plugin enables opencode (via MCP) to (1) read Unreal logs and (2) execute Python inside the running Unreal Editor. The plugin is defined by `UnrealMCPLogForwarder.uplugin` and runs a small HTTP MCP server inside Unreal.

## Quick Start (Using the Plugin)

1.  **Enable Plugin in Unreal**: Ensure the `UnrealMCPLogForwarder` plugin is enabled and the Unreal Editor is running. The plugin starts the MCP server on port `3001`.
2.  **Configure opencode**: Add the remote MCP config (Step 2 below).
3.  **Use the Tools**:
    - Read logs: `use the unreal_logs/get_logs tool with limit=200`
    - Diagnose log path: `use the unreal_logs/get_log_path tool`
    - Run Python in Unreal: `use the unreal_logs/exec tool with code="print('hello')"`

## Goal
- Provide an MCP server in Unreal that opencode can call.
- Log access is pull-based (opencode requests last N lines) to avoid unbounded growth.
- Python execution is tool-based (opencode sends Python code; Unreal executes and returns output).

## Plan Steps
1. **Update mcp_log_forwarder.py**:
   - Run an MCP HTTP server (in-process in Unreal) using Python stdlib on port 3001.
   - Expose tools:
     - `unreal_logs/get_logs` tails the last N lines from the resolved log file.
     - `unreal_logs/get_log_path` reports which log file is being used + search paths.
     - `unreal_logs/exec` executes arbitrary Python in the Unreal Python environment.
   - Resolve log path dynamically:
     - Preferred: `<Project>/Saved/Logs/*.log`.
     - Fallbacks: `%LOCALAPPDATA%\UnrealEngine\*\Saved\Logs\*.log` and `%LOCALAPPDATA%\<ProjectName>\Saved\Logs\*.log`.
     - Overrides: `UNREAL_MCP_LOG_PATH` (env) or `path` tool arg (per call).

2. **Configure opencode**:
   - The user's opencode installation has been configured by creating a global config file at `~/.config/opencode/opencode.json` with the following remote MCP configuration:
     ```json
     {
       "mcp": {
         "unreal_logs": {
           "type": "remote",
           "url": "http://localhost:3001",
           "enabled": true
         }
       }
     }
     ```

3. **Test Integration**:
   - Verify log capture in Unreal (the plugin logs its status).
   - Test MCP tool call from opencode after configuration.
   - Handle errors and edge cases (e.g., empty buffer).

4. **Enhancements** (if needed):
   - Add filtering or log levels.
   - Persistent storage or rotation.
   - Security measures for local server.

## Progress
- [x] Created plugin folder structure and initial files (`UnrealMCPLogForwarder.uplugin`, etc.).
- [x] Updated mcp_log_forwarder.py to MCP server (HTTP server on port 3001).
- [x] Implemented dynamic log path resolution + `unreal_logs/get_log_path`.
- [ ] Implemented `unreal_logs/exec` Python execution tool.
- [x] Created this plan file.
- [x] Tested in Unreal Engine.
- [x] Configured and tested with opencode.

## Architecture
- Unreal runs `Content/Python/init_unreal.py` at startup.
- `init_unreal.py` imports `Content/Python/mcp_log_forwarder.py`.
- `mcp_log_forwarder.py` starts an HTTP server on `127.0.0.1:${UNREAL_MCP_PORT:-3001}` and implements MCP-like endpoints:
  - `GET /mcp` for tool discovery
  - `POST /mcp/messages` for tool execution

### Main Thread Execution
Unreal editor APIs (like `unreal.EditorAssetLibrary` and `unreal.EditorLevelLibrary`) generally must be called from the main thread.

Because the HTTP server handles requests on background threads, `unreal_logs/exec` schedules Python execution onto the main thread using a Slate tick callback and waits (with a timeout) for the result.

## Decision Log / Failed Attempts
### Attempt: Capture logs via Unreal output device
We attempted to capture logs directly inside Unreal using `unreal.OutputDevice` to register a custom output device.

Result: Unreal 5.6 Python API does not expose `unreal.OutputDevice` (error: `module 'unreal' has no attribute 'OutputDevice'`).

Conclusion: Do not try the output-device approach again for this plugin. Use on-disk log tailing instead.
<parameter name="filePath">C:\Users\hannes\Documents\Unreal Projects\Test\Plugins\UnrealMCPLogForwarder\PLAN.md
