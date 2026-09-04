#!/usr/bin/env python3
"""Loopback synthetic site for hosted BWM-796 acceptance. No real accounts."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>BWM-796 Synthetic</title>
<style>
body{font-family:sans-serif;margin:0}
.pad{height:1400px;background:linear-gradient(#eef,#ccd)}
#hero{margin:24px;padding:24px;border:2px solid #333}
button,input{font-size:18px;padding:10px}
#click-target{width:200px;height:48px}
</style></head>
<body>
<div id="hero">
  <h1>Synthetic Agent Computer</h1>
  <p id="status">ready</p>
  <button id="click-target" onclick="mark('owner-pixel-clicked')">Pixel target</button>
  <button id="agent-ready" onclick="mark('agent-ready')">Agent ready</button>
  <p><input id="text-input" placeholder="type here"/></p>
  <p><input id="file-input" type="file" onchange="document.getElementById('uploaded').textContent='received:'+this.files[0].name"/></p>
  <p id="uploaded">no-file</p>
  <p><a id="download" href="/artifact.bin" download="bwm796-artifact.txt">Download artifact</a></p>
  <button id="checkpoint" onclick="document.getElementById('gate').hidden=false;mark('2FA required')">Consequential submit</button>
  <div id="gate" hidden>
    <h2>2FA required</h2>
    <p>Owner must take control and enter the code.</p>
    <input id="otp" placeholder="000000"/>
    <button id="finish" onclick="mark('submitted-once')">Submit once</button>
  </div>
</div>
<div class="pad">scrollable region</div>
<script>
function mark(t){document.getElementById('status').textContent=t;document.cookie='synth=1;path=/;max-age=86400';try{localStorage.setItem('bwm796','persist')}catch(e){}}
document.getElementById('text-input').addEventListener('input',function(){document.getElementById('status').textContent=this.value});
(function(){
  var bits=[];
  if(document.cookie.indexOf('synth=1')>=0) bits.push('cookie');
  try{if(localStorage.getItem('bwm796')==='persist') bits.push('persist');}catch(e){}
  if(bits.length) document.getElementById('status').textContent+=' '+bits.join(' ');
})();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        if self.path.startswith("/artifact.bin"):
            body = b"BWM-796 synthetic artifact\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Disposition", "attachment; filename=bwm796-artifact.txt")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "synth=1; Path=/; Max-Age=86400")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        _ = self.rfile.read(length)
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"SYNTHETIC http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
