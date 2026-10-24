# -*- coding: utf-8 -*-
"""auth_server：快爆网页授权服务器（bot 内嵌版）

移植自 paradigm-reboot-data/tools/hykb_web_login.py，改造为多用户服务模式：
- /?t=<token>            授权首页（含快爆登录 iframe）
- /proxy-login           反代快爆登录页（注入 HykbJsInterface 捕获脚本）
- /login.php|/idcard|... 登录 ajax 反代
- /capture/<token>       credential 捕获（写入 self.captured[token]）
- /state/<token>         bot 轮询：pending/done（取走即焚）

由 bot 在 /范式绑定 时动态启动（端口 18964），空闲自动关闭。
"""
import http.server
import json
import threading
import urllib.parse
import urllib.request
from pathlib import Path

GAME_ID = "24931"
UPSTREAM = "https://www.3839.com"

INJECT_SNIPPET = r'''<script>
(function() {
  window.HykbJsInterface = {
    onLoginCallback: function(result, code, msg) {
      try {
        var payload = JSON.parse(result);
        window.parent.postMessage({type: 'hykb_login_result', payload: payload}, '*');
      } catch(e) {
        window.parent.postMessage({type: 'hykb_login_result', payload: {raw: String(result)}}, '*');
      }
    }
  };
  window.webkit = {
    messageHandlers: {
      onLoginCallback: {
        postMessage: function(str) {
          try {
            window.parent.postMessage({type: 'hykb_login_result', payload: JSON.parse(str)}, '*');
          } catch(e) {}
        }
      }
    }
  };
})();
</script>'''

PAGE = '''<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>范式起源 · 快爆授权</title>
<style>
body{font-family:system-ui,sans-serif;background:#12141c;color:#e8ecf4;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;box-sizing:border-box}
.card{background:#262c3c;border-radius:12px;padding:28px;max-width:460px;width:100%;box-sizing:border-box}
h2{margin:0 0 12px;color:#6ca6ff}
.info{color:#969eb0;font-size:14px;line-height:1.7}
.ok{color:#4ade80;font-size:1.2em;font-weight:bold}
iframe{width:100%;height:70vh;border:0;border-radius:8px;margin-top:12px}
</style></head><body><div class="card">
<div id="status">
<h2>好游快爆授权登录</h2>
<p class="info">即将打开快爆登录页，请用<b>手机号 + 短信验证码</b>登录。</p>
<p class="info">登录成功后本页面自动完成，无需其他操作。全程不接触你的密码。</p>
</div>
<div id="frame-wrap" style="display:none"><iframe id="loginFrame" src="/proxy-login"></iframe></div>
</div>
<script>
window.addEventListener('message', function(e) {
  if (e.data && e.data.type === 'hykb_login_result') {
    fetch('/capture/%TOKEN%', { method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(e.data.payload) })
      .then(function(r){ return r.json(); })
      .then(function(){
        document.getElementById('status').innerHTML =
          '<h2 class="ok">✓ 授权成功！</h2><p class="info">回到 QQ 里查看 bot 的确认消息即可。</p>';
        document.getElementById('frame-wrap').style.display = 'none';
      })
      .catch(function(){
        document.getElementById('status').innerHTML =
          '<h2 style="color:#f87171">上传失败</h2><p class="info">请回 QQ 重新发起 /范式绑定</p>';
      });
  }
});
document.getElementById('frame-wrap').style.display = 'block';
</script></body></html>'''


class AuthState:
    """授权状态容器（bot 持有）"""

    def __init__(self):
        self.captured: dict[str, dict] = {}  # token -> payload（取走即焚）
        self.tokens: set[str] = set()        # 有效令牌
        self.last_activity = 0.0
        self.server = None
        self.thread = None


def _make_handler(state: AuthState):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # 静默（bot 日志由上层记）

        def _send(self, code, body: bytes, ct="text/html; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _proxy(self, method):
            path = self.path
            if path.startswith("/login.php") or path.startswith("/idcard"):
                path = "/sdk" + path
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else None
            fwd = {}
            for k in ("Content-Type", "X-Requested-With", "Referer", "Cookie", "Origin"):
                v = self.headers.get(k)
                if v:
                    fwd[k] = v
            fwd["User-Agent"] = "Mozilla/5.0 (Linux; Android 12; Mobile) AppleWebKit/537.36"
            fwd["Referer"] = f"{UPSTREAM}/sdk/login.php?gid={GAME_ID}&from=android"
            req = urllib.request.Request(UPSTREAM + path, data=body, headers=fwd, method=method)
            try:
                resp = urllib.request.urlopen(req, timeout=20)
                status, rbody = resp.status, resp.read()
                rct = resp.headers.get("Content-Type", "text/html")
                cookies = resp.headers.get_all("Set-Cookie") or []
            except urllib.error.HTTPError as e:
                status, rbody = e.code, e.read()
                rct = e.headers.get("Content-Type", "text/html")
                cookies = e.headers.get_all("Set-Cookie") or []
            if "text/html" in (rct or "") and b"HykbJsInterface" in rbody:
                rbody = rbody.replace(b"<head>", b"<head>" + INJECT_SNIPPET.encode(), 1)
            extra = {}
            for ck in set(cookies):
                extra["Set-Cookie"] = ck  # 单值即可（多 cookie 场景罕见于登录页）
            self._send(status, rbody, rct or "text/html; charset=utf-8", extra)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/":
                token = (qs.get("t") or [""])[0]
                if token not in state.tokens:
                    self._send(410, "❌ 授权链接无效或已过期，请回 QQ 重新 /范式绑定".encode())
                    return
                state.last_activity = __import__("time").time()
                self._send(200, PAGE.replace("%TOKEN%", token).encode())
            elif parsed.path == "/proxy-login":
                self.path = f"/sdk/login.php?gid={GAME_ID}&from=android"
                self._proxy("GET")
            else:
                self._proxy("GET")

        def do_POST(self):
            if self.path.startswith("/capture/"):
                token = self.path.rsplit("/", 1)[-1]
                if token not in state.tokens:
                    self._send(410, b'{"error":"expired"}', "text/json")
                    return
                length = int(self.headers.get("Content-Length", 0) or 0)
                try:
                    payload = json.loads(self.rfile.read(length))
                    if not (payload.get("userId") or payload.get("uid")):
                        raise ValueError("no uid")
                    state.captured[token] = payload
                    self._send(200, b'{"ok":true}', "text/json")
                except Exception:
                    self._send(400, b'{"error":"bad payload"}', "text/json")
            else:
                self._proxy("POST")

    return Handler


class AuthServer:
    """授权服务器：start(port) 起，stop() 停；captured 由 bot 轮询"""

    def __init__(self, port: int = 18964):
        self.port = port
        self.state = AuthState()
        self.httpd = None

    def start(self):
        import socket

        class DualStackServer(http.server.ThreadingHTTPServer):
            address_family = socket.AF_INET6  # IPv6 套接字
            allow_reuse_address = True

            def server_bind(self_):
                # 双栈：IPv6 套接字同时接受 IPv4 映射连接（v4-mapped）
                try:
                    self_.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, False)
                except OSError:
                    pass
                super().server_bind()

        self.httpd = DualStackServer(("::", self.port), _make_handler(self.state))
        th = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        th.start()
        return th

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def new_token(self, token: str):
        self.state.tokens.add(token)

    def poll(self, token: str) -> dict:
        """pending / {status: done, payload}；取走即焚"""
        if token in self.state.captured:
            payload = self.state.captured.pop(token)
            self.state.tokens.discard(token)
            return {"status": "done", "payload": payload}
        if token in self.state.tokens:
            return {"status": "pending"}
        return {"status": "expired"}

    @property
    def base_url(self) -> str:
        """当前公网 IPv6 授权链接基址"""
        import socket

        ips = []
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6):
                ip = info[4][0]
                if not ip.startswith("fe80") and ip != "::1" and ip not in ips:
                    ips.append(ip)
        except OSError:
            pass
        ip = ips[0] if ips else "::1"
        return f"http://[{ip}]:{self.port}"
