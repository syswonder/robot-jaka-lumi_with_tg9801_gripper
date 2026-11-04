#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Lumi 手臂 + 扩展 IO 从站带到「SDK 可读写夹爪」的状态。

为什么需要这个脚本（2026-11-04 实机摸清的顺序，少一步都不行）：
  1. 机械臂上电，等约 5 s（夹爪靠机械臂取电）；
  2. **机械臂必须下使能**——App 在使能态点「运行」会提示
     「请确保机器人下使能，才能进行操作！」，按钮静默无效；
  3. 从站启停只能在 App UI 里点（`MSGID_SETEXTIOMODE` 直调恒返 0 但不生效）；
  4. 控制源在 App 时 SDK 写操作返 10007，所以点完「运行」要把控制源移交回远程。

顺序：SDK power_on → 5 s → 移交远程 → SDK disable_robot → 夺取本地
      → App 点「运行」→ 移交远程 → SDK 校验 is_extio_running()==1

用法：
    python3 tools/jaka_bringup.py up        # 一条命令走完上面全部步骤
    python3 tools/jaka_bringup.py status    # 只看当前状态（不动任何东西）
    python3 tools/jaka_bringup.py stop      # 在 App 里停掉从站（保持控制源不变）

凭据：JAKA_WEB_USER / JAKA_WEB_PASSWORD（默认 jakazu7admin，密码需显式给）；
      SDK 账号用 jaka_sdk / JAKAsdk@123（与 provider 一致）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SDK_PKG = os.path.expanduser("~/lumi/primitive-jaka-mini_with_tg9801_gripper-arm_gripper-rbnx")
IP = "192.168.10.90"
SUB = 54052
WEB_USER = os.environ.get("JAKA_WEB_USER", "jakazu7admin")
WEB_PASS = os.environ.get("JAKA_WEB_PASSWORD", "")

sys.path.insert(0, HERE)
sys.path.insert(0, SDK_PKG)

import extio_slave  # noqa: E402  (login/logout/handle_rpc/rpc)
from cdp_drive import Chrome  # noqa: E402


# ---------- SDK ----------
def sdk_login():
    from jaka_arm import jaka_sdk  # noqa: F401  (loader)
    import jkrc
    rc = jkrc.RC(IP)
    ret = rc.login(1, "jaka_sdk", "JAKAsdk@123")
    if ret[0] != 0:
        raise SystemExit(f"SDK 登录失败: {ret}")
    return rc


def sdk_state(rc) -> tuple:
    return tuple(rc.get_robot_state()[1])


# ---------- 网页 API ----------
def web(action: str) -> int:
    if not WEB_PASS:
        raise SystemExit("需要 JAKA_WEB_PASSWORD（App 管理员口令）")
    token = extio_slave.login(IP, WEB_USER, WEB_PASS)
    try:
        if action == "remote":
            return extio_slave.handle_rpc(IP, token, "TransCtrl", mode=3)[0]
        if action == "local":
            return extio_slave.handle_rpc(IP, token, "CaptureCtrl")[0]
        raise SystemExit(f"未知动作 {action}")
    finally:
        extio_slave.logout(IP, token)


# ---------- App UI ----------
def app_login(c: Chrome) -> None:
    c.navigate("https://192.168.10.90/", wait=8)
    c.eval("""[...document.querySelectorAll('.ant-segmented-item')]
              .find(e=>e.innerText.includes('离线')).click()""")
    time.sleep(1)
    c.eval(f"""
      const set=(el,v)=>{{const s=Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype,'value').set;s.call(el,v);
        el.dispatchEvent(new Event('input',{{bubbles:true}}));
        el.dispatchEvent(new Event('change',{{bubbles:true}}));}};
      const ip=document.querySelector('input[placeholder="请输入机器人IP"]');
      if(ip) set(ip,'{IP}');
      const pw=document.querySelector('#LOGIN_PASSWORD');
      if(pw) set(pw,'{WEB_PASS}');
    """)
    time.sleep(1)
    c.eval("""[...document.querySelectorAll('button')]
              .find(x=>x.innerText.includes('连接机器人')).click()""")
    time.sleep(5)
    # 首次连接会弹 TLS 证书确认
    c.eval("""(()=>{const b=[...document.querySelectorAll('button')]
               .find(x=>x.innerText.replace(/\\s/g,'')==='确认'); b&&b.click();})()""")
    time.sleep(12)


def app_modbus(c: Chrome) -> None:
    c.eval("""(()=>{const n=[...document.querySelectorAll('*')]
               .filter(e=>e.innerText&&e.innerText.trim()==='设置');
               n[n.length-1].click();})()""")
    time.sleep(4)
    c.eval("""(()=>{const a=[...document.querySelectorAll('*')]
               .filter(e=>e.children.length===0&&e.innerText&&
                          e.innerText.trim()==='Modbus');
               const el=a[a.length-1]; let p=el;
               for(let i=0;i<4&&p;i++){if(p.className&&
                 String(p.className).match(/item|menu|card|grid/i))break;
                 p=p.parentElement;}
               (p||el).click();})()""")
    time.sleep(6)


def app_slave_button_state(c: Chrome) -> str:
    return (c.eval("""(()=>{const b=document.querySelector('#SET_HC_MODBUS_RUN_0');
                       return b? b.innerText.trim() : 'no-button'})()""") or "")


def app_click_slave(c: Chrome) -> None:
    c.eval("""(()=>{const b=document.querySelector('#SET_HC_MODBUS_RUN_0');
              b&&b.click();})()""")
    time.sleep(8)


def app_logout(c: Chrome) -> None:
    """退出登录，释放控制器的 App 单会话（否则下一次登录会 917508）。"""
    c.eval("""(()=>{const b=[...document.querySelectorAll('button')]
               .find(x=>x.innerText.replace(/\\s/g,'')==='退出登录');
               b&&b.click();})()""")
    time.sleep(2)
    c.eval("""(()=>{const b=[...document.querySelectorAll('.ant-modal button')]
               .find(x=>/确定|确认/.test(x.innerText.replace(/\\s/g,'')));b&&b.click();})()""")
    time.sleep(2)


def app_ensure_slave(c: Chrome, want_running: bool) -> bool:
    app_login(c)
    app_modbus(c)
    label = app_slave_button_state(c)
    print(f"[app] 从站按钮当前显示: {label!r}")
    running = label == "停止"      # 按钮显示「停止」= 从站正在运行
    if running == want_running:
        print(f"[app] 从站已在{'运行' if running else '停止'}状态，无需操作")
        app_logout(c)
        return True
    app_click_slave(c)
    label = app_slave_button_state(c)
    print(f"[app] 点击后按钮显示: {label!r}")
    app_logout(c)
    return (label == "停止") == want_running


# ---------- 命令 ----------
def cmd_status() -> int:
    rc = sdk_login()
    try:
        state = sdk_state(rc)
        extio = rc.is_extio_running()
        ai = [rc.get_analog_input(2, i, SUB)[1] for i in range(3)]
        print(f"机器人状态 : powered_on={state[1]} enabled={state[2]}")
        print(f"扩展 IO    : {extio}")
        print(f"AI 位置/电流/状态: {ai}")
        print(f"last_error : {rc.get_last_error()}")
    finally:
        rc.logout()
    if WEB_PASS:
        token = extio_slave.login(IP, WEB_USER, WEB_PASS)
        try:
            reply = extio_slave._post(IP, "/grpcRequest/", {
                "reqtype": "MSGID_GETEXTIOMODCONFIG", "ip": IP,
                "robot_index": 0, "intid": SUB}, token)
            data = reply.get("data", {})
            print(f"从站配置读取: errcode={data.get('errcode')}")
        finally:
            extio_slave.logout(IP, token)
    return 0


def cmd_stop() -> int:
    if not WEB_PASS:
        raise SystemExit("需要 JAKA_WEB_PASSWORD（App 管理员口令）")
    print("夺取控制源:", web("local"))
    try:
        with Chrome() as c:
            app_ensure_slave(c, want_running=False)
    finally:
        print("移交控制源:", web("remote"))
    return 0


def cmd_up() -> int:
    if not WEB_PASS:
        raise SystemExit("需要 JAKA_WEB_PASSWORD（App 管理员口令）")
    rc = sdk_login()
    ok = True
    try:
        state = sdk_state(rc)
        print(f"[1] 初始状态: powered_on={state[1]} enabled={state[2]}")
        if state[1] == 0:
            print("    上电:", rc.power_on())
            time.sleep(5)
        else:
            print("    已上电，跳过")

        # 控制源交回远程，SDK 才能下使能
        print("[2] 移交控制源到远程:", web("remote"))
        time.sleep(1)
        if sdk_state(rc)[2] == 1:
            print("    下使能:", rc.disable_robot())
            time.sleep(1)
        print("    当前状态:", sdk_state(rc))

        print("[3] 夺取控制源到 App:", web("local"))
        with Chrome() as c:
            ok = app_ensure_slave(c, want_running=True) and ok

        print("[4] 移交控制源到远程:", web("remote"))
        time.sleep(2)
        extio = rc.is_extio_running()
        print(f"[5] is_extio_running = {extio}")
        ok = ok and extio == (0, 1)
        if ok:
            ai = [rc.get_analog_input(2, i, SUB)[1] for i in range(3)]
            print(f"    AI 位置/电流/状态 = {ai}")
            print("    写入探针:", rc.set_analog_output(2, 0, 1000.0, SUB))
    finally:
        rc.logout()
    print("结果:", "OK" if ok else "失败，见上面日志")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["up", "status", "stop"])
    args = ap.parse_args()
    return {"up": cmd_up, "status": cmd_status, "stop": cmd_stop}[args.action]()


if __name__ == "__main__":
    raise SystemExit(main())
