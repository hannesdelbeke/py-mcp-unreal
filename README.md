# UnrealMCPLogForwarder
A pure Python Unreal plugin containing a MCP-like server that lets an AI/LLM talk to Unreal by:  
- Reading Unreal's Output Log
- Sending Python code to execute in Unreal

There are no pre-programmed commands, it fully relies on your AI executing Unreal Python commands, with no guardrails.
There may be limits, since Unreal does not expose everything to Python.

<img width="1394" height="429" alt="image" src="https://github.com/user-attachments/assets/c9f9d773-cb35-4937-a800-f16cf6b8f9f8" />

_I asked GPT to create a small maze with cubes_

For now, I mostly plan to use this to ask an AI "what went wrong?" by checking the log, without having to copy/paste it manually.
Everything below is AI-generated (with light edits).

---

Expose a small MCP-like HTTP server inside the Unreal Editor so OpenCode can:

- Read the latest Unreal log lines (tail the log file)
- Execute arbitrary Python in the running Unreal Editor (for inspection / automation)

This is a project plugin. Unreal runs `Content/Python/init_unreal.py` automatically when the plugin is enabled.

## What You Get

An HTTP server bound to `127.0.0.1:3001` (configurable) with MCP-like endpoints:

- `GET /mcp` tool discovery
- `POST /mcp/messages` tool execution

Tools:

- `unreal_logs/get_logs` - return last N lines of the Unreal log
- `unreal_logs/get_log_path` - show which log file is being used + search paths
- `unreal_logs/exec` - run Python code inside Unreal and return stdout / result
  - Note: Unreal editor APIs generally require running on the editor/main thread. The plugin schedules execution accordingly.

## Install (Project Plugin)

1. Copy this folder to your project:

   `<ProjectRoot>/Plugins/UnrealMCPLogForwarder`

2. In the Unreal Editor:

   `Edit -> Plugins` and enable `MCP Log Forwarder`.

3. Restart the Unreal Editor.

You should see startup logs indicating that the server started.

## Configure OpenCode

Add this to your OpenCode config (global example on Windows):

`C:\Users\hannes\.config\opencode\opencode.json`

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

Restart OpenCode.

## Usage

In OpenCode prompts:

- Read logs:
  - `use unreal_logs/get_logs with limit=200`
- Verify what log file is being tailed:
  - `use unreal_logs/get_log_path`
- Execute Python inside Unreal:
  - `use unreal_logs/exec with code="print('hello from unreal')"`
  - `use unreal_logs/exec with code="import unreal; len(unreal.EditorLevelLibrary.get_all_level_actors())"`

Example (project name derived from `.uproject` path):

```python
import os
import unreal

uproject = unreal.Paths.get_project_file_path()
project_name = os.path.splitext(os.path.basename(str(uproject)))[0]
print("project_name", project_name)

# Example: project name + actor count
actors = unreal.EditorLevelLibrary.get_all_level_actors()
print({"project": project_name, "actor_count": len(actors)})
```

## Log Path Resolution

The plugin auto-detects the current log file:

1. Preferred: `<Project>/Saved/Logs/*.log` (via Unreal API)
2. Windows fallback: `%LOCALAPPDATA%\UnrealEngine\*\Saved\Logs\*.log`
3. Windows fallback: `%LOCALAPPDATA%\<ProjectName>\Saved\Logs\*.log`

Overrides:

- Env var: `UNREAL_MCP_LOG_PATH` (absolute path to a specific `.log` file)
- Tool arg: `path` (per-call override for `get_logs` / `get_log_path`)

## Security Notes

- The server binds to `127.0.0.1` only.
- `unreal_logs/exec` is intentionally powerful: it runs arbitrary Python in the Editor process.
  Only use this on trusted machines and do not expose the port to the network.

## Troubleshooting

- If `GET /mcp` times out, confirm the plugin is enabled and the Editor was restarted.
- If log resolution fails, call `unreal_logs/get_log_path` to see what paths were searched.
