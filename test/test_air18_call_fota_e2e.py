#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIR-18: 来电暗号（0 话费秒挂拦截）与空中热更新 (FOTA) 实机端到端守候验证脚本
连接本地 Gateway Hub (127.0.0.1:17800)，实时监听并记录:
1. 来电拒接事件 (call_rx: action=REJECTED, cost=0_toll, fota_trigger=True)
2. FOTA 启动与探测事件 (fota_status: status=checking, source=call_secret)
3. CDN 元数据对比结果 (fota_status: status=up_to_date 或 downloading/success)
"""

import sys
import json
import time
import socket

def monitor_call_and_fota(timeout_sec=60):
    print("=" * 65)
    print(" AIR-18: 来电暗号 (0元秒挂) -> FOTA 触发实机端到端监听器")
    print("=" * 65)
    print(f"[*] 正在连接网关中枢 (127.0.0.1:17800)...")
    
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(('127.0.0.1', 17800))
        s.settimeout(timeout_sec)
    except Exception as e:
        print(f"[-] 连接网关中枢失败: {e}")
        return False

    print("[+] 成功连入网关中枢 IPC 总线！")
    print("[*] 正在守候板端硬件事件 (最大等待: %d 秒)..." % timeout_sec)
    print(">>> 提示: 请使用手机拨打开发板号码: +86 13800138000")
    print(">>> 预期行为: 手机拨出后约 1~2 声响即被秒挂断 (0话费)，板端将自动触发 FOTA 探测")
    print("-" * 65)

    f = s.makefile(encoding='utf-8')
    t_start = time.time()
    call_detected = False
    fota_checking = False
    fota_result = None

    while time.time() - t_start < timeout_sec:
        try:
            line = f.readline()
            if not line:
                break
            obj = json.loads(line.strip())
            ev = obj.get("event")
            data = obj.get("data", {})

            if ev == "call_rx":
                call_detected = True
                print("\n[🎯 捕获来电事件 call_rx]")
                print(f"  来自号码: {data.get('from')}")
                print(f"  拦截动作: {data.get('action')} (已硬件级秒挂断)")
                print(f"  计费模式: {data.get('cost')} (双方 0 元话费)")
                print(f"  暗号触发: {data.get('fota_trigger')}")

            elif ev == "fota_status":
                status = data.get("status")
                if status == "checking":
                    fota_checking = True
                    print(f"\n[🚀 捕获 FOTA 触发事件 fota_status: checking]")
                    print(f"  触发源头: {data.get('source')} (呼叫暗号)")
                    print(f"  主叫方: {data.get('caller')}")
                    print(f"  当前固件: v{data.get('current_version')}")
                elif status in ("up_to_date", "downloading", "reboot_pending", "failed"):
                    fota_result = status
                    print(f"\n[🏁 捕获 FOTA 终态事件 fota_status: {status}]")
                    print(f"  远程版本: {data.get('remote_version')}")
                    print(f"  本地版本: {data.get('current_version')}")
                    if status == "up_to_date":
                        print("  结果结论: 固件已是最新对齐版本，临时蜂窝数据已自动回退掐断 (0流量保号)")
                    break

        except socket.timeout:
            print("\n[-] 守候超时，未收到来电事件。")
            break
        except Exception as e:
            pass

    s.close()
    print("-" * 65)
    print(f"[*] 监听会话结束: 来电捕获={call_detected}, FOTA激活={fota_checking}, 最终结果={fota_result}")
    return call_detected and fota_checking

if __name__ == "__main__":
    timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    monitor_call_and_fota(timeout)
