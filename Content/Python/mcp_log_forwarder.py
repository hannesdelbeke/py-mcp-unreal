import os
import json
import threading
import http.server
import socketserver
import glob
import time
import datetime
import traceback
import uuid

try:
    import unreal
except Exception:
    unreal = None

# --- Configuration ---
MCP_PORT = int(os.getenv("UNREAL_MCP_PORT", "3001"))

# Optional overrides
# - UNREAL_MCP_LOG_PATH: absolute path to a specific log file
# - UNREAL_PROJECT_NAME: used if Unreal API is not available
LOG_PATH_OVERRIDE = os.getenv("UNREAL_MCP_LOG_PATH")

RETURN_LOG_LINES = 500  # Default lines to return per tool call
LOG_LINE_LIMIT = 5000  # Safety cap on returned lines
DEFAULT_EXEC_TIMEOUT = int(os.getenv("UNREAL_MCP_EXEC_TIMEOUT", "60"))
MAX_EXEC_TIMEOUT = int(os.getenv("UNREAL_MCP_MAX_EXEC_TIMEOUT", "300"))
RECENT_ERROR_LOG_LINES = int(os.getenv("UNREAL_MCP_ERROR_LOG_LINES", "120"))

_CACHED_LOG_PATH = None
_CACHED_SEARCH = None

_MAIN_THREAD_QUEUE = []
_MAIN_THREAD_LOCK = threading.Lock()
_MAIN_THREAD_INIT = False
_MAIN_THREAD_READY = False
_MAIN_THREAD_IDENT = None

_TICK_HANDLE = None
_TICK_KIND = None  # "editor", "slate_post", or "slate_pre"

_SERVER = None
_SERVER_THREAD = None
_TASKS = {}
_TASKS_LOCK = threading.Lock()


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _clamp_exec_timeout(timeout):
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_EXEC_TIMEOUT
    return max(1, min(timeout, MAX_EXEC_TIMEOUT))


def _structured_error(error_type, message, status_code=500, details=None):
    out = {
        "status": "error",
        "error_type": str(error_type),
        "message": str(message),
        "timestamp": _utc_now_iso(),
    }
    if details is not None:
        out["details"] = details
    out["_status_code"] = int(status_code)
    return out


def _log_info(msg):
    try:
        if unreal is not None:
            unreal.log(str(msg))
        else:
            print(str(msg))
    except Exception:
        pass


def _log_error(msg):
    try:
        if unreal is not None:
            unreal.log_error(str(msg))
        else:
            print("ERROR: " + str(msg))
    except Exception:
        pass


def _safe_get_ident():
    try:
        return threading.get_ident()
    except Exception:
        return None


def _ensure_main_thread_runner():
    """Register a tick callback to run queued work on the editor main thread."""
    global _MAIN_THREAD_INIT, _MAIN_THREAD_READY
    global _MAIN_THREAD_IDENT
    global _TICK_HANDLE, _TICK_KIND

    if _MAIN_THREAD_INIT:
        return

    if unreal is None:
        _MAIN_THREAD_INIT = True
        _MAIN_THREAD_READY = False
        return

    # Prefer editor tick over Slate tick. In some configurations Slate can be multi-threaded.
    register = getattr(unreal, "register_editor_tick_callback", None)
    if register is not None:
        _TICK_KIND = "editor"
    else:
        register = getattr(unreal, "register_slate_post_tick_callback", None)
        if register is not None:
            _TICK_KIND = "slate_post"
        else:
            register = getattr(unreal, "register_slate_pre_tick_callback", None)
            if register is not None:
                _TICK_KIND = "slate_pre"

    if register is None:
        _log_error("No editor/slate tick callback registration found; cannot run exec on main thread.")
        _MAIN_THREAD_INIT = True
        _MAIN_THREAD_READY = False
        return

    def _tick(_delta_time):
        global _MAIN_THREAD_IDENT

        # Capture the runner thread identity for diagnostics.
        if _MAIN_THREAD_IDENT is None:
            _MAIN_THREAD_IDENT = _safe_get_ident()

        # Drain queue
        while True:
            with _MAIN_THREAD_LOCK:
                if not _MAIN_THREAD_QUEUE:
                    break
                fn = _MAIN_THREAD_QUEUE.pop(0)
            try:
                fn()
            except Exception as e:
                _log_error(f"Main-thread task failed: {e}")

    try:
        # If this is called from init_unreal.py during editor startup, we're on the main thread.
        if _MAIN_THREAD_IDENT is None:
            _MAIN_THREAD_IDENT = _safe_get_ident()
        _TICK_HANDLE = register(_tick)
        _MAIN_THREAD_INIT = True
        _MAIN_THREAD_READY = True
        _log_info(f"Main-thread runner registered via {_TICK_KIND} tick callback")
    except Exception as e:
        _log_error(f"Failed to register main-thread runner: {e}")
        _MAIN_THREAD_INIT = True
        _MAIN_THREAD_READY = False


def _get_project_name():
    if unreal is not None:
        try:
            get_name = getattr(unreal.SystemLibrary, "get_project_name", None)
            if get_name is not None:
                name = get_name()
                if name:
                    return str(name)
        except Exception:
            pass

        # Fallback: derive from project file path
        try:
            p = unreal.Paths.get_project_file_path()
            if p:
                base = os.path.basename(str(p))
                root, _ext = os.path.splitext(base)
                if root:
                    return root
        except Exception:
            pass

        # Fallback: derive from project directory
        try:
            d = unreal.Paths.project_dir()
            if d:
                base = os.path.basename(os.path.normpath(str(d)))
                if base:
                    return base
        except Exception:
            pass
    env_name = os.getenv("UNREAL_PROJECT_NAME")
    return env_name if env_name else None


def _pick_newest_log(log_files, project_name=None):
    if not log_files:
        return None

    preferred = []
    if project_name:
        pn = project_name.lower()
        for p in log_files:
            base = os.path.basename(p).lower()
            if base.startswith(pn) and base.endswith(".log"):
                preferred.append(p)

    candidates = preferred if preferred else log_files
    candidates = [p for p in candidates if os.path.isfile(p)]
    if not candidates:
        return None

    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return candidates[0]


def _resolve_log_file_path(explicit_path=None, use_cache=True):
    global _CACHED_LOG_PATH, _CACHED_SEARCH

    if use_cache and explicit_path is None and _CACHED_LOG_PATH:
        return _CACHED_LOG_PATH, (_CACHED_SEARCH or [])

    searched = []

    def _try_path(p):
        if not p:
            return None
        p = os.path.expandvars(os.path.expanduser(str(p)))
        p = os.path.normpath(p)
        searched.append(p)
        if os.path.isfile(p):
            return p
        return None

    # 1) Explicit tool argument path
    if explicit_path:
        resolved = _try_path(explicit_path)
        if resolved:
            return resolved, searched

    # 2) Env override
    if LOG_PATH_OVERRIDE:
        resolved = _try_path(LOG_PATH_OVERRIDE)
        if resolved:
            _CACHED_LOG_PATH, _CACHED_SEARCH = resolved, searched
            return resolved, searched

    project_name = _get_project_name()

    # 3) Project Saved/Logs (Editor/project)
    if unreal is not None:
        try:
            saved_dir = unreal.Paths.project_saved_dir()
            if saved_dir:
                saved_dir_abs = unreal.Paths.convert_relative_path_to_full(saved_dir)
                logs_dir = os.path.join(str(saved_dir_abs), "Logs")
                searched.append(os.path.normpath(logs_dir))
                if os.path.isdir(logs_dir):
                    logs = glob.glob(os.path.join(logs_dir, "*.log"))
                    picked = _pick_newest_log(logs, project_name=project_name)
                    if picked:
                        _CACHED_LOG_PATH, _CACHED_SEARCH = picked, searched
                        return picked, searched
        except Exception as e:
            print(e)  # todo logging warn

    # 4) Windows LocalAppData locations
    localappdata = os.getenv("LOCALAPPDATA")
    if localappdata:
        # 4a) Engine logs: %LOCALAPPDATA%\UnrealEngine\*\Saved\Logs\
        ue_root = os.path.join(localappdata, "UnrealEngine")
        searched.append(os.path.normpath(ue_root))
        if os.path.isdir(ue_root):
            version_dirs = [os.path.join(ue_root, d) for d in os.listdir(ue_root)]
            logs = []
            for vd in version_dirs:
                logs_dir = os.path.join(vd, "Saved", "Logs")
                searched.append(os.path.normpath(logs_dir))
                if os.path.isdir(logs_dir):
                    logs.extend(glob.glob(os.path.join(logs_dir, "*.log")))
            picked = _pick_newest_log(logs, project_name=project_name)
            if picked:
                _CACHED_LOG_PATH, _CACHED_SEARCH = picked, searched
                return picked, searched

        # 4b) Packaged-ish logs: %LOCALAPPDATA%\<Project>\Saved\Logs\
        if project_name:
            logs_dir = os.path.join(localappdata, project_name, "Saved", "Logs")
            searched.append(os.path.normpath(logs_dir))
            if os.path.isdir(logs_dir):
                logs = glob.glob(os.path.join(logs_dir, "*.log"))
                picked = _pick_newest_log(logs, project_name=project_name)
                if picked:
                    _CACHED_LOG_PATH, _CACHED_SEARCH = picked, searched
                    return picked, searched

    _CACHED_LOG_PATH, _CACHED_SEARCH = None, searched
    return None, searched

# --- Log Tailing Utility ---

def tail_log_file(filename, n=RETURN_LOG_LINES):
    """Return last n lines from filename.

    Implementation reads chunks from EOF backwards to avoid loading the whole file.
    """
    try:
        # Check if the file exists before attempting to open
        if not os.path.exists(filename):
            _log_error(f"Log file not found at: {filename}")
            return [f"ERROR: Log file not found at {filename}"]

        with open(filename, "rb") as f:
            # Move the file pointer to the end
            f.seek(0, os.SEEK_END)

            block_size = 8192
            buf = b""
            lines = []
            pos = f.tell()

            while len(lines) <= n and pos > 0:
                read_start = max(0, pos - block_size)
                f.seek(read_start)
                chunk = f.read(pos - read_start)
                pos = read_start

                buf = chunk + buf
                parts = buf.split(b"\n")

                # keep first (possibly partial) line in buf, consume full lines
                buf = parts[0]
                full_lines = parts[1:]

                # add consumed lines to list (as bytes)
                for bline in reversed(full_lines):
                    if bline:
                        lines.append(bline)
                        if len(lines) >= n:
                            break

            # lines currently reversed (newest-first)
            out = []
            for bline in reversed(lines[:n]):
                try:
                    out.append(bline.decode("utf-8", errors="ignore"))
                except Exception:
                    out.append(str(bline))
            return out

    except Exception as e:
        _log_error(f"Error reading log file: {e}")
        return [f"ERROR: Could not read log file: {e}"]


def get_recent_logs(limit=RECENT_ERROR_LOG_LINES, path=None):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = RECENT_ERROR_LOG_LINES
    limit = max(1, min(limit, LOG_LINE_LIMIT))

    resolved, _searched = _resolve_log_file_path(explicit_path=path, use_cache=True)
    if not resolved:
        return []
    return tail_log_file(resolved, limit)


# --- MCP Tool Implementation ---

def get_logs(limit=RETURN_LOG_LINES, path=None):
    """Retrieves the most recent Unreal Engine log entries from the resolved log file."""
    # Ensure limit is an integer and within the safe bounds
    try:
        limit = int(limit)
    except (ValueError, TypeError):
        limit = RETURN_LOG_LINES
        
    limit = max(1, min(limit, LOG_LINE_LIMIT))
    
    resolved, searched = _resolve_log_file_path(explicit_path=path)
    if not resolved:
        return [
            "ERROR: Could not resolve Unreal log file.",
            "Searched:",
        ] + searched[-20:]

    return tail_log_file(resolved, limit)


def get_log_path(path=None):
    """Return the current resolved log path and search locations."""
    resolved, searched = _resolve_log_file_path(explicit_path=path, use_cache=(path is None))
    return {
        "project": _get_project_name(),
        "resolved": resolved,
        "searched": searched,
        "hint": {
            "override_env": "UNREAL_MCP_LOG_PATH",
            "port_env": "UNREAL_MCP_PORT",
        },
    }


def exec_python(code, mode="exec", timeout=DEFAULT_EXEC_TIMEOUT):
    """Execute Python inside Unreal and return output.

    Parameters:
    - code: python source code
    - mode: "exec" (default) or "eval"
    """

    timeout = _clamp_exec_timeout(timeout)

    if unreal is None:
        return _structured_error(
            error_type="UnrealUnavailable",
            message="unreal module not available",
            status_code=503,
        )

    if code is None:
        return _structured_error(
            error_type="ValidationError",
            message="Missing required argument: code",
            status_code=400,
        )

    try:
        code_str = str(code)
    except Exception:
        code_str = code

    def _run():
        return _execute_code_now(code_str=code_str, mode=mode, timeout=timeout)

    # Unreal editor APIs generally must run on the main thread.
    _ensure_main_thread_runner()

    # If we are already on the runner thread, execute directly to avoid deadlocks/timeouts.
    try:
        current_ident = _safe_get_ident()
        if _MAIN_THREAD_IDENT is not None and current_ident == _MAIN_THREAD_IDENT:
            out = _run()
            out["thread"] = {
                "current_ident": current_ident,
                "runner_ident": _MAIN_THREAD_IDENT,
                "runner_kind": _TICK_KIND,
                "scheduled": False,
            }
            return out
    except Exception:
        pass

    # If we cannot schedule, fail fast. Running Unreal editor APIs from this
    # request thread will often throw "outside the main game thread".
    if unreal is None or not _MAIN_THREAD_READY:
        current_ident = _safe_get_ident()
        return {
            "ok": False,
            "status": "error",
            "error_type": "MainThreadUnavailable",
            "message": "Main-thread runner not available; cannot execute Unreal editor APIs from MCP request thread",
            "mode": mode,
            "stdout": "",
            "stderr": "",
            "timestamp": _utc_now_iso(),
            "thread": {
                "current_ident": current_ident,
                "runner_ident": _MAIN_THREAD_IDENT,
                "runner_kind": _TICK_KIND,
                "scheduled": False,
            },
        }

    done = threading.Event()
    out = {}

    def _job():
        nonlocal out
        out = _run()
        try:
            current_ident = _safe_get_ident()
            out["thread"] = {
                "current_ident": current_ident,
                "runner_ident": _MAIN_THREAD_IDENT,
                "runner_kind": _TICK_KIND,
                "scheduled": True,
            }
        except Exception:
            pass
        done.set()

    with _MAIN_THREAD_LOCK:
        _MAIN_THREAD_QUEUE.append(_job)

    # Wait for result (avoid hanging the server thread forever)
    if not done.wait(timeout=float(timeout)):
        current_ident = _safe_get_ident()
        return {
            "ok": False,
            "status": "error",
            "error_type": "MainThreadTimeout",
            "message": "Timed out waiting for main-thread execution",
            "mode": mode,
            "stdout": "",
            "stderr": "",
            "timeout_seconds": timeout,
            "timestamp": _utc_now_iso(),
            "thread": {
                "current_ident": current_ident,
                "runner_ident": _MAIN_THREAD_IDENT,
                "runner_kind": _TICK_KIND,
                "scheduled": True,
            },
        }

    return out


def _execute_code_now(code_str, mode, timeout):
    import io
    import contextlib

    stdout = io.StringIO()
    stderr = io.StringIO()
    progress_events = []

    def report_progress(message, current=None, total=None):
        progress_events.append(
            {
                "type": "progress",
                "message": str(message),
                "current": current,
                "total": total,
                "timestamp": time.time(),
            }
        )

    g = {"unreal": unreal, "report_progress": report_progress}
    l = {}

    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            if mode == "eval":
                result = eval(code_str, g, l)
            else:
                exec(code_str, g, l)
                result = l.get("result", None)

        return {
            "ok": True,
            "status": "success",
            "mode": mode,
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "result": result,
            "progress_events": progress_events,
            "timeout_seconds": timeout,
            "timestamp": _utc_now_iso(),
        }
    except Exception as e:
        return {
            "ok": False,
            "status": "error",
            "error_type": type(e).__name__,
            "message": str(e),
            "mode": mode,
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "stack_trace": traceback.format_exc(),
            "progress_events": progress_events,
            "unreal_logs": get_recent_logs(),
            "timeout_seconds": timeout,
            "timestamp": _utc_now_iso(),
        }


def _port_is_open(host, port, timeout=0.15):
    try:
        import socket

        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _stop_server():
    global _SERVER, _SERVER_THREAD

    srv = _SERVER
    thr = _SERVER_THREAD
    _SERVER = None
    _SERVER_THREAD = None

    if srv is not None:
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            srv.server_close()
        except Exception:
            pass

    if thr is not None:
        try:
            thr.join(timeout=1.0)
        except Exception:
            pass


def _unregister_tick():
    global _TICK_HANDLE, _TICK_KIND

    if unreal is None:
        _TICK_HANDLE = None
        _TICK_KIND = None
        return

    handle = _TICK_HANDLE
    kind = _TICK_KIND
    _TICK_HANDLE = None
    _TICK_KIND = None

    if handle is None or kind is None:
        return

    try:
        if kind == "editor":
            unreg = getattr(unreal, "unregister_editor_tick_callback", None)
        elif kind == "slate_post":
            unreg = getattr(unreal, "unregister_slate_post_tick_callback", None)
        else:
            unreg = getattr(unreal, "unregister_slate_pre_tick_callback", None)
        if unreg is not None:
            unreg(handle)
    except Exception:
        pass


def _create_task_record(mode, timeout):
    task_id = uuid.uuid4().hex
    record = {
        "task_id": task_id,
        "status": "queued",
        "mode": mode,
        "timeout_seconds": timeout,
        "created_at": _utc_now_iso(),
        "started_at": None,
        "completed_at": None,
        "result": None,
    }
    with _TASKS_LOCK:
        _TASKS[task_id] = record
    return task_id


def _get_task_record(task_id):
    with _TASKS_LOCK:
        rec = _TASKS.get(task_id)
        if rec is None:
            return None
        # Return a shallow copy to avoid races in serialization.
        return dict(rec)


def exec_python_async(code, mode="exec", timeout=DEFAULT_EXEC_TIMEOUT):
    timeout = _clamp_exec_timeout(timeout)

    if unreal is None:
        return _structured_error(
            error_type="UnrealUnavailable",
            message="unreal module not available",
            status_code=503,
        )

    if code is None:
        return _structured_error(
            error_type="ValidationError",
            message="Missing required argument: code",
            status_code=400,
        )

    try:
        code_str = str(code)
    except Exception:
        code_str = code

    _ensure_main_thread_runner()
    if not _MAIN_THREAD_READY:
        return _structured_error(
            error_type="MainThreadUnavailable",
            message="Main-thread runner not available; cannot queue async Unreal execution",
            status_code=503,
        )

    task_id = _create_task_record(mode=mode, timeout=timeout)

    def _job():
        with _TASKS_LOCK:
            rec = _TASKS.get(task_id)
            if rec is None:
                return
            rec["status"] = "running"
            rec["started_at"] = _utc_now_iso()

        result = _execute_code_now(code_str=code_str, mode=mode, timeout=timeout)

        with _TASKS_LOCK:
            rec = _TASKS.get(task_id)
            if rec is None:
                return
            rec["status"] = "completed" if result.get("ok") else "failed"
            rec["result"] = result
            rec["completed_at"] = _utc_now_iso()

    with _MAIN_THREAD_LOCK:
        _MAIN_THREAD_QUEUE.append(_job)

    return {
        "status": "accepted",
        "task_id": task_id,
        "mode": mode,
        "timeout_seconds": timeout,
        "created_at": _utc_now_iso(),
    }

# The MCP Tool Definition (for discovery)
MCP_TOOLS = {
    "unreal_logs/get_logs": {
        "description": "Retrieves the most recent Unreal Engine log entries from the resolved log file. Default limit is 500 lines.",
        "function": get_logs,
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"The maximum number of log lines to return (default {RETURN_LOG_LINES}, max {LOG_LINE_LIMIT})."
                },
                "path": {
                    "type": "string",
                    "description": "Optional absolute path to a specific .log file (overrides auto-detection for this call)."
                }
            }
        }
    },
    "unreal_logs/get_log_path": {
        "description": "Returns the resolved Unreal log file path plus search locations. Supports optional path override.",
        "function": get_log_path,
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Optional absolute path to test as the log file path."
                }
            }
        }
    }
    ,
    "unreal_logs/exec": {
        "description": "Execute arbitrary Python in the running Unreal Editor process. Returns stdout/stderr/result and progress events.",
        "function": exec_python,
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python source code to execute."
                },
                "mode": {
                    "type": "string",
                    "description": "Execution mode: 'exec' (default) or 'eval'."
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Execution timeout in seconds (default {DEFAULT_EXEC_TIMEOUT}, max {MAX_EXEC_TIMEOUT})."
                }
            },
            "required": ["code"]
        }
    },
    "unreal_logs/exec_async": {
        "description": "Queue Python execution in Unreal Editor and return immediately with a task_id. Poll /tasks/{task_id}/status for completion.",
        "function": exec_python_async,
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python source code to execute asynchronously."
                },
                "mode": {
                    "type": "string",
                    "description": "Execution mode: 'exec' (default) or 'eval'."
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Execution timeout in seconds (default {DEFAULT_EXEC_TIMEOUT}, max {MAX_EXEC_TIMEOUT})."
                }
            },
            "required": ["code"]
        }
    }
}


def _tool_definitions_with_runtime_context():
    resolved, _ = _resolve_log_file_path(use_cache=True)
    tool_definitions = []
    for name, tool_data in MCP_TOOLS.items():
        desc = tool_data["description"]
        if name == "unreal_logs/get_logs" and resolved:
            desc = desc + f" (current: {resolved})"

        tool_definitions.append(
            {
                "name": name,
                "description": desc,
                "parameters": tool_data["parameters"],
            }
        )
    return tool_definitions


def get_mcp_help():
    return {
        "status": "ok",
        "timestamp": _utc_now_iso(),
        "server": {
            "name": "UnrealMCPLogForwarder",
            "host": "127.0.0.1",
            "port": MCP_PORT,
            "limits": {
                "default_log_lines": RETURN_LOG_LINES,
                "max_log_lines": LOG_LINE_LIMIT,
                "default_exec_timeout_seconds": DEFAULT_EXEC_TIMEOUT,
                "max_exec_timeout_seconds": MAX_EXEC_TIMEOUT,
            },
        },
        "endpoints": [
            {"method": "GET", "path": "/mcp", "description": "Tool discovery."},
            {"method": "GET", "path": "/mcp/help", "description": "Tool and endpoint usage documentation."},
            {"method": "GET", "path": "/health", "description": "Server health and main-thread runner status."},
            {"method": "GET", "path": "/tasks/{task_id}/status", "description": "Poll async task status and result."},
            {"method": "POST", "path": "/mcp/messages", "description": "Run a tool call with payload {tool, arguments}."},
        ],
        "examples": [
            {
                "description": "Execute Python in Unreal",
                "request": {
                    "tool": "unreal_logs/exec",
                    "arguments": {"code": "print('hello from unreal')", "timeout": 60},
                },
            },
            {
                "description": "Get last 200 log lines",
                "request": {
                    "tool": "unreal_logs/get_logs",
                    "arguments": {"limit": 200},
                },
            },
            {
                "description": "Get resolved log path",
                "request": {
                    "tool": "unreal_logs/get_log_path",
                    "arguments": {},
                },
            },
        ],
        "tools": _tool_definitions_with_runtime_context(),
    }


def get_health():
    resolved_log, _ = _resolve_log_file_path(use_cache=True)
    return {
        "status": "ok",
        "timestamp": _utc_now_iso(),
        "server": {
            "listening": _SERVER is not None,
            "thread_alive": bool(_SERVER_THREAD is not None and _SERVER_THREAD.is_alive()),
            "port": MCP_PORT,
        },
        "main_thread_runner": {
            "initialized": _MAIN_THREAD_INIT,
            "ready": _MAIN_THREAD_READY,
            "runner_ident": _MAIN_THREAD_IDENT,
            "runner_kind": _TICK_KIND,
            "queued_jobs": len(_MAIN_THREAD_QUEUE),
        },
        "log_resolution": {
            "project": _get_project_name(),
            "resolved_log": resolved_log,
        },
    }


# --- MCP HTTP Server Implementation ---

class MCPHandler(http.server.BaseHTTPRequestHandler):
    """Handles HTTP requests for the Model Context Protocol (MCP)."""
    
    # Disable logging to prevent infinite log loop inside Unreal
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        """Handle tool discovery and diagnostics endpoints."""
        if self.path == '/mcp':
            self._send_json(200, {"tools": _tool_definitions_with_runtime_context()})
        elif self.path == '/mcp/help':
            self._send_json(200, get_mcp_help())
        elif self.path == '/health':
            self._send_json(200, get_health())
        elif self.path.startswith('/tasks/') and self.path.endswith('/status'):
            parts = self.path.strip('/').split('/')
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "status":
                task_id = parts[1]
                rec = _get_task_record(task_id)
                if rec is None:
                    self._send_error_json(404, "TaskNotFound", f"Task not found: {task_id}")
                else:
                    self._send_json(200, rec)
            else:
                self._send_error_json(404, "NotFound", f"Path not found: {self.path}")
        else:
            self._send_error_json(404, "NotFound", f"Path not found: {self.path}")

    def do_POST(self):
        """Handle tool call request (POST /mcp/messages)."""
        if self.path == '/mcp/messages':
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                if content_length <= 0:
                    self._send_error_json(400, "ValidationError", "Missing request body")
                    return

                post_data = self.rfile.read(content_length)
                payload = json.loads(post_data.decode('utf-8'))
                
                tool_name = payload.get("tool")
                arguments = payload.get("arguments", {})
                if not isinstance(arguments, dict):
                    self._send_error_json(400, "ValidationError", "Field 'arguments' must be an object")
                    return
                
                tool_data = MCP_TOOLS.get(tool_name)
                if tool_data and tool_data["function"]:
                    # Call the function with arguments
                    try:
                        import inspect
                        func_params = inspect.signature(tool_data["function"]).parameters
                        filtered_arguments = {k: v for k, v in arguments.items() if k in func_params}
                        
                        result = tool_data["function"](**filtered_arguments)
                    except (TypeError, AttributeError):
                        # Fallback for older Python versions or inspect issues
                        result = tool_data["function"](**arguments)

                    status_code = 200
                    if isinstance(result, dict):
                        status_code = int(result.pop("_status_code", 200))

                    self._send_json(status_code, {"result": result})
                else:
                    self._send_error_json(400, "ToolNotFound", f"Tool not found or invalid: {tool_name}")

            except json.JSONDecodeError as e:
                self._send_error_json(400, "InvalidJson", f"Invalid JSON payload: {e}")
            except Exception as e:
                _log_error(f"MCP Server error during POST: {e}")
                self._send_error_json(500, "ServerError", str(e), details={"stack_trace": traceback.format_exc()})
        else:
            self._send_error_json(404, "NotFound", f"Path not found: {self.path}")

    def _send_json(self, status_code, payload):
        self.send_response(int(status_code))
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode('utf-8'))

    def _send_error_json(self, status_code, error_type, message, details=None):
        payload = _structured_error(error_type, message, status_code=status_code, details=details)
        payload.pop("_status_code", None)
        self._send_json(status_code, payload)

    def _send_400(self, message):
        self._send_error_json(400, "BadRequest", message)
        
    def _send_404(self):
        self._send_error_json(404, "NotFound", "Path not found")

    def _send_500(self, message):
        self._send_error_json(500, "ServerError", message)


# Helper to run server in its own thread
def start_mcp_server():
    """Starts the MCP HTTP server in a thread."""
    global _SERVER
    try:
        # Use a non-default thread class that is properly daemonized
        class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            pass

        ThreadingHTTPServer.daemon_threads = True
        ThreadingHTTPServer.allow_reuse_address = True

        # We bind to 0.0.0.0 to listen on all interfaces
        server = ThreadingHTTPServer(("127.0.0.1", MCP_PORT), MCPHandler)
        _SERVER = server
        _log_info(f"Starting MCP Server (File Reader) on port {MCP_PORT}...")
        server.serve_forever()
    except Exception as e:
        _log_error(f"Failed to start MCP Server (Port {MCP_PORT} in use?): {e}")

# Start the server in a separate daemon thread
if os.getenv("UNREAL_MCP_DISABLE_SERVER") != "1":
    # On module reload, stop any existing server first.
    _stop_server()
    _unregister_tick()

    _SERVER_THREAD = threading.Thread(target=start_mcp_server, daemon=True)
    _SERVER_THREAD.start()
    _log_info(f"MCP Log Forwarder (Server Thread) started on port {MCP_PORT}. Access via http://localhost:{MCP_PORT}")

# Ensure main-thread runner is registered as early as possible.
# init_unreal.py runs on editor startup (main thread), so this should succeed.
try:
    _ensure_main_thread_runner()
except Exception:
    pass
