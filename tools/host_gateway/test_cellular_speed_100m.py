#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780EPV 4G 蜂窝网络 - 100MB 4路并发极限压力测速工具
通过动态检测 RNDIS 接口 IP 并进行套接字源地址强绑定，排除有线/WiFi 旁路，
在 4 路并发高负载下测试 LTE Cat.1 bis 的峰值聚合吞吐、基站并发调度、多流 RTT 与芯片温升。
"""

import time
import socket
import ssl
import json
import threading
import sys
import psutil
from typing import Dict, Any, List, Optional

TARGET_HOST = "speed.cloudflare.com"
TARGET_IP = "172.66.0.218"

NUM_THREADS = 4
BYTES_PER_THREAD = 25 * 1024 * 1024  # 25 MB per thread -> 100 MB total
TOTAL_BYTES_TARGET = NUM_THREADS * BYTES_PER_THREAD

def detect_rndis_ip() -> Optional[str]:
    """动态探测 Air780EPV USB RNDIS 虚拟网卡分配的 IPv4 地址"""
    for iface, addrs in psutil.net_if_addrs().items():
        if "以太网" in iface or "NDIS" in iface:
            for a in addrs:
                if a.family == socket.AF_INET:
                    # 排除本地路由和内网 192.168.x.x / 169.254.x.x
                    if not a.address.startswith("192.168.") and not a.address.startswith("169.254."):
                        return a.address
    return None

def get_gateway_hardware_status() -> Dict[str, Any]:
    try:
        s = socket.socket()
        s.settimeout(1.5)
        s.connect(("127.0.0.1", 17800))
        cmd = {"type": "cmd", "id": "stat_probe", "cmd": "get_status", "params": {}}
        s.sendall((json.dumps(cmd) + "\n").encode())
        res = s.recv(4096).decode()
        s.close()
        for line in res.splitlines():
            obj = json.loads(line)
            if obj.get("id") == "stat_probe" and "data" in obj:
                return obj["data"]
    except Exception:
        pass
    return {}

# 全局共享状态
total_bytes_downloaded = 0
thread_bytes = {i + 1: 0 for i in range(NUM_THREADS)}
bytes_lock = threading.Lock()
is_running = True
thread_latencies = {}
thread_errors = {}

def worker_thread(thread_idx: int, bind_ip: str):
    global total_bytes_downloaded
    raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        raw_sock.bind((bind_ip, 0))
        raw_sock.settimeout(30)

        t0 = time.time()
        raw_sock.connect((TARGET_IP, 443))
        tcp_rtt = (time.time() - t0) * 1000

        context = ssl.create_default_context()
        s = context.wrap_socket(raw_sock, server_hostname=TARGET_HOST)
        ssl_rtt = (time.time() - t0) * 1000
        thread_latencies[thread_idx] = (tcp_rtt, ssl_rtt)

        req = (
            f"GET /__down?bytes={BYTES_PER_THREAD} HTTP/1.1\r\n"
            f"Host: {TARGET_HOST}\r\n"
            f"User-Agent: Air780EPV-Concurrent-Stress/{thread_idx}\r\n"
            f"Connection: close\r\n\r\n"
        )
        s.sendall(req.encode("utf-8"))

        header_done = False
        while is_running:
            chunk = s.recv(32768)
            if not chunk:
                break
            if not header_done:
                if b"\r\n\r\n" in chunk:
                    _, body = chunk.split(b"\r\n\r\n", 1)
                    header_done = True
                    b_len = len(body)
                    with bytes_lock:
                        total_bytes_downloaded += b_len
                        thread_bytes[thread_idx] += b_len
                    continue
            b_len = len(chunk)
            with bytes_lock:
                total_bytes_downloaded += b_len
                thread_bytes[thread_idx] += b_len
        s.close()
    except Exception as e:
        thread_errors[thread_idx] = str(e)
    finally:
        try:
            raw_sock.close()
        except Exception:
            pass

def main():
    global is_running
    bind_ip = detect_rndis_ip()
    if not bind_ip:
        print("[-] 错误: 未检测到 Air780EPV RNDIS 4G 蜂窝网卡 IP，请确认 RNDIS 随身网络已开启。")
        sys.exit(1)

    print("=" * 65)
    print("  [Air780EPV] 100MB 4路并发极限压力测速 (Concurrent Stress Test)")
    print(f"  [物理接口] 强绑定 4G IP: {bind_ip} (中国联通基站)")
    print(f"  [压测配置] {NUM_THREADS} 路并行流 x 25.0 MB = 总目标 100.0 MB")
    print("=" * 65)

    hw_init = get_gateway_hardware_status()
    csq_init = hw_init.get("csq", "未知")
    rsrp_init = hw_init.get("rsrp", "未知")
    temp_init = hw_init.get("temp", "未知")
    vbat_init = hw_init.get("vbat", "未知")
    print(f"[*] 起测硬件状态: 信号 CSQ {csq_init}/31 ({rsrp_init} dBm) | 初始温度: {temp_init} C | 稳态电压: {vbat_init} V")
    print(f"[*] 正在通过 4G 蜂窝物理信道并行建立 {NUM_THREADS} 路独立 TLS 连接...")

    t_start = time.time()
    threads: List[threading.Thread] = []
    for i in range(NUM_THREADS):
        t = threading.Thread(target=worker_thread, args=(i + 1, bind_ip), daemon=True)
        threads.append(t)
        t.start()

    # 监控采样
    speed_samples = []
    temp_records = []
    try:
        if str(temp_init).replace(".", "").isdigit():
            temp_records.append(float(temp_init))
    except Exception:
        pass

    last_bytes = 0
    last_time = time.time()
    last_hw_check = time.time()
    cur_temp = temp_init

    try:
        while True:
            time.sleep(1.0)
            now = time.time()
            with bytes_lock:
                cur_bytes = total_bytes_downloaded

            delta_b = cur_bytes - last_bytes
            delta_t = now - last_time
            last_bytes = cur_bytes
            last_time = now

            inst_mbps = (delta_b * 8) / (delta_t * 1_000_000) if delta_t > 0 else 0
            inst_kb_s = (delta_b / 1024) / delta_t if delta_t > 0 else 0
            if inst_mbps > 0.1:
                speed_samples.append(inst_mbps)

            progress = min(100.0, (cur_bytes / TOTAL_BYTES_TARGET) * 100)
            mb_done = cur_bytes / (1024 * 1024)

            # 每 5 秒轮询一次模组物理健康看板
            if now - last_hw_check >= 5.0:
                last_hw_check = now
                hw_now = get_gateway_hardware_status()
                cur_temp = hw_now.get("temp", cur_temp)
                try:
                    temp_records.append(float(cur_temp))
                except Exception:
                    pass

            elapsed = now - t_start
            sys.stdout.write(
                f"\r  -> 进度: {progress:5.1f}% ({mb_done:5.1f}/100.0 MB) | "
                f"4路聚合: {inst_mbps:5.2f} Mbps ({inst_kb_s:6.1f} KB/s) | "
                f"芯片温度: {cur_temp} C | 耗时: {elapsed:4.0f}s"
            )
            sys.stdout.flush()

            alive_count = sum(1 for t in threads if t.is_alive())
            if alive_count == 0 or cur_bytes >= TOTAL_BYTES_TARGET:
                break
    except KeyboardInterrupt:
        print("\n[*] 用户手动中止测速")
        is_running = False

    t_end = time.time()
    total_duration = t_end - t_start

    hw_final = get_gateway_hardware_status()
    temp_final = hw_final.get("temp", cur_temp)
    try:
        temp_records.append(float(temp_final))
    except Exception:
        pass

    final_mb = total_bytes_downloaded / (1024 * 1024)
    avg_mbps = (total_bytes_downloaded * 8) / (total_duration * 1_000_000) if total_duration > 0 else 0
    avg_kb_s = (total_bytes_downloaded / 1024) / total_duration if total_duration > 0 else 0
    peak_mbps = max(speed_samples) if speed_samples else avg_mbps
    peak_temp = max(temp_records) if temp_records else temp_final

    print("\n\n" + "=" * 65)
    print("  [报告] Air780EPV 100MB 4路并发极限压力测速结果")
    print("=" * 65)
    print(f"  * 接入制式:         中国联通 4G LTE (LTE Cat.1 bis)")
    print(f"  * 并发流连接数:     {NUM_THREADS} 路独立 TLS 长连接")
    for tid in range(1, NUM_THREADS + 1):
        if tid in thread_latencies:
            rtt_tcp, rtt_ssl = thread_latencies[tid]
            mb_t = thread_bytes[tid] / (1024 * 1024)
            print(f"    - 流 #{tid}: TCP握手 {rtt_tcp:5.1f} ms | TLS耗时 {rtt_ssl:5.1f} ms | 贡献下载 {mb_t:5.2f} MB")
        elif tid in thread_errors:
            print(f"    - 流 #{tid}: 异常中断 ({thread_errors[tid]})")

    print(f"  * 实际传输总量:     {final_mb:.2f} MB / 100.00 MB")
    print(f"  * 持续高压耗时:     {total_duration:.2f} 秒 ({total_duration / 60:.2f} 分钟)")
    print(f"  * 平均聚合下行带宽: {avg_mbps:.2f} Mbps ({avg_kb_s:.1f} KB/s)")
    print(f"  * 瞬时峰值聚合带宽: {peak_mbps:.2f} Mbps ({peak_mbps * 125:.1f} KB/s)")
    print(f"  * 核心芯片温升轨迹: 起测 {temp_init} C -> 峰值 {peak_temp} C -> 结束 {temp_final} C")
    print(f"  * 射频大负荷压降:   起测 {vbat_init} V -> 结束 {hw_final.get('vbat', vbat_init)} V")
    print("=" * 65)

if __name__ == "__main__":
    main()
