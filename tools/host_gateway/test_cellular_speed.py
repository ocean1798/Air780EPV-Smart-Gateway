#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780EPV 智能随身通信网关 - 4G 蜂窝网络独立接口精准测速工具
通过 Windows 本地源 IP 接口强绑定，在保留本地 WiFi/有线网的同时，
100% 仅使用 4G 模组的 RNDIS 物理信道进行纯蜂窝上下行与延迟测试。
"""

import sys
import time
import socket
import ssl
import json
import urllib.request
from typing import Dict, Any

# 默认绑定网关 RNDIS 虚拟网卡分配的私网 IP（可通过命令行参数覆盖: python test_cellular_speed.py <ip>）
BIND_IP = sys.argv[1] if len(sys.argv) > 1 else "10.0.0.2"
TARGET_HOST = "speed.cloudflare.com"
TARGET_IP = "172.66.0.218"
TEST_BYTES = 5 * 1024 * 1024  # 5 MB 测速包

def get_gateway_hardware_status() -> Dict[str, Any]:
    try:
        s = socket.socket()
        s.settimeout(2)
        s.connect(("127.0.0.1", 17800))
        cmd = {"type": "cmd", "id": "hw_stat", "cmd": "get_status", "params": {}}
        s.sendall((json.dumps(cmd) + "\n").encode())
        res = s.recv(4096).decode()
        s.close()
        for line in res.splitlines():
            obj = json.loads(line)
            if obj.get("id") == "hw_stat" and "data" in obj:
                return obj["data"]
    except Exception:
        pass
    return {}

def run_speed_test():
    print("=" * 60)
    print("  [Air780EPV] 4G 蜂窝网络真机独立测速 (Interface Binding)")
    print(f"  [物理接口] 绑定适配器 IP: {BIND_IP} (中国联通 4G 基站)")
    print("=" * 60)

    # 1. 采集测试前硬件健康度
    hw_before = get_gateway_hardware_status()
    csq = hw_before.get("csq", "未知")
    rsrp = hw_before.get("rsrp", "未知")
    temp = hw_before.get("temp", "未知")
    vbat = hw_before.get("vbat", "未知")
    print(f"[*] 基站信号: CSQ {csq}/31 | RSRP {rsrp} dBm | 芯片温度: {temp} ℃ | 供电电压: {vbat} V")
    print(f"[*] 准备通过 4G 蜂窝物理链路建立 TCP 连接至 {TARGET_HOST} ({TARGET_IP}:443)...")

    # 2. 建立原生绑定套接字
    raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw_sock.bind((BIND_IP, 0))
    raw_sock.settimeout(15)

    # 3. 测量 TCP 握手 RTT
    t_start = time.time()
    raw_sock.connect((TARGET_IP, 443))
    t_tcp = (time.time() - t_start) * 1000
    print(f"[+] TCP 三次握手成功: 延迟 {t_tcp:.1f} ms")

    # 4. 测量 TLS 握手
    context = ssl.create_default_context()
    s = context.wrap_socket(raw_sock, server_hostname=TARGET_HOST)
    t_ssl = (time.time() - t_start) * 1000
    print(f"[+] TLS 安全加密握手完成: 耗时 {t_ssl:.1f} ms")

    # 5. 下发 5MB 流式测速请求
    req = f"GET /__down?bytes={TEST_BYTES} HTTP/1.1\r\nHost: {TARGET_HOST}\r\nUser-Agent: Air780EPV-Speedtest/1.0\r\nConnection: close\r\n\r\n"
    s.sendall(req.encode("utf-8"))

    print(f"[*] 开始流式下载 5.0 MB 测速测试数据包...")
    header_done = False
    total_bytes = 0
    start_dl = None
    last_print = 0
    samples = []

    sample_interval = 0.5
    last_sample_t = None
    last_sample_b = 0

    while True:
        chunk = s.recv(32768)
        if not chunk:
            break
        if not header_done:
            if b"\r\n\r\n" in chunk:
                h, b = chunk.split(b"\r\n\r\n", 1)
                header_done = True
                total_bytes += len(b)
                start_dl = time.time()
                last_sample_t = start_dl
                last_sample_b = total_bytes
                continue
        total_bytes += len(chunk)
        now = time.time()

        # 计算瞬时速率采样
        if start_dl and (now - last_sample_t) >= sample_interval:
            cur_delta_b = total_bytes - last_sample_b
            cur_delta_t = now - last_sample_t
            inst_mbps = (cur_delta_b * 8) / (cur_delta_t * 1_000_000)
            samples.append(inst_mbps)
            last_sample_t = now
            last_sample_b = total_bytes

            progress = (total_bytes / TEST_BYTES) * 100
            print(f"    -> 进度: {progress:5.1f}% | 瞬时速率: {inst_mbps:5.2f} Mbps ({inst_mbps*125:.1f} KB/s)")

    s.close()
    dl_total_time = time.time() - start_dl

    avg_speed_bps = (total_bytes * 8) / dl_total_time
    avg_speed_mbps = avg_speed_bps / 1_000_000
    avg_speed_kb_s = (total_bytes / 1024) / dl_total_time
    peak_mbps = max(samples) if samples else avg_speed_mbps

    # 6. 测速后硬件状态
    hw_after = get_gateway_hardware_status()
    temp_after = hw_after.get("temp", temp)

    print("\n" + "=" * 60)
    print("  [报告] Air780EPV 4G 蜂窝网络性能测速报告")
    print("=" * 60)
    print(f"  * 接入网络:       中国联通 4G (LTE Cat.1 bis)")
    print(f"  * 信号质量:       CSQ {csq}/31 ({rsrp} dBm)")
    print(f"  * 基站 RTT 延迟:  {t_tcp:.1f} ms")
    print(f"  * 消耗测试流量:   {total_bytes / 1024 / 1024:.2f} MB")
    print(f"  * 下载耗时:       {dl_total_time:.2f} 秒")
    print(f"  * 平均下行速率:   {avg_speed_mbps:.2f} Mbps ({avg_speed_kb_s:.1f} KB/s)")
    print(f"  * 峰值下行速率:   {peak_mbps:.2f} Mbps ({peak_mbps*125:.1f} KB/s)")
    print(f"  * 芯片负载温度:   {temp} C -> {temp_after} C (供电稳态 {vbat}V)")
    print("=" * 60)

if __name__ == "__main__":
    run_speed_test()
