"""Anycast service on a PoP: answers HTTP GET on 198.51.100.1:80 with the PoP name."""
import http.server
import sys

name = sys.argv[1].encode() + b"\n"


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(name)))
        self.end_headers()
        self.wfile.write(name)

    def log_message(self, *a):
        pass


http.server.HTTPServer(("198.51.100.1", 80), H).serve_forever()
