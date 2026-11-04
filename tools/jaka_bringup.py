#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Lumi 手臂 + 扩展 IO 从站带到「SDK 可读写夹爪」的状态。

顺序（2026-11-04 实机摸清，少一步都不行）：
  1. 机械臂上电，等约 5 s（夹爪靠机械臂取电）；
  2. **机械臂必须下使能**——使能态启动从站会得到
     「请确保机器人下使能，才能进行操作！」；
  3. 控制源必须是本地（App）才允许启停从站，所以先夺取、启完再移交回远程；
  4. `MSGID_SETEXTIOMODE` 必须带 App 会话 token 且用 `extio_mode_req.tarmode`
     （1=运行 0=停止）；写成 `intid`/`mode` 或不带 token 都会返回成功却无效。

用法：
    python3 tools/jaka_bringup.py up        # 一条命令走完上面全部步骤
    python3 tools/jaka_bringup.py status    # 只看当前状态（不动任何东西）
    python3 tools/jaka_bringup.py stop      # 停掉从站

凭据：JAKA_WEB_USER / JAKA_WEB_PASSWORD（App 管理员账号，默认 jakazu7admin）；
      SDK 账号固定 jaka_sdk / JAKAsdk@123（与 provider 一致）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SDK_PKG = os.path.expanduser("~/lumi/primitive-jaka-mini_with_tg9801_gripper-arm_gripper-rbnx")
IP = "192.168.10.90"
SUB = 54052
WEB_USER = os.environ.get("JAKA_WEB_USER", "jakazu7admin")
WEB_PASS = os.environ.get("JAKA_WEB_PASSWORD", "")

sys.path.insert(0, HERE)
sys.path.insert(0, SDK_PKG)

import extio_slave  # noqa: E402


def sdk_login():
    from jaka_arm import jaka_sdk  # noqa: F401  (触发 jkrc 加载)
    import jkrc
    rc = jkrc.RC(IP)
    ret = rc.login(1, "jaka_sdk", "JAKAsdk@123")
    if ret[0] != 0:
        raise SystemExit(f"SDK 登录失败: {ret}")
    return rc


def state(rc) -> tuple:
    return tuple(rc.get_robot_state()[1])


def extio_running(rc) -> bool:
    ret = rc.is_extio_running()
    return ret[0] == 0 and bool(ret[1])


def web(action: str) -> int:
    if not WEB_PASS:
        raise SystemExit("需要 JAKA_WEB_PASSWORD（App 管理员口令）")
    token = extio_slave.login(IP, WEB_USER, WEB_PASS)
    try:
        if action == "remote":
            return extio_slave.handle_rpc(IP, token, "TransCtrl", mode=3)[0]
        return extio_slave.handle_rpc(IP, token, "CaptureCtrl")[0]
    finally:
        extio_slave.logout(IP, token)


def set_slave(rc, run: bool, timeout_s: float = 15.0) -> bool:
    """夺取控制源 → 启/停从站 → 等状态就位 → 移交远程。"""
    token = extio_slave.login(IP, WEB_USER, WEB_PASS)
    try:
        code = extio_slave.handle_rpc(IP, token, "CaptureCtrl")[0]
        if code != 0:
            print(f"[slave] 夺取控制源失败: {code}")
            return False
        code, msg = extio_slave.set_mode(IP, token, SUB, run=run)
        if code != 0:
            print(f"[slave] SETEXTIOMODE 失败: {code} {msg}")
            return False
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if extio_running(rc) == run:
                print(f"[slave] 从站已{'启动' if run else '停止'}")
                return True
            time.sleep(0.5)
        print(f"[slave] 等待从站{'启动' if run else '停止'}超时"
              "（机器人是否上电且下使能？）")
        return False
    finally:
        extio_slave.handle_rpc(IP, token, "TransCtrl", mode=3)
        extio_slave.logout(IP, token)


def cmd_status() -> int:
    rc = sdk_login()
    try:
        st = state(rc)
        print(f"机器人状态 : powered_on={st[1]} enabled={st[2]}")
        print(f"扩展 IO    : {rc.is_extio_running()}")
        print(f"AI 位置/电流/状态: {[rc.get_analog_input(2, i, SUB)[1] for i in range(3)]}")
        print(f"last_error : {rc.get_last_error()}")
    finally:
        rc.logout()
    if WEB_PASS:
        token = extio_slave.login(IP, WEB_USER, WEB_PASS)
        try:
            code, _ = extio_slave.rpc(IP, "MSGID_GETEXTIOMODCONFIG", intid=SUB)
            print(f"从站配置读取: errcode={code}")
        finally:
            extio_slave.logout(IP, token)
    return 0


def cmd_stop() -> int:
    if not WEB_PASS:
        raise SystemExit("需要 JAKA_WEB_PASSWORD（App 管理员口令）")
    rc = sdk_login()
    try:
        return 0 if set_slave(rc, run=False) else 1
    finally:
        rc.logout()


def cmd_up() -> int:
    if not WEB_PASS:
        raise SystemExit("需要 JAKA_WEB_PASSWORD（App 管理员口令）")
    rc = sdk_login()
    try:
        st = state(rc)
        print(f"[1] 初始状态: powered_on={st[1]} enabled={st[2]}")
        if st[1] == 0:
            print("    上电:", rc.power_on())
            time.sleep(5)
        else:
            print("    已上电，跳过")
        if state(rc)[2] == 1:
            print("    下使能:", rc.disable_robot())
            time.sleep(1)
        print(f"    当前状态: {state(rc)}")

        if extio_running(rc):
            print("[2] 从站已在运行")
        else:
            print("[2] 启动从站")
            if not set_slave(rc, run=True):
                return 1

        print(f"[3] is_extio_running = {rc.is_extio_running()}")
        print(f"    AI 位置/电流/状态 = {[rc.get_analog_input(2, i, SUB)[1] for i in range(3)]}")
        print("    写入探针:", rc.set_analog_output(2, 0, 1000.0, SUB))
        return 0
    finally:
        rc.logout()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["up", "status", "stop"])
    args = ap.parse_args()
    return {"up": cmd_up, "status": cmd_status, "stop": cmd_stop}[args.action]()


if __name__ == "__main__":
    raise SystemExit(main())
