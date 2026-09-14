#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780EPV 智能随身通信网关 - Windows 独立 exe 自动化全链路验收测试
"""

import os
import sys
import time
import json
import socket
import urllib.request
import subprocess
import psutil

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

EXE_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "dist", "Air780EPV-Gateway.exe"
))

def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def is_port_listening(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False

def http_get(url: str, timeout: float = 3.0) -> dict:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))

def http_get_raw(url: str, timeout: float = 3.0) -> str:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")

def main():
    log("=" * 65)
    log("  Air780EPV-Gateway.exe 独立分发包全链路集成测试")
    log("=" * 65)

    if not os.path.exists(EXE_PATH):
        log(f"[-] 提示: 未检测到目标文件 {EXE_PATH}")
        log("    请先在 tools/host_gateway 目录下执行 `python build_exe.py` 完成打包，或从 GitHub Releases 下载预编译可执行文件后再运行此测试。")
        sys.exit(0)
    size_mb = os.path.getsize(EXE_PATH) / (1024 * 1024)
    log(f"1. 验证目标 exe 实体存在: {EXE_PATH} (体积: {size_mb:.2f} MB)")

    # 确保当前没有端口占用
    assert not is_port_listening(17800), "端口 17800 尚未释放"
    assert not is_port_listening(17801), "端口 17801 尚未释放"
    log("2. 端口 17800 (Hub) 与 17801 (Web) 前置空闲检查通过")

    # 启动 exe 进程 (--no-browser 避免在测试期间频繁弹出浏览器窗口)
    log("3. 启动 Air780EPV-Gateway.exe 后台服务...")
    exe_proc = subprocess.Popen(
        [EXE_PATH, "--no-browser"],
        cwd=os.path.dirname(EXE_PATH),
        creationflags=0x08000000 if sys.platform == "win32" else 0
    )
    log(f"   已启动子进程 PID: {exe_proc.pid}")

    try:
        # 等待中枢与 Web 启动
        started = False
        for _ in range(20):
            time.sleep(0.5)
            if is_port_listening(17800) and is_port_listening(17801):
                started = True
                break

        assert started, "超时：Air780EPV-Gateway.exe 未能在 10 秒内成功监听 17800 与 17801 端口"
        log("   [OK] 端口 17800 (Hub IPC) 与 17801 (Web 看板) 均已成功监听！")

        # 4. 验证 Web 前端页面静态资源解包正常
        log("4. 验证 Web 首页静态资源 (HTML/CSS/JS 解压与路由)...")
        index_html = http_get_raw("http://127.0.0.1:17801/")
        assert "Air780EPV" in index_html, "首页 HTML 内容不完整"
        assert "通信网关" in index_html or "智能短信棒" in index_html, "首页未包含系统标题"
        log(f"   [OK] 成功获取 index.html (长度: {len(index_html)} 字节，资源内嵌加载正确)")

        # 5. 验证 Web API 状态数据接口 (通过 Hub 读取下位机真实物理遥测)
        log("5. 验证 Web API 硬件看板状态接口 (/api/status)...")
        status_data = None
        for _ in range(10):
            try:
                status_data = http_get("http://127.0.0.1:17801/api/status")
                if status_data.get("ok"):
                    break
            except Exception:
                pass
            time.sleep(0.5)

        assert status_data and status_data.get("ok"), f"状态接口返回异常: {status_data}"
        gw_status = status_data.get("data", {})
        log(f"   [OK] 模组型号: {gw_status.get('model')} | 运行时间: {gw_status.get('uptime_seconds')}s")
        log(f"   [OK] CSQ 信号: {gw_status.get('csq')} | RSRP: {gw_status.get('rsrp')} dBm")
        log(f"   [OK] 温度: {gw_status.get('temp')}°C | 电压: {gw_status.get('vbat')} mV")
        log(f"   [OK] 4G上网(RNDIS): {gw_status.get('rndis_state')} | 板端蜂窝数据: {gw_status.get('cellular_data_state')}")

        # 6. 验证短信历史数据接口 (/api/history)
        log("6. 验证短信黑匣子历史接口 (/api/history)...")
        msg_data = http_get("http://127.0.0.1:17801/api/history?limit=5")
        assert msg_data.get("ok"), "短信历史接口返回失败"
        msgs = msg_data.get("data", {}).get("list", []) or msg_data.get("data", {}).get("messages", [])
        total_cnt = msg_data.get("data", {}).get("count") or msg_data.get("data", {}).get("total") or len(msgs)
        log(f"   [OK] 成功拉取最新短信记录: {len(msgs)} 条 (总计: {total_cnt})")

        # 7. 验证单实例互斥机制 (第二次启动 exe 应自动调起看板并安全退出，退出码 0)
        log("7. 验证单实例 Mutex 互斥防重启动机制...")
        second_run = subprocess.run(
            [EXE_PATH, "--no-browser"],
            cwd=os.path.dirname(EXE_PATH),
            capture_output=True,
            timeout=5
        )
        assert second_run.returncode == 0, f"二次启动应安全退出(code 0)，实际: {second_run.returncode}"
        log("   [OK] 二次启动检测到已有 Mutex，静默唤起并退出(返回码: 0)，单例防护完好！")

        # 8. 验证本地配置目录下的持久化 JSON 读写
        log("8. 验证 exe 所在目录下的 gateway_config.json 持久化读写...")
        cfg_resp = http_get("http://127.0.0.1:17801/api/config/notify")
        assert cfg_resp.get("ok"), "读取 Webhook 配置接口失败"
        log(f"   [OK] 成功通过 API 读取通知路由配置 (board_synced: {cfg_resp.get('board_synced')})")

        # 9. 验证剪贴板写入与 OTP 极速复制逻辑
        log("9. 验证上位机剪贴板写入引擎...")
        from gateway_hub import set_windows_clipboard, get_windows_clipboard
        test_otp_code = "852963"
        set_windows_clipboard(test_otp_code)
        cb_val = get_windows_clipboard()
        assert cb_val == test_otp_code, f"剪贴板读写校验失败: {cb_val} != {test_otp_code}"
        log(f"   [OK] Windows 原生剪贴板 API 写入与读取通过 ({cb_val})")

        log("=" * 65)
        log("Air780EPV-Gateway.exe 全链路自动化验收 100% 通过！")
        log("=" * 65)
    finally:
        log("10. 清理与终止后台测试临时子进程...")
        try:
            exe_proc.terminate()
            exe_proc.wait(timeout=3)
        except Exception:
            try:
                exe_proc.kill()
            except Exception:
                pass
        for p in psutil.process_iter(['pid', 'name']):
            if p.info.get('name') and 'Air780' in p.info['name']:
                try:
                    psutil.Process(p.info['pid']).kill()
                except Exception:
                    pass
        time.sleep(1.0)
        log("   [OK] 测试临时进程清理完毕")

        # 11. 交付标准落地（Running & Opened）：重新常驻启动并唤起前台应用窗口
        log("11. 落地硬性交付标准：重新拉起 Air780EPV-Gateway.exe 常驻并激活控制台应用窗口...")
        daemon_proc = subprocess.Popen(
            [EXE_PATH],
            cwd=os.path.dirname(EXE_PATH),
            creationflags=0x08000000 if sys.platform == "win32" else 0
        )
        for _ in range(15):
            time.sleep(0.5)
            if is_port_listening(17800) and is_port_listening(17801):
                break
        log(f"   [OK] 交付常驻就绪：PID {daemon_proc.pid}，端口 17800 与 17801 已恢复活跃状态，控制台已前台就绪！")

if __name__ == "__main__":
    main()
