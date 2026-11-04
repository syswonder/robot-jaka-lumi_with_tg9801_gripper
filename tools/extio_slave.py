#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制 JAKA 控制器的扩展 IO 从站与控制源（Coboπ 网页背后的接口）。

背景：从站停掉时 `set_analog_output` 返回 -61，控制器 last_error 是
`983126 Invalid access to extio since it's not running!`；控制源在本地时所有写
操作返回 `10007`（gRPC Code 7 `please switch to in Remote control source by APP`）。
SDK 与 legacy 协议都没有这两个开关，只有 Coboπ 网页有，所以这里复用它的接口：

    POST /webapp/api/user/login          {username, password, ip, ...}   → token
    POST /webapp/api/user/logout         {ip, robot_index}               (+ token)
    POST /webapp/api/grpcRequest/        {reqtype: MSGID_..., ip, ...}
    POST /webapp/api/grpcRequest/handleRpc  {rpcKey: CaptureCtrl|TransCtrl, ...} (+ token)

注意：
  · 请求体里必须带 `ip`，否则网关拿 `undefined` 解析后端地址，返回
    `14 UNAVAILABLE: Name resolution failed for target dns:undefined:50053`。
  · `handleRpc` 必须带 `Authorization: <token>`，否则 `16 UNAUTHENTICATED`。
  · 控制器对 App 会话是全局单会话：用完要 logout，否则下一次登录会得到
    `917508 Login failed:other app account already login`。
  · 从站启停只在控制源为「本地/App」时被允许（远程时报 917522 Permission denied）。

用法：
    python3 tools/extio_slave.py status
    python3 tools/extio_slave.py start|stop
    python3 tools/extio_slave.py ctrl remote|local

凭据走环境变量或参数：JAKA_WEB_USER / JAKA_WEB_PASSWORD。
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

DEFAULT_IP = "192.168.10.90"
DEFAULT_SUBMODULE = 54052
_CTX = ssl._create_unverified_context()  # 控制器用自签证书


def _post(ip: str, path: str, payload: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json", "robotIp": ip, "jointNums": "6"}
    if token:
        headers["Authorization"] = token
    req = urllib.request.Request(
        f"https://{ip}/webapp/api{path}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=_CTX) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:  # 500 也带 JSON 说明
        body = exc.read().decode(errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise SystemExit(f"HTTP {exc.code}: {body[:300]}") from exc


def login(ip: str, user: str, password: str) -> str:
    reply = _post(ip, "/user/login", {
        "username": user, "password": password, "ip": ip,
        "ismanual": True, "robot_index": 0, "page": "login",
    })
    data = reply.get("data", {}).get("loginData", {})
    err = data.get("errinfo", {})
    if err.get("errcode"):
        raise SystemExit(f"登录失败 {err.get('errcode')}: {err.get('errormsg')}")
    token = data.get("token")
    if not token:
        raise SystemExit(f"登录没有返回 token: {reply}")
    return token


def logout(ip: str, token: str) -> None:
    try:
        _post(ip, "/user/logout", {"ip": ip, "robot_index": 0}, token)
    except SystemExit:
        pass


def rpc(ip: str, reqtype: str, **fields) -> tuple[int, str]:
    reply = _post(ip, "/grpcRequest/", {
        "reqtype": reqtype, "ip": ip, "robot_index": 0, **fields,
    })
    data = reply.get("data", {})
    return int(data.get("errcode", -1)), str(data.get("errormsg", ""))


def handle_rpc(ip: str, token: str, key: str, **fields) -> tuple[int, str]:
    reply = _post(ip, "/grpcRequest/handleRpc", {
        "rpcKey": key, "robot_index": 0, "ip": ip, **fields,
    }, token)
    data = reply.get("data", {})
    if not data and reply.get("errormsg"):
        return -1, str(reply["errormsg"])
    return int(data.get("errcode", 0)), str(data.get("errormsg") or "")


def status(ip: str, submodule: int) -> int:
    code, msg = rpc(ip, "MSGID_GETEXTIOMODCONFIG", intid=submodule)
    print(f"从站 {submodule} 配置读取: errcode={code} {msg}".rstrip())
    return 0 if code == 0 else 1


def set_mode(ip: str, token: str, submodule: int, start: bool) -> int:
    code, msg = rpc(ip, "MSGID_SETEXTIOMODE", intid=submodule)
    action = "启动" if start else "停止"
    if code == 0:
        print(f"{action}从站 {submodule}: API 返回成功")
        return 0
    if code == 917522:
        print(f"{action}从站 {submodule}: 权限不足（917522）。"
              "先把控制源切到本地（ctrl local）再试。")
    else:
        print(f"{action}从站 {submodule} 失败: errcode={code} {msg}")
    return 1


def set_ctrl(ip: str, token: str, to_remote: bool) -> int:
    if to_remote:
        code, msg = handle_rpc(ip, token, "TransCtrl", mode=3)
        label = "移交控制源到外部（远程）"
    else:
        code, msg = handle_rpc(ip, token, "CaptureCtrl")
        label = "夺取控制源到 App（本地）"
    if code == 0:
        print(f"{label}: 成功")
        return 0
    print(f"{label} 失败: errcode={code} {msg}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["status", "start", "stop", "ctrl"])
    ap.add_argument("target", nargs="?", choices=["remote", "local"],
                    help="ctrl 的目标控制源")
    ap.add_argument("--ip", default=DEFAULT_IP)
    ap.add_argument("--submodule", type=int, default=DEFAULT_SUBMODULE)
    ap.add_argument("--user", default=os.environ.get("JAKA_WEB_USER", "jaka_sdk_app"))
    ap.add_argument("--password", default=os.environ.get("JAKA_WEB_PASSWORD", ""))
    args = ap.parse_args()

    if args.action == "status":
        return status(args.ip, args.submodule)

    if args.action == "ctrl" and not args.target:
        print("ctrl 需要 remote 或 local", file=sys.stderr)
        return 2
    if not args.password:
        print("需要 --password 或 JAKA_WEB_PASSWORD", file=sys.stderr)
        return 2

    token = login(args.ip, args.user, args.password)
    try:
        if args.action == "ctrl":
            return set_ctrl(args.ip, token, to_remote=args.target == "remote")
        return set_mode(args.ip, token, args.submodule, start=args.action == "start")
    finally:
        logout(args.ip, token)


if __name__ == "__main__":
    raise SystemExit(main())
