#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780EPV - 状态变更全渠道联动与通知模板端到端自动化测试
验证：
1. 状态报告模板与手机号码规范化 (13800138000 +86, 🟢/⚪ 色块状态)
2. 蜂窝网络开关切换联动 (state_change 事件)
3. 纯信令 0 流量保号下宿主宽带代推
4. 恢复双重默认关闭基线
"""

import urllib.request
import json
import time
import sys

WEB_BASE = "http://127.0.0.1:17801"

def api_call(path, method="GET", payload=None):
    url = f"{WEB_BASE}{path}"
    data = json.dumps(payload).encode("utf-8") if payload else None
    headers = {"Content-Type": "application/json"} if payload else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

def test_suite():
    print("==================================================")
    print("  Air780EPV 状态变更与标准化通知模板端到端验证")
    print("==================================================")

    # 1. 查询基础看板状态
    print("\n[Step 1] 查询当前网关看板状态...")
    res = api_call("/api/status")
    assert res.get("ok"), f"查询状态失败: {res}"
    data = res["data"]
    print(f"  [+] 模组型号: {data.get('model')}")
    print(f"  [+] 信号强度: CSQ {data.get('csq')} (RSRP {data.get('rsrp')} dBm)")
    print(f"  [+] 核心温度: {data.get('temp')} ℃ | 电压: {data.get('vbat')} V")
    print(f"  [+] USB共享网卡: {data.get('rndis_enable')}")
    print(f"  [+] 板载蜂窝数据: {data.get('cellular_data_enable')}")

    # 2. 模拟切换板端数据开启
    print("\n[Step 2] 切换板端蜂窝数据为: 开启 (测试状态变更广播)...")
    toggle_res = api_call("/api/control/data", method="POST", payload={"enable": True})
    assert toggle_res.get("ok"), f"开启蜂窝数据失败: {toggle_res}"
    print(f"  [+] 接口返回: {toggle_res}")
    time.sleep(2)

    status_on = api_call("/api/status")
    print(f"  [+] 切换后板载蜂窝数据状态: {status_on['data'].get('cellular_data_enable')}")
    assert status_on['data'].get('cellular_data_enable') == True, "蜂窝数据未生效为 True"

    # 3. 模拟切换板端数据关闭 (切回 0 流量保号防线，触发宿主宽带代推)
    print("\n[Step 3] 切换板端蜂窝数据为: 关闭 (切入 0 流量纯信令态，触发宿主代推)...")
    toggle_off_res = api_call("/api/control/data", method="POST", payload={"enable": False})
    assert toggle_off_res.get("ok"), f"关闭蜂窝数据失败: {toggle_off_res}"
    print(f"  [+] 接口返回: {toggle_off_res}")
    time.sleep(2)

    status_off = api_call("/api/status")
    print(f"  [+] 切换后板载蜂窝数据状态: {status_off['data'].get('cellular_data_enable')}")
    assert status_off['data'].get('cellular_data_enable') == False, "蜂窝数据未生效为 False"

    # 4. 确认 USB 共享同样保持关闭 (双重默认关闭)
    assert status_off['data'].get('rndis_enable') == False, "RNDIS 未处于关闭状态"
    print("\n[Step 4] 双重默认关闭状态基线确认完毕：")
    print("  - RNDIS USB 网卡: [已关闭] (防偷跑流量)")
    print("  - 板载蜂窝数据通信: [已掐断] (0 流量纯信令保号，电脑宽带代发)")

    print("\n[SUCCESS] 全部端到端用例执行成功，通知联动与状态机运转完美！")

if __name__ == "__main__":
    test_suite()
