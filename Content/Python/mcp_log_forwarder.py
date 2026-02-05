import os
import json
import threading
import http.server
import socketserver
import glob

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

_CACHED_LOG_PATH = None
_CACHED_SEARCH = None

_MAIN_THREAD_QUEUE = []
_MAIN_THREAD_LOCK = threading.Lock()
_MAIN_THREAD_INIT = False
_MAIN_THREAD_READY = False

_TICK_HANDLE = None
_TICK_KIND = None  # "post" or "pre"

_SERVER = None
_SERVER_THREAD = None


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


def _ensure_main_thread_runner():
    """Register a tick callback to run queued work on the editor main thread."""
    global _MAIN_THREAD_INIT, _MAIN_THREAD_READY
    global _TICK_HANDLE, _TICK_KIND

    if _MAIN_THREAD_INIT:
        return

    if unreal is None:
        _MAIN_THREAD_INIT = True
        _MAIN_THREAD_READY = False
        return

    # Many Unreal Python builds expose register_slate_post_tick_callback.
    register = getattr(unreal, "register_slate_post_tick_callback", None)
    if register is None:
        register = getattr(unreal, "register_slate_pre_tick_callback", None)

    if register is None:
        _log_error("No Slate tick callback registration found; cannot run exec on main thread.")
        _MAIN_THREAD_INIT = True
        _MAIN_THREAD_READY = False
        return

    def _tick(_delta_time):
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
        _TICK_HANDLE = register(_tick)
        _TICK_KIND = "post" if getattr(unreal, "register_slate_post_tick_callback", None) is register else "pre"
        _MAIN_THREAD_INIT = True
        _MAIN_THREAD_READY = True
        _log_info("Main-thread runner registered via Slate tick callback")
    except Exception as e:
        _log_error(f"Failed to register main-thread runner: {e}")
        _MAIN_THREAD_INIT = True
        _MAIN_THREAD_READY = False


def _get_project_name():
    if unreal is not None:
        try:
            name = unreal.SystemLibrary.get_project_name()
            if name:
                return str(name)
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
                logs_dir = os.path.join(str(saved_dir), "Logs")
                searched.append(os.path.normpath(logs_dir))
                if os.path.isdir(logs_dir):
                    logs = glob.glob(os.path.join(logs_dir, "*.log"))
                    picked = _pick_newest_log(logs, project_name=project_name)
                    if picked:
                        _CACHED_LOG_PATH, _CACHED_SEARCH = picked, searched
                        return picked, searched
        except Exception:
            pass

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


def exec_python(code, mode="exec"):
    """Execute Python inside Unreal and return output.

    Parameters:
    - code: python source code
    - mode: "exec" (default) or "eval"
    """

    if unreal is None:
        return {
            "ok": False,
            "error": "unreal module not available",
        }

    if code is None:
        return {
            "ok": False,
            "error": "Missing required argument: code",
        }

    try:
        code_str = str(code)
    except Exception:
        code_str = code

    import io
    import contextlib

    def _run():
        stdout = io.StringIO()
        stderr = io.StringIO()
        g = {"unreal": unreal}
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
                "mode": mode,
                "stdout": stdout.getvalue(),
                "stderr": stderr.getvalue(),
                "result": result,
            }
        except Exception as e:
            import traceback
            return {
                "ok": False,
                "mode": mode,
                "stdout": stdout.getvalue(),
                "stderr": stderr.getvalue(),
                "error": str(e),
                "traceback": traceback.format_exc(),
            }

    # Unreal editor APIs generally must run on the main thread.
    _ensure_main_thread_runner()

    # If we cannot schedule, fail fast. Running Unreal editor APIs from this
    # request thread will often throw "outside the main game thread".
    if unreal is None or not _MAIN_THREAD_READY:
        return {
            "ok": False,
            "mode": mode,
            "stdout": "",
            "stderr": "",
            "error": "Main-thread runner not available; cannot execute Unreal editor APIs from MCP request thread",
        }

    done = threading.Event()
    out = {}

    def _job():
        nonlocal out
        out = _run()
        done.set()

    with _MAIN_THREAD_LOCK:
        _MAIN_THREAD_QUEUE.append(_job)

    # Wait for result (avoid hanging the server thread forever)
    if not done.wait(timeout=5.0):
        return {
            "ok": False,
            "mode": mode,
            "stdout": "",
            "stderr": "",
            "error": "Timed out waiting for main-thread execution",
        }

    return out


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
        if kind == "post":
            unreg = getattr(unreal, "unregister_slate_post_tick_callback", None)
        else:
            unreg = getattr(unreal, "unregister_slate_pre_tick_callback", None)
        if unreg is not None:
            unreg(handle)
    except Exception:
        pass

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
        "description": "Execute arbitrary Python in the running Unreal Editor process. Returns stdout/stderr/result.",
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
                }
            },
            "required": ["code"]
        }
    }
}


# --- MCP HTTP Server Implementation ---

class MCPHandler(http.server.BaseHTTPRequestHandler):
    """Handles HTTP requests for the Model Context Protocol (MCP)."""
    
    # Disable logging to prevent infinite log loop inside Unreal
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        """Handle tool discovery request (GET /mcp)."""
        if self.path == '/mcp':
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            
            resolved, _ = _resolve_log_file_path(use_cache=True)

            tool_definitions = []
            for name, tool_data in MCP_TOOLS.items():
                desc = tool_data["description"]
                if name == "unreal_logs/get_logs" and resolved:
                    desc = desc + f" (current: {resolved})"

                tool_definitions.append({
                    "name": name,
                    "description": desc,
                    "parameters": tool_data["parameters"],
                })
                
            response = {"tools": tool_definitions}
            self.wfile.write(json.dumps(response).encode('utf-8'))
        else:
            self._send_404()

    def do_POST(self):
        """Handle tool call request (POST /mcp/messages)."""
        if self.path == '/mcp/messages':
            try:
                content_length = int(self.headers['Content-Length'])
                post_data = self.rfile.read(content_length)
                payload = json.loads(post_data.decode('utf-8'))
                
                tool_name = payload.get("tool")
                arguments = payload.get("arguments", {})
                
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
                    
                    self.send_response(200)
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"result": result}).encode('utf-8'))
                else:
                    self._send_400(f"Tool not found or invalid: {tool_name}")
            
            except Exception as e:
                _log_error(f"MCP Server error during POST: {e}")
                self._send_500(str(e))
        else:
            self._send_404()

    def _send_400(self, message):
        self.send_response(400)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": message}).encode('utf-8'))
        
    def _send_404(self):
        self.send_response(404)
        self.end_headers()

    def _send_500(self, message):
        self.send_response(500)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": message}).encode('utf-8'))


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
