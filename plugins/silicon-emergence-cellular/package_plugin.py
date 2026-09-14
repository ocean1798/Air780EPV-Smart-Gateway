#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780EPV 随身通信网关插件打包与自检工具
生成符合《硅基涌现》Plugin Platform v4.0 规范的 .se-plugin 安装包
"""

import os
import sys
import json
import zipfile
import hashlib
from pathlib import Path

# 确保 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PLUGIN_SRC = Path(__file__).parent.resolve()
PROJECT_ROOT = PLUGIN_SRC.parent.parent.resolve()
DIST_DIR = PROJECT_ROOT / "dist"
TARGET_PACKAGE = DIST_DIR / "cellular-gateway.se-plugin"

FILES_TO_PACK = [
    "manifest.json",
    "index.mjs",
    "gateway-protocol.mjs",
    "gateway-sdk-bridge.mjs",
    "README.md"
]

def calculate_sha256(filepath: Path) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def package():
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    print(f"📦 开始打包《硅基涌现》通信网关插件...")
    print(f"   源码目录: {PLUGIN_SRC}")
    print(f"   目标产物: {TARGET_PACKAGE}")

    # 1. 检查必要文件
    for fname in ["manifest.json", "index.mjs"]:
        fpath = PLUGIN_SRC / fname
        if not fpath.exists():
            print(f"❌ 缺失必要文件: {fname}")
            sys.exit(1)

    # 2. 验证 manifest JSON 语法
    with open(PLUGIN_SRC / "manifest.json", "r", encoding="utf-8") as f:
        try:
            manifest = json.load(f)
            print(f"✅ manifest.json 校验通过: {manifest.get('pluginId')} v{manifest.get('version')}")
        except Exception as e:
            print(f"❌ manifest.json 语法错误: {e}")
            sys.exit(1)

    # 3. 创建 .se-plugin (ZIP 归档)
    with zipfile.ZipFile(TARGET_PACKAGE, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fname in FILES_TO_PACK:
            fpath = PLUGIN_SRC / fname
            if fpath.exists():
                arcname = fname
                zf.write(fpath, arcname)
                f_sha = calculate_sha256(fpath)
                print(f"   + 添加文件: {arcname} ({fpath.stat().st_size} bytes, sha256: {f_sha[:12]}...)")

    pkg_size = TARGET_PACKAGE.stat().st_size
    pkg_sha = calculate_sha256(TARGET_PACKAGE)
    print(f"\n🎉 打包完成！产物详情:")
    print(f"   文件路径: {TARGET_PACKAGE}")
    print(f"   文件大小: {pkg_size} 字节 ({pkg_size / 1024:.1f} KB)")
    print(f"   SHA-256:  {pkg_sha}")

    return TARGET_PACKAGE

if __name__ == "__main__":
    package()
