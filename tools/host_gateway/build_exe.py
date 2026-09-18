#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780EPV 智能随身通信网关 - Windows 独立 exe 打包构建脚本
使用 PyInstaller 将网关 Hub、Web 服务端、前端看板与系统托盘打包为单文件绿色免安装 exe。
"""

import os
import sys
import shutil
import subprocess
import argparse

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))
ENTRY_SCRIPT = os.path.join(CURRENT_DIR, "gateway_app.py")
WEB_DIR = os.path.join(CURRENT_DIR, "web")
ICON_PATH = os.path.join(CURRENT_DIR, "app.ico")
DIST_DIR = os.path.join(CURRENT_DIR, "dist")
BUILD_DIR = os.path.join(CURRENT_DIR, "build")

LUA_SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "deploy", "smart-gateway-780epv")
FLASHER_DIR = os.path.join(PROJECT_ROOT, "tools", "flasher")
DUMMY_BIN = os.path.join(PROJECT_ROOT, "tools", "Luatools", "_temp", "dummy.bin")

def build(output_dir=None, build_info=None):
    dist_dir = os.path.join(output_dir, "dist") if output_dir else DIST_DIR
    build_dir = os.path.join(output_dir, "work") if output_dir else BUILD_DIR
    if output_dir:
        os.makedirs(output_dir, exist_ok=False)
    print("=" * 60)
    print("  开始构建 Air780EPV-Gateway Windows 独立分发版 (.exe)")
    print("=" * 60)

    # 1. 检查必要文件
    if not os.path.exists(ENTRY_SCRIPT):
        print(f"[-] 错误: 未找到入口脚本: {ENTRY_SCRIPT}")
        sys.exit(1)
    if not os.path.exists(WEB_DIR):
        print(f"[-] 错误: 未找到 Web 目录: {WEB_DIR}")
        sys.exit(1)

    # 2. 构建 PyInstaller 参数
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name", "Air780EPV-Gateway",
        "--distpath", dist_dir,
        "--workpath", build_dir,
        "--specpath", output_dir or CURRENT_DIR,
        f"--add-data={WEB_DIR};web",
        f"--add-data={ICON_PATH};.",
        f"--add-data={LUA_SCRIPTS_DIR};lua_scripts",
        f"--add-data={FLASHER_DIR};flasher",
        f"--add-data={DUMMY_BIN};.",
        "--hidden-import=pystray._win32",
        "--hidden-import=serial.tools.list_ports",
        "--hidden-import=PIL.ImageDraw",
        "--hidden-import=PIL.Image",
    ]
    if build_info:
        cmd.append(f"--add-data={build_info};.")

    if os.path.exists(ICON_PATH):
        cmd.append(f"--icon={ICON_PATH}")

    cmd.append(ENTRY_SCRIPT)

    print("[*] 执行打包指令:")
    print(" ".join(cmd))
    print("-" * 60)

    res = subprocess.run(cmd, cwd=CURRENT_DIR)
    if res.returncode != 0:
        print(f"[-] 打包失败，退出码: {res.returncode}")
        sys.exit(res.returncode)

    output_exe = os.path.join(dist_dir, "Air780EPV-Gateway.exe")
    if os.path.exists(output_exe):
        size_mb = os.path.getsize(output_exe) / (1024 * 1024)
        print("=" * 60)
        print(f"[+] 打包成功！独立可执行文件已生成:")
        print(f"   路径: {output_exe}")
        print(f"   体积: {size_mb:.2f} MB")
        print(f"   说明: 单文件绿色免安装，双击即可在后台常驻并托盘运行。")
        print("=" * 60)
    else:
        print("[-] 未在预期目录找到输出文件")
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir")
    parser.add_argument("--build-info")
    args = parser.parse_args()
    build(os.path.abspath(args.output_dir) if args.output_dir else None, args.build_info)
