"""生图服务健康检查：面板靠它决定那个勾能不能点。

跑法：python tests/test_imagegen.py
"""

import http.server
import socketserver
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from somnia import imagegen


class _OK(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def _serve():
    srv = socketserver.TCPServer(("127.0.0.1", 0), _OK)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_health_false_when_nothing_listens():
    # 关着的端口：立刻返回 False，不能抛异常
    assert imagegen.health("http://127.0.0.1:1/v1/images/generations", timeout=1) is False


def test_health_true_when_service_up():
    srv = _serve()
    port = srv.server_address[1]
    try:
        assert imagegen.health(f"http://127.0.0.1:{port}/v1/images/generations",
                               timeout=2) is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_health_strips_v1_suffix():
    """health 打的是 /health，不是 /v1/images/generations。"""
    srv = _serve()
    port = srv.server_address[1]
    try:
        url = f"http://127.0.0.1:{port}/v1/images/generations"
        assert imagegen.health(url, timeout=2)
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    passed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            passed += 1
            print(f"  ok  {name}")
    print(f"\n{passed} passed")
