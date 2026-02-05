import unreal
import threading
import json
import http.server
import socketserver
import os

# --- Configuration ---
MCP_PORT = 3001
LOG_FILE_PATH = "C:\\Users\\hannes\\AppData\\Local\\UnrealEngine\\5.6\\Saved\\Logs\\Test.log"
RETURN_LOG_LINES = 500  # Default lines to return per tool call
LOG_LINE_LIMIT = 5000  # Safety cap on returned lines

# --- Log Tailing Utility ---

def tail_log_file(filename, n=RETURN_LOG_LINES):
    """Return last n lines from filename.

    Implementation reads chunks from EOF backwards to avoid loading the whole file.
    """
    try:
        # Check if the file exists before attempting to open
        if not os.path.exists(filename):
            unreal.log_error(f"Log file not found at: {filename}")
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
        unreal.log_error(f"Error reading log file: {e}")
        return [f"ERROR: Could not read log file: {e}"]


# --- MCP Tool Implementation ---

def get_logs(limit=RETURN_LOG_LINES):
    """Retrieves the most recent Unreal Engine log entries from the file."""
    # Ensure limit is an integer and within the safe bounds
    try:
        limit = int(limit)
    except (ValueError, TypeError):
        limit = RETURN_LOG_LINES
        
    limit = max(1, min(limit, LOG_LINE_LIMIT))
    
    return tail_log_file(LOG_FILE_PATH, limit)

# The MCP Tool Definition (for discovery)
MCP_TOOLS = {
    "unreal_logs/get_logs": {
        "description": f"Retrieves the most recent Unreal Engine log entries from {LOG_FILE_PATH}. Default limit is 500 lines.",
        "function": get_logs,
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"The maximum number of log lines to return (default {RETURN_LOG_LINES}, max {LOG_LINE_LIMIT})."
                }
            }
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
            
            tool_definitions = []
            for name, tool_data in MCP_TOOLS.items():
                tool_definitions.append({
                    "name": name,
                    "description": tool_data["description"],
                    "parameters": tool_data["parameters"]
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
                unreal.log_error(f"MCP Server error during POST: {e}")
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
    try:
        # Use a non-default thread class that is properly daemonized
        class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            pass

        ThreadingHTTPServer.daemon_threads = True
        ThreadingHTTPServer.allow_reuse_address = True

        # We bind to 0.0.0.0 to listen on all interfaces
        server = ThreadingHTTPServer(("127.0.0.1", MCP_PORT), MCPHandler)
        unreal.log(f"Starting MCP Server (File Reader) on port {MCP_PORT}...")
        server.serve_forever()
    except Exception as e:
        unreal.log_error(f"Failed to start MCP Server (Port {MCP_PORT} in use?): {e}")

# Start the server in a separate daemon thread
_server_thread = threading.Thread(target=start_mcp_server, daemon=True)
_server_thread.start()

unreal.log(f"MCP Log Forwarder (Server Thread) started on port {MCP_PORT}. Access via http://localhost:{MCP_PORT}")
