#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 CDP 驱动本机 Chrome（Playwright 附带的那份）操作 Coboπ 网页。

为什么不用 DSH 的 browser_* 工具：它只探测 `/usr/bin/google-chrome` 等固定路径，
而这台机器上的 Chrome 在 `~/.cache/ms-playwright/chromium-*/chrome-linux-arm64/`。
本脚本直接起 headless Chrome + CDP（websocket-client），能截图、执行 JS、抓网络请求。

用法（库）：
    from tools.cdp_drive import Chrome
    with Chrome() as c:
        c.navigate("https://192.168.10.90/")
        c.screenshot("/tmp/login.png")
        print(c.text())
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import time
import urllib.request

import websocket

PORT = 9333
PROFILE = "/tmp/cobo-cdp-profile"


def find_chrome() -> str:
    for pat in (
        os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"),
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    raise SystemExit("没有找到 Chrome")


class Chrome:
    def __init__(self, port: int = PORT):
        self.port = port
        self.proc = None
        self.ws = None
        self._id = 0
        self.network: list[dict] = []

    # ---------- lifecycle ----------
    def __enter__(self):
        self.proc = subprocess.Popen(
            [find_chrome(), "--headless=new", "--no-sandbox", "--disable-gpu",
             "--ignore-certificate-errors", "--disable-dev-shm-usage",
             f"--remote-debugging-port={self.port}",
             f"--user-data-dir={PROFILE}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/json/version", timeout=1) as r:
                    info = json.loads(r.read().decode())
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise SystemExit("Chrome 没起来")
        targets = json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/json/list", timeout=2).read().decode())
        page = next(t for t in targets if t["type"] == "page")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"],
                                              timeout=30, max_size=None)
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Network.enable")
        return self

    def __exit__(self, *exc):
        try:
            self.ws.close()
        finally:
            if self.proc:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()

    # ---------- CDP ----------
    def send(self, method: str, **params):
        self._id += 1
        msg_id = self._id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            raw = json.loads(self.ws.recv())
            if raw.get("method") == "Network.requestWillBeSent":
                req = raw["params"]["request"]
                self.network.append({"url": req["url"], "method": req["method"],
                                     "postData": req.get("postData")})
            if raw.get("id") == msg_id:
                if "error" in raw:
                    raise RuntimeError(f"{method}: {raw['error']}")
                return raw.get("result", {})

    def navigate(self, url: str, wait: float = 3.0):
        self.send("Page.navigate", url=url)
        time.sleep(wait)

    def eval(self, expression: str, await_promise: bool = False):
        res = self.send("Runtime.evaluate", expression=expression,
                        returnByValue=True, awaitPromise=await_promise)
        return res.get("result", {}).get("value")

    def text(self) -> str:
        return self.eval("document.body ? document.body.innerText : ''") or ""

    def screenshot(self, path: str):
        import base64
        data = self.send("Page.captureScreenshot", format="png")["data"]
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))
        return path

    def wait_for(self, js_condition: str, timeout: float = 20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval(js_condition):
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False


if __name__ == "__main__":
    with Chrome() as c:
        c.navigate("https://192.168.10.90/", wait=6)
        print("title:", c.eval("document.title"))
        print("text[:400]:", c.text()[:400].replace("\n", " | "))
        print("shot:", c.screenshot("/tmp/cobo_login.png"))
        print("requests:", len(c.network))
