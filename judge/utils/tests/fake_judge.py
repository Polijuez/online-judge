"""Juez falso para los tests de propagación.

La API de control real del juez escucha en loopback y responde ``POST
/update/problems`` con 200. Estos helpers levantan esa API en un puerto
efímero, así los tests nunca tocan los puertos de los jueces de verdad.
"""

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONTROL_PATH = '/update/problems'


def closed_port_endpoint():
    """URL de un puerto de loopback donde nadie escucha: un juez caído."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    return 'http://127.0.0.1:%d%s' % (port, CONTROL_PATH)


class FakeJudgeControlServer:
    """API de control mínima que registra los avisos recibidos.

    ``delay`` simula un juez colgado que tarda más que el timeout; ``status``
    permite simular una respuesta de error.
    """

    def __init__(self, delay=0.0, status=200):
        self.delay = delay
        self.status = status
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                if length:
                    self.rfile.read(length)
                server.requests.append(self.path)
                if server.delay:
                    time.sleep(server.delay)
                self.send_response(server.status)
                self.end_headers()
                try:
                    self.wfile.write(b'As you wish.')
                except (BrokenPipeError, ConnectionResetError):
                    # El cliente ya se cansó de esperar (test de timeout).
                    pass

            def log_message(self, format, *args):
                pass

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        host, port = self.httpd.server_address[:2]
        return 'http://%s:%d%s' % (host, port, CONTROL_PATH)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
