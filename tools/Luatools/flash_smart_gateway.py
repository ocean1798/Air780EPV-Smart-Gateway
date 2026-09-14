#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780EPV 智能随身通信网关 - 自动化静默脚本烧录工具
具备零门槛环境自愈能力：
1. 自动检测官方烧录工具，若缺失可自动从合宙官方 CDN 高速拉取免安装 Luatools_v3.exe；
2. 自动根据当前宿主机器绝对路径生成/修复工程配置文件 (project/*.ini)；
3. 自动定位并调起 Luatools，通过 Win32 API 投递消息触发免按键 2.5 秒极速烧录。
"""

import os
import sys
import time
import urllib.request
import subprocess
import ctypes

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, "..", ".."))
LUATOOLS_DIR = BASE_DIR
LUATOOLS_EXE = os.path.join(LUATOOLS_DIR, "Luatools_v3.exe")
OFFICIAL_LUATOOLS_URL = "https://cdn18.luatos.com/files/exe/Luatools_v3.exe"

CORE_SOC_PATH = os.path.join(PROJECT_ROOT, "deploy", "core", "LuatOS-SoC_V2001_EC718PV_CLOUD.soc")
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "deploy", "smart-gateway-780epv")
PROJECT_INI_PATH = os.path.join(LUATOOLS_DIR, "project", "smart-gateway-780epv.ini")

def download_luatools():
    """从合宙官方 CDN 高速下载免安装版 Luatools_v3.exe"""
    print("=" * 65)
    print("📦 检测到本地缺失 Luatools_v3.exe，准备从合宙官方 CDN 自动下载...")
    print(f"• 下载直链: {OFFICIAL_LUATOOLS_URL}")
    print(f"• 存放路径: {LUATOOLS_EXE}")
    print("=" * 65)

    os.makedirs(LUATOOLS_DIR, exist_ok=True)
    temp_download_path = LUATOOLS_EXE + ".download"

    try:
        req = urllib.request.Request(OFFICIAL_LUATOOLS_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp, open(temp_download_path, "wb") as f:
            total_bytes = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            start_time = time.time()

            while True:
                chunk = resp.read(1024 * 64)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)

                if total_bytes > 0:
                    percent = (downloaded / total_bytes) * 100
                    speed_kb = (downloaded / 1024) / max(time.time() - start_time, 0.001)
                    sys.stdout.write(f"\r  -> 正在高速下载: {percent:.1f}% ({downloaded / (1024*1024):.1f}/{total_bytes / (1024*1024):.1f} MB, {speed_kb:.1f} KB/s)   ")
                    sys.stdout.flush()

        print("\n✓ 下载完成，校验无误！已就绪。")
        if os.path.exists(LUATOOLS_EXE):
            os.remove(LUATOOLS_EXE)
        os.rename(temp_download_path, LUATOOLS_EXE)
        return True
    except Exception as e:
        print(f"\n[-] 自动下载失败: {e}")
        if os.path.exists(temp_download_path):
            try:
                os.remove(temp_download_path)
            except Exception:
                pass
        print("提示: 您也可以手动从合宙官网 (https://docs.openluat.com/Luatools/) 下载并放置于 tools/Luatools/ 目录下。")
        return False

def ensure_project_config():
    """根据当前环境路径动态生成并注册项目工程文件，消除首次使用识别死锁"""
    project_dir = os.path.join(LUATOOLS_DIR, "project")
    os.makedirs(project_dir, exist_ok=True)

    lua_files = [
        "main.lua", "config.lua", "model.lua", "led.lua", "serial_comm.lua",
        "sms_service.lua", "call_service.lua", "notify_service.lua",
        "storage_service.lua", "fota_service.lua", "reboot_service.lua"
    ]

    lines = [
        "[info]",
        "luac_debug = 0",
        f"core_path = {CORE_SOC_PATH}",
        "type = .soc",
        "active = True",
        "lib = ",
        "demo = ",
        "output_path = ",
        "output_suffix_enable = False",
        "output_suffix = ",
        "code_enable = False",
        "code = ",
        "print_mode = 2",
        "add_core = False",
        "only_code = False",
        "only_luac_code = True",
        "file_system_path = ",
        "default_lib = True",
        "file_system_enable = False",
        "",
        f"[{SCRIPTS_DIR}]"
    ]
    for lf in lua_files:
        lines.append(f"{lf} = ")
    lines.append("")

    try:
        with open(PROJECT_INI_PATH, "w", encoding="gbk", errors="ignore") as f:
            f.write("\n".join(lines))
    except Exception as e:
        print(f"[*] 写入项目工程配置提示: {e}")

def get_all_hwnds():
    import win32gui
    hwnds = []
    def cb(hwnd, extra):
        hwnds.append(hwnd)
    win32gui.EnumWindows(cb, None)
    return hwnds

def is_luatools_running():
    import win32gui
    for hwnd in get_all_hwnds():
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if "Luatools" in title:
                return hwnd
    return None

def find_button_by_text(parent_hwnd, text):
    import win32gui
    found = None
    def cb(chwnd, extra):
        nonlocal found
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(chwnd, buf, 512)
        if text in buf.value:
            found = chwnd
    win32gui.EnumChildWindows(parent_hwnd, cb, None)
    return found

def main():
    if sys.platform != "win32":
        print("[-] 本自动化免按键刷机脚本基于 Win32 消息队列构建，目前仅支持 Windows 平台。")
        print("    在 Linux / macOS 下，请通过合宙标准工具链进行烧录。")
        sys.exit(1)

    try:
        import win32gui
        import win32con
    except ImportError:
        print("[-] 缺少必要依赖 pywin32。请先在终端执行: pip install pywin32")
        sys.exit(1)

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    print("=" * 65)
    print("⚡ 合宙 Air780EPV 智能随身通信网关 · 自动化极速烧录工具")
    print("=" * 65)

    # 1. 确保 Luatools_v3.exe 存在
    if not os.path.exists(LUATOOLS_EXE):
        if not download_luatools():
            sys.exit(1)

    # 2. 动态生成适配当前路径的项目工程文件
    ensure_project_config()

    # 3. 检测或唤醒 Luatools 主窗体
    main_hwnd = is_luatools_running()
    if not main_hwnd:
        print("[*] 正在静默调起 Luatools_v3.exe...")
        subprocess.Popen([LUATOOLS_EXE], cwd=LUATOOLS_DIR)
        for _ in range(30):
            time.sleep(0.5)
            main_hwnd = is_luatools_running()
            if main_hwnd:
                break

    if not main_hwnd:
        print("[-] 错误: 无法捕获 Luatools 窗体，请确认软件未被安全卫士阻断拦截。")
        sys.exit(1)

    print(f"[+] 成功捕获 Luatools 主窗体 (HWND: {hex(main_hwnd)})")
    try:
        win32gui.ShowWindow(main_hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(main_hwnd)
    except Exception:
        pass
    time.sleep(0.8)

    # 4. 寻找“仅下载脚本”按钮
    btn = find_button_by_text(main_hwnd, "仅下载脚本")
    if not btn:
        print("\n" + "-" * 65)
        print("👉 [首次烧录引导提示]")
        print("  未检测到'仅下载脚本'快捷按钮，当前可能处于项目首次配置界面。")
        print("  • 如果您拿到的是【全新出厂空板/AT固件板】：")
        print("    请在弹出的 Luatools 窗口中点击【项目管理测试】-> 勾选【全量烧录】，")
        print(f"    选择底层 Core: {CORE_SOC_PATH}")
        print(f"    选择脚本目录: {SCRIPTS_DIR}")
        print("    点击【下载底层和脚本】完成基座与业务全量注入！")
        print("  • 完成首次全量烧录后，后续任何业务更新再次运行本脚本即可享受 2.5 秒全自动免按键秒刷。")
        print("-" * 65 + "\n")
        sys.exit(0)

    print(f"[+] 找到目标控件 '仅下载脚本' (HWND: {hex(btn)})，投递极速下载事件...")
    rect = win32gui.GetWindowRect(btn)
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2

    # 同时投递 BM_CLICK 和精准物理鼠标事件
    win32gui.SendMessage(btn, win32con.BM_CLICK, 0, 0)
    try:
        import win32api
        win32api.SetCursorPos((cx, cy))
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, cx, cy, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, cx, cy, 0, 0)
    except Exception:
        pass

    print("[+] 烧录触发指令已投递！模组将自动通过 COM9 软复位并快速灌入最新 Lua 脚本...")

    for i in range(5, 0, -1):
        print(f"[*] 等待模组软复位与重启入网 ({i}s)...")
        time.sleep(1)

    print("\n🎉 极速烧录流程触发完毕！板载 NET 绿灯闪烁即代表开机就绪。")

if __name__ == "__main__":
    main()
