"""Local-only animated page used by the supervised wired-display checkpoint."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import TracebackType


DEMO_HTML = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HCCAST wired motion demo</title>
<style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#05070b;color:#fff;
font-family:system-ui,sans-serif}main{height:100%;display:grid;grid-template-rows:auto auto 1fr auto;
gap:18px;padding:28px;box-sizing:border-box}.label{font-size:24px;letter-spacing:.12em;color:#83f8ff}
#clock{font:700 70px/1 ui-monospace,monospace}#counter{font:600 34px/1 ui-monospace,monospace}
#track{position:relative;border:5px solid #fff;background:linear-gradient(90deg,#080808 50%,#eee 50%)}
#sweep{position:absolute;top:0;bottom:0;width:24px;background:#ff2d55;box-shadow:0 0 24px #ff2d55}
#contrast{position:absolute;left:0;right:0;bottom:0;height:32%;mix-blend-mode:difference;background:#fff}
a{display:block;padding:20px;text-align:center;background:#18a957;color:#fff;font-size:30px;font-weight:700;
text-decoration:none;border:4px solid #fff}.inverse{filter:invert(1)}
</style></head><body><main><div><div class="label">SOURCE CLOCK</div><div id="clock">--:--:--.---</div></div>
<div id="counter">UPDATE 000000</div><div id="track"><div id="sweep"></div><div id="contrast"></div></div>
<a href="https://www.youtube.com/results?search_query=Big+Buck+Bunny">OPEN YOUTUBE SEARCH</a></main>
<script>(()=>{const clock=document.querySelector('#clock'),counter=document.querySelector('#counter'),
sweep=document.querySelector('#sweep'),contrast=document.querySelector('#contrast');let frame=0,last=-1;
function tick(now){frame++;const d=new Date();clock.textContent=d.toLocaleTimeString('en-US',{hour12:false})+'.'+
String(d.getMilliseconds()).padStart(3,'0');counter.textContent='UPDATE '+String(frame).padStart(6,'0');
const phase=(now%4000)/2000,pos=phase<=1?phase:2-phase;sweep.style.left=`calc(${(pos*100).toFixed(2)}% - 12px)`;
const second=Math.floor(now/1000);if(second!==last){last=second;contrast.classList.toggle('inverse')}
requestAnimationFrame(tick)}requestAnimationFrame(tick)})();</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(DEMO_HTML)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(DEMO_HTML)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class DemoPageServer:
    """Own one HTTP server bound only to the Jetson loopback interface."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8877) -> None:
        if host != "127.0.0.1":
            raise ValueError("demo server host is fixed at 127.0.0.1")
        self._server = _Server((host, port), _Handler)
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}/"

    def start(self) -> DemoPageServer:
        if self._closed:
            raise RuntimeError("demo HTTP server is closed")
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="hccast-demo-http",
        )
        self._thread.start()
        return self

    def close(self) -> None:
        if self._closed:
            return
        thread = self._thread
        if thread is not None:
            self._server.shutdown()
        self._server.server_close()
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                raise RuntimeError("demo HTTP server did not stop")
        self._thread = None
        self._closed = True

    def poll_failure(self) -> str | None:
        if self._thread is None or not self._thread.is_alive():
            return "demo-server-exited"
        return None

    def __enter__(self) -> DemoPageServer:
        return self.start()

    def __exit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()
