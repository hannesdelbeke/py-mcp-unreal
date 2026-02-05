# MCP Log Forwarder Plugin Plan and Progress

## Overview
This Unreal Engine plugin enables opencode (via MCP) to read Unreal logs. The plugin is defined by `UnrealMCPLogForwarder.uplugin` and runs an MCP server that exposes a tool for pulling logs on demand, allowing opencode to query and retrieve buffered log entries.

## Quick Start (Using the Plugin)

1.  **Enable Plugin in Unreal**: Ensure the `UnrealMCPLogForwarder` plugin is enabled in your Unreal project and the Unreal Engine Editor is running. The plugin will start the MCP server on port `3001`.
2.  **Configure opencode**: The following configuration has been added to `~/.config/opencode/opencode.json` (See Step 2 below).
3.  **Use the Tool**: In opencode, you can now use the `unreal_logs/get_logs` tool:
    - `use the unreal_logs/get_logs tool to check for recent errors.`
    - `use the unreal_logs/get_logs tool with a limit of 10 to see the 10 most recent logs.`

## Goal
- Unreal Engine captures logs via a custom OutputDevice.
- Logs are buffered in memory.
- An MCP server runs locally on **port 3001**, providing a "get_logs" tool.
- Opencode connects as MCP client and can call the tool to retrieve logs.

## Plan Steps
1. **Update mcp_log_forwarder.py**:
   - Implemented an MCP HTTP server using Python's standard library modules, running on port 3001 in a separate thread.
   - Exposed the "unreal_logs/get_logs" tool, which returns the last X log lines (default 500) from a max buffer of 5000 lines.
   - Kept background log capture and buffering with buffer size limits.

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
- [x] Updated mcp_log_forwarder.py to MCP server (Implemented HTTP server on port 3001, exposed `unreal_logs/get_logs` tool with log line limit).
- [x] Created this plan file.
- [ ] Tested in Unreal Engine.
- [ ] Configured and tested with opencode. (Pending creation of opencode.json).</content>
<parameter name="filePath">C:\Users\hannes\Documents\Unreal Projects\Test\Plugins\UnrealMCPLogForwarder\PLAN.md