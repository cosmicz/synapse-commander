"""Small authenticated human-order surface, separate from the game gateway."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit


class ControlServer:
    def __init__(self, controller, token, *, host='127.0.0.1', port=8090):
        if host != '127.0.0.1' or not isinstance(token, str) or len(token) < 32:
            raise ValueError('Use a loopback listener and a private token of at least 32 characters')
        page = Path(__file__).with_name('control.html').read_bytes()
        slots = threading.BoundedSemaphore(8)

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def process_request(self, request, client_address):
                if not slots.acquire(blocking=False):
                    self.shutdown_request(request)
                    return
                try:
                    super().process_request(request, client_address)
                except BaseException:
                    slots.release()
                    raise

            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    slots.release()

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(3)

            def log_message(self, *args):
                pass

            def respond(self, code, data, content_type='application/json'):
                body = data if isinstance(data, bytes) else json.dumps(data).encode()
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)

            def authenticated(self):
                origin = self.headers.get('Origin')
                if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                    self.respond(403, {'error': 'cross_origin'})
                    return False
                supplied = self.headers.get('Authorization', '')
                if not hmac.compare_digest(supplied, 'Bearer ' + token):
                    self.respond(401, {'error': 'unlock_required'})
                    return False
                return True

            def do_GET(self):
                if self.path == '/':
                    self.respond(200, page, 'text/html; charset=utf-8')
                elif self.path == '/status' and self.authenticated():
                    self.respond(200, controller.snapshot())
                elif self.path != '/status':
                    self.respond(404, {'error': 'not_found'})

            def do_POST(self):
                if not self.authenticated():
                    return
                if self.path not in ('/orders', '/pause'):
                    self.respond(404, {'error': 'not_found'})
                    return
                try:
                    if self.headers.get('Content-Type') != 'application/json' or self.headers.get('Transfer-Encoding'):
                        raise ValueError('JSON required')
                    size = int(self.headers.get('Content-Length', '-1'))
                    if not 0 < size <= 8192:
                        raise ValueError('bounded body required')
                    data = json.loads(self.rfile.read(size))
                    if not isinstance(data, dict):
                        raise ValueError('object required')
                    result = controller.accept_order(data.get('text')) if self.path == '/orders' else controller.pause()
                    self.respond(202, result)
                except (ValueError, UnicodeError):
                    self.respond(400, {'error': 'invalid_order'})

        self.httpd = Server((host, port), Handler)
        self.server_port = self.httpd.server_port
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, name='human-control', daemon=True)
        self._thread.start()
        return self

    def close(self):
        if self._thread:
            self.httpd.shutdown()
            self._thread.join(timeout=5)
        self.httpd.server_close()
