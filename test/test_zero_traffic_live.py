# -*- coding: utf-8 -*-
"""
真机全链路 0 流量保号与宽带代推自证测试脚本
"""

import sys
import os
import time
import json
import socket

HUB_HOST = "127.0.0.1"
HUB_PORT = 17800

def test_full_zero_traffic():
    print("[1/4] 连接本地网关共享中枢 127.0.0.1:17800...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect((HUB_HOST, HUB_PORT))
    except Exception as e:
        print(f"[-] 连接 Hub 失败: {e}，请确保 gateway_hub 已启动")
        return False

    print("[+] 成功接入共享中枢！")

    # 查询当前状态
    req_id = f"test_{int(time.time()*1000)}"
    cmd_pkt = {
        "type": "cmd",
        "id": req_id,
        "cmd": "get_status",
        "params": {}
    }
    s.sendall((json.dumps(cmd_pkt) + "\n").encode("utf-8"))

    buffer = ""
    status_data = None
    start_t = time.time()
    while time.time() - start_t < 5.0:
        chunk = s.recv(4096).decode("utf-8", errors="ignore")
        if not chunk:
            break
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("id") == req_id and obj.get("type") in ("res", "response"):
                    status_data = obj.get("data", {})
                    break
            except Exception:
                pass
        if status_data:
            break

    if not status_data:
        print("[-] 查询状态超时")
        s.close()
        return False

    print("[2/4] 验证双重默认关闭基线指标:")
    rndis_val = status_data.get("rndis")
    data_val = status_data.get("cellular_data")
    csq_val = status_data.get("csq")
    print(f"  - 随身上网 (RNDIS): {rndis_val} (期望: False)")
    print(f"  - 板载数据 (Cellular Data): {data_val} (期望: False)")
    print(f"  - 蜂窝信号 (CSQ): {csq_val}")

    assert rndis_val == False, "RNDIS 必须为关闭状态！"
    assert data_val == False, "板载蜂窝数据必须为关闭状态 (0流量)！"

    print("[3/4] 触发短信回环测试 (向本机发送测试短信)...")
    tx_id = f"tx_{int(time.time()*1000)}"
    sms_pkt = {
        "type": "cmd",
        "id": tx_id,
        "cmd": "send_sms",
        "params": {
            "phone": "+8613800138000",
            "content": "【网关自测】0流量保号与宽带代推测试 动态验证码 665544"
        }
    }
    s.sendall((json.dumps(sms_pkt) + "\n").encode("utf-8"))

    print("[4/4] 挂起等待接收短信回环 (sms_rx) 与验证码提取...")
    rx_sms_found = None
    rx_code_found = None
    wait_start = time.time()
    while time.time() - wait_start < 25.0:
        chunk = s.recv(4096).decode("utf-8", errors="ignore")
        if not chunk:
            break
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("type") == "event" and obj.get("event") == "sms_rx":
                    ev_data = obj.get("data", {})
                    if "665544" in ev_data.get("content", ""):
                        rx_sms_found = ev_data
                        rx_code_found = ev_data.get("code")
                        break
            except Exception:
                pass
        if rx_sms_found:
            break

    s.close()

    if rx_sms_found:
        print(f"\033[32;1m[SUCCESS] 成功捕获短信回环！\033[0m")
        print(f"  - 发件人: {rx_sms_found.get('from')}")
        print(f"  - 提取验证码: 【{rx_code_found}】")
        print(f"  - 正文: {rx_sms_found.get('content')}")
        return True
    else:
        print("[-] 未在超时时间内捕获到短信回环")
        return False

if __name__ == "__main__":
    success = test_full_zero_traffic()
    sys.exit(0 if success else 1)
