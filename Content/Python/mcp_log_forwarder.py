import unreal
import threading
import json
import http.server
import socketserver
from typing import List

# --- Configuration ---
MCP_PORT = 3001
LOG_LINE_LIMIT = 5000  # Max lines to store in memory
RETURN_LOG_LINES = 500 # Default lines to return per tool call

# --- Shared Log Buffer ---
_log_buffer: List[str] = []
_buffer_lock = threading.Lock()

# --- Unreal Log Capture ---

class UnrealLogCapturer(unreal.OutputDevice):
    """Custom Unreal output device that captures log lines."""
    def serialize(self, message: str, verbosity: unreal.LogVerbosity, category: unreal.Name):
        # We must acquire the lock before doing anything with the shared buffer
        with _buffer_lock:
            # Format and append log
            formatted = f"[{category.text}] {verbosity.name}: {message}"
            _log_buffer.append(formatted)
            
            # Enforce buffer size limit by keeping only the last LOG_LINE_LIMIT lines
            if len(_log_buffer) > LOG_LINE_LIMIT:
                _log_buffer[:] = _log_buffer[-LOG_LINE_LIMIT:]

# Register device when the script runs
_capturer = UnrealLogCapturer()
unreal.register_output_device(_capturer)
unreal.log(f"MCP Log Forwarder (UnrealLogCapturer) loaded. Max buffer size: {LOG_LINE_LIMIT} lines.")


# --- MCP Tool Implementation ---

def get_logs(limit: int = RETURN_LOG_LINES) -> List[str]:
    """Retrieves the most recent Unreal Engine log entries."""
    # Ensure limit is an integer and within the safe bounds
    try:
        limit = int(limit)
    except (ValueError, TypeError):
        limit = RETURN_LOG_LINES
        
    limit = max(1, min(limit, LOG_LINE_LIMIT))
    
    with _buffer_lock:
        # Return the last 'limit' lines
        return _log_buffer[-limit:]

# The MCP Tool Definition (for discovery)
MCP_TOOLS = {
    "unreal_logs/get_logs": {
        "description": "Retrieves the most recent Unreal Engine log entries. Default limit is 500 lines.",
        "function": get_logs,
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "The maximum number of log lines to return (default 500, max 5000)."
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
                    # Filter out arguments not expected by the function to prevent TypeError
                    import inspect
                    func_params = inspect.signature(tool_data["function"]).parameters
                    filtered_arguments = {k: v for k, v in arguments.items() if k in func_params}
                    
                    result = tool_data["function"](**filtered_arguments)
                    
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

        # We bind to 0.0.0.0 to listen on all interfaces, but it's only for localhost access
        server = ThreadingHTTPServer(("0.0.0.0", MCP_PORT), MCPHandler)
        unreal.log(f"Starting MCP Server on port {MCP_PORT}...")
        server.serve_forever()
    except Exception as e:
        unreal.log_error(f"Failed to start MCP Server (Port {MCP_PORT} in use?): {e}")

# Start the server in a separate daemon thread
_server_thread = threading.Thread(target=start_mcp_server, daemon=True)
_server_thread.start()

unreal.log(f"MCP Log Forwarder (Server Thread) started on port {MCP_PORT}. Access via http://localhost:{MCP_PORT}")