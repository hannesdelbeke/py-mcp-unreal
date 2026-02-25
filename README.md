# UnrealMCPLogForwarder

Project plugin that starts a local MCP-like HTTP server inside Unreal Editor so an AI client can:
- Read Unreal log output
- Execute Python in the running editor

This plugin is intentionally powerful. `unreal_logs/exec` and `unreal_logs/exec_async` run arbitrary Python in the editor process.

## What It Starts

When the plugin is enabled, Unreal runs:
- `Content/Python/init_unreal.py`

That imports `mcp_log_forwarder.py`, which starts a server on:
- `http://127.0.0.1:3001` (default)

## Endpoints

- `GET /mcp`
  - Tool discovery
  - Includes `meta.startup_guidance` for MCP auto-detection and config hints
- `GET /mcp/help`
  - Built-in API docs (tools, limits, examples, endpoints)
- `GET /health`
  - Server and main-thread runner status
  - Includes current log resolution and startup guidance
- `GET /tasks/{task_id}/status`
  - Poll async execution task state/result
- `POST /mcp/messages`
  - Execute a tool with payload: `{"tool": "...", "arguments": {...}}`

## Tools

- `unreal_logs/get_logs`
  - Return last N log lines (default 500, max 5000)
- `unreal_logs/get_log_path`
  - Return resolved log file and search paths
- `unreal_logs/exec`
  - Execute Python synchronously on Unreal main thread
  - Supports `timeout` argument
  - Exposes `report_progress(message, current=None, total=None)` in execution context
- `unreal_logs/exec_async`
  - Queue Python execution and return immediately with `task_id`
  - Poll `GET /tasks/{task_id}/status` for completion

## OpenCode Config

Example:

```json
{
  "mcp": {
    "unreal_logs": {
      "type": "remote",
      "url": "http://127.0.0.1:3001",
      "enabled": true
    }
  }
}
```

## Request Examples

Get logs:

```json
{
  "tool": "unreal_logs/get_logs",
  "arguments": { "limit": 200 }
}
```

Sync exec:

```json
{
  "tool": "unreal_logs/exec",
  "arguments": {
    "code": "print('hello from unreal')",
    "timeout": 60
  }
}
```

Async exec:

```json
{
  "tool": "unreal_logs/exec_async",
  "arguments": {
    "code": "import time\nfor i in range(3):\n    report_progress(f'step {i+1}', i+1, 3)\n    time.sleep(1)\nresult={'done': True}",
    "timeout": 60
  }
}
```

Then poll:

`GET /tasks/<task_id>/status`

## Structured Errors

HTTP/API errors are returned as JSON:

```json
{
  "status": "error",
  "error_type": "InvalidJson",
  "message": "Invalid JSON payload: ...",
  "timestamp": "2026-02-25T09:23:41.032949+00:00"
}
```

Execution failures from `unreal_logs/exec` / `exec_async` include:
- `error_type`, `message`, `stack_trace`
- `stdout`, `stderr`
- `unreal_logs` context
- `timestamp`, `timeout_seconds`

## Config Environment Variables

- `UNREAL_MCP_PORT`
  - Server port (default `3001`)
- `UNREAL_MCP_LOG_PATH`
  - Absolute path to a specific `.log` file
- `UNREAL_MCP_EXEC_TIMEOUT`
  - Default sync/async execution timeout seconds (default `60`)
- `UNREAL_MCP_MAX_EXEC_TIMEOUT`
  - Maximum allowed timeout seconds (default `300`)
- `UNREAL_MCP_ERROR_LOG_LINES`
  - Number of log lines attached to exec error payloads (default `120`)
- `UNREAL_MCP_DISABLE_SERVER`
  - Set to `1` to disable server startup

## Log Path Resolution

Resolution order:
1. Explicit `path` argument (tool call)
2. `UNREAL_MCP_LOG_PATH`
3. `<Project>/Saved/Logs/*.log` (preferred)
4. `%LOCALAPPDATA%/UnrealEngine/*/Saved/Logs/*.log`
5. `%LOCALAPPDATA%/<ProjectName>/Saved/Logs/*.log`

## Notes on Async Behavior

- Async is non-blocking for the MCP client (you get a `task_id` immediately).
- Unreal Python still executes on Unreal's main thread for editor safety.
- Use `exec_async` + polling to avoid HTTP wait timeouts for long jobs.

## Troubleshooting

- If `/mcp` is down:
  - verify plugin enabled
  - verify Python plugin enabled
  - restart Unreal Editor
- If logs cannot be resolved:
  - call `unreal_logs/get_log_path`
  - set `UNREAL_MCP_LOG_PATH`
- For capability/introspection:
  - call `/mcp/help` and `/health`
