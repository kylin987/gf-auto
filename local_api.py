import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from loguru import logger


class _LocalApiHandler(BaseHTTPRequestHandler):
    server_version = 'XianyuLocalApi/1.0'

    def do_GET(self):
        if urlparse(self.path).path == '/health':
            self._send_json({
                'status': 'ok',
                'ws_connected': self.server.live.ws is not None,
            })
            return
        self._send_json({'error': 'not found'}, status=404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ('/api/reply', '/api/send', '/api/adjust_price', '/api/order_detail', '/api/consign_dummy', '/api/cancel_order'):
            self._send_json({'error': 'not found'}, status=404)
            return
        payload = self._read_json()
        if payload is None:
            return
        try:
            if path == '/api/adjust_price':
                result = {'ok': True, 'result': self.server.live.adjust_order_price(payload)}
            elif path == '/api/order_detail':
                result = {'ok': True, 'order': self.server.live.get_order_detail(payload)}
            elif path == '/api/consign_dummy':
                result = {'ok': True, 'result': self.server.live.consign_dummy_order(payload)}
            elif path == '/api/cancel_order':
                result = {'ok': True, 'result': self.server.live.cancel_order(payload)}
            else:
                result = self.server.live.send_reply(payload)
        except ValueError as exc:
            self._send_json({'error': str(exc)}, status=400)
            return
        except LookupError as exc:
            self._send_json({'error': str(exc)}, status=404)
            return
        except Exception as exc:
            logger.exception('send reply failed')
            self._send_json({'error': str(exc)}, status=500)
            return
        self._send_json(result)

    def _read_json(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b''
        if not raw:
            self._send_json({'error': 'empty request body'}, status=400)
            return None
        try:
            return json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({'error': 'invalid JSON body'}, status=400)
            return None

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.debug(fmt % args)


class LocalApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, live):
        self.live = live
        super().__init__(addr, _LocalApiHandler)
