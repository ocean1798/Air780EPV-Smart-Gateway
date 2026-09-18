#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780 系列智能通信网关 - LuaDB 脚本打包器与静态语法校验器
负责将 deploy/smart-gateway-780epv/ 源码安全编译并组装为符合合宙与移芯底层规范的：
1. 标准 LuaDB 脚本镜像 (script.bin，用于底层 FlashTool 物理烧录于 0x00324000)
2. 标准 SOTA 容器升级包 (script_ota.bin，用于应用层串口/HTTP 注入流式热更)

具备三层安全防线：
- 静态语法预检防线：扫描所有 .lua 文件词法/语法，阻断坏包下发防止板端 Reboot Loop 死锁；
- 根头与尾部校验防线：24 字节 Tag 03 根头与 16 字节 .airm2m_all_crc#.bin MD5 尾部闭环；
- 容器结构防线：SOTA 封装校验 92 字节 CoreUpgrade 头部 (0xeac37218) 与 CRC32。
"""

import os
import sys
import re
import struct
import binascii
import hashlib
import tempfile
import subprocess
import json
from typing import Tuple, List, Dict, Optional, Any
from typing import Tuple, List, Dict, Optional

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, "..", ".."))

def get_bundle_resource_dir(sub_name: str, fallback_path: str) -> str:
    """优先从 PyInstaller 解压目录获取内嵌资源，开发模式下回退工程源码路径"""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        bundled = os.path.join(meipass, sub_name)
        if os.path.exists(bundled):
            return bundled
    return fallback_path

DEFAULT_SCRIPTS_DIR = get_bundle_resource_dir("lua_scripts", os.path.join(PROJECT_ROOT, "deploy", "smart-gateway-780epv"))
LUAC_EXE = get_bundle_resource_dir(os.path.join("tools", "luac_536.exe"), os.path.join(PROJECT_ROOT, "tools", "Luatools", "_temp", "tools", "luac_536.exe"))
SOC_TOOLS_EXE = get_bundle_resource_dir(os.path.join("tools", "soc_tools.exe"), os.path.join(PROJECT_ROOT, "tools", "Luatools", "_temp", "tools", "soc_tools.exe"))
DUMMY_BIN = get_bundle_resource_dir("dummy.bin", os.path.join(PROJECT_ROOT, "tools", "Luatools", "_temp", "dummy.bin"))

CORE_SCRIPTS = [
    "main.lua",
    "config.lua",
    "model.lua",
    "led.lua",
    "serial_comm.lua",
    "sms_service.lua",
    "call_service.lua",
    "notify_service.lua",
    "storage_service.lua",
    "fota_service.lua",
    "reboot_service.lua"
]

FOTA_MAGIC = 0xeac37218
FLASH_SCRIPT_ADDR = 0x00324000

VERSION_MANIFEST_PATH = os.path.join(PROJECT_ROOT, "tools", "fota", "version.json")

def get_version_manifest() -> Dict[str, Any]:
    """从工程 tools/fota/version.json 读取版本事实源元数据"""
    if os.path.exists(VERSION_MANIFEST_PATH):
        try:
            with open(VERSION_MANIFEST_PATH, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            pass
    return {"version": "1.2.7", "changelog": "优化弱信号重连稳定性与长短信防重发", "size": 21992}


class LuaSyntaxError(Exception):
    """Lua 语法检查未通过异常"""
    pass

def validate_lua_syntax(scripts_dir: str = DEFAULT_SCRIPTS_DIR) -> Tuple[bool, List[str]]:
    """
    静态扫描脚本目录下的核心 Lua 文件，检测是否存在语法错误。
    若存在 luac_536.exe，则调用 luac -p 执行精确语法检查；
    若不可用，则执行基础括号/字符串配对与关键字语法预检。
    """
    errors = []
    has_luac = os.path.exists(LUAC_EXE)

    for fn in CORE_SCRIPTS:
        fp = os.path.join(scripts_dir, fn)
        if not os.path.exists(fp):
            errors.append(f"核心脚本缺失: {fn}")
            continue

        if has_luac:
            cmd = f'"{LUAC_EXE}" -p "{fp}"'
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if res.returncode != 0:
                err_msg = (res.stderr or res.stdout).strip()
                errors.append(f"{fn} 语法错误: {err_msg}")
        else:
            # 纯 Python 词法/括号基础校验
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            # 检查成对括号
            for open_c, close_c in [("(", ")"), ("[", "]"), ("{", "}")]:
                if content.count(open_c) != content.count(close_c):
                    errors.append(f"{fn}: 括号不匹配 '{open_c}' 与 '{close_c}' (数量: {content.count(open_c)} vs {content.count(close_c)})")
            # 检查 block 关键字 (function, if, do, for, while vs end)
            tokens = re.findall(r'\b(function|if|do|for|while|then|end)\b', content)
            block_starts = sum(1 for t in tokens if t in ("function", "then", "do"))
            block_ends = sum(1 for t in tokens if t == "end")
            if block_starts != block_ends:
                errors.append(f"{fn}: 语句块 block / end 数量不匹配 (起始: {block_starts}, end: {block_ends})")

    return (len(errors) == 0, errors)

def strip_lua_code(code_str: str) -> str:
    """剔除多余行注释与多余空行以压缩源码体积，节约板端 RAM"""
    lines = []
    for line in code_str.splitlines():
        trimmed = line.strip()
        # 忽略纯单行注释行（保留非注释或内嵌代码）
        if trimmed.startswith("--") and not trimmed.startswith("--[["):
            continue
        if trimmed:
            lines.append(line)
    return "\n".join(lines)

def pack_luadb(
    scripts_dir: str = DEFAULT_SCRIPTS_DIR,
    target_version: Optional[str] = None,
    magic: Optional[int] = None,
    base_addr: Optional[int] = None,
    **kwargs
) -> bytes:
    """
    将 scripts_dir 下的核心脚本打包为标准的合宙 LuaDB 二进制镜像。
    包含 24 字节根头、文件条目及尾部 .airm2m_all_crc#.bin MD5 校验码。
    magic 与 base_addr 为底层线刷工具调用预留（标准的 LuaDB 具有固定 TLV 根头）。
    """
    valid, errors = validate_lua_syntax(scripts_dir)
    if not valid:
        raise LuaSyntaxError(f"打包前语法校验未通过:\n" + "\n".join(errors))

    has_luac = os.path.exists(LUAC_EXE)
    if not target_version:
        target_version = get_version_manifest().get("version", "1.2.7")
    file_entries: List[Tuple[str, bytes]] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for fn in CORE_SCRIPTS:
            src_fp = os.path.join(scripts_dir, fn)
            with open(src_fp, "r", encoding="utf-8", errors="ignore") as fp:
                src_code = fp.read()

            # 动态替换代码中的版本号字符串
            if target_version:
                src_code = re.sub(r'(_G\.GATEWAY_VERSION|VERSION)\s*=\s*"[^"]+"', f'\\1 = "{target_version}"', src_code)

            if has_luac:
                out_lua_tmp = os.path.join(tmpdir, fn)
                with open(out_lua_tmp, "w", encoding="utf-8") as tfp:
                    tfp.write(src_code)

                out_luac = os.path.join(tmpdir, fn.replace(".lua", ".luac"))
                cmd = f'"{LUAC_EXE}" -s -o "{out_luac}" "{out_lua_tmp}"'
                res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                if res.returncode != 0 or not os.path.exists(out_luac):
                    raise RuntimeError(f"编译 {fn} 失败: {res.stderr or res.stdout}")

                with open(out_luac, "rb") as c_fp:
                    compiled_bytes = c_fp.read()
                file_entries.append((fn.replace(".lua", ".luac"), compiled_bytes))
            else:
                # 无外部 luac 时使用轻量源码剥离注释
                stripped_code = strip_lua_code(src_code)
                file_entries.append((fn, stripped_code.encode("utf-8")))

    buf = bytearray()

    # 1. 根头部 (24 字节)
    # Magic(6B): 01 04 5a a5 5a a5
    # Tag 02 (版本=2, len=2): 02 00
    # Tag 03 (头部长度=24, len=4): 18 00 00 00  (注意十六进制 0x18 = 24)
    # Tag 04 (总文件数=len+1, len=2)
    # Tag fe (累加校验和, len=2)
    root_hdr = bytearray(b"\x01\x04\x5a\xa5\x5a\xa5\x02\x02\x02\x00\x03\x04\x18\x00\x00\x00\x04\x02")
    file_count = len(file_entries) + 1  # 包含尾部校验文件
    root_hdr.extend(struct.pack("<H", file_count))
    root_hdr.extend(b"\xfe\x02")
    root_hdr.extend(struct.pack("<H", sum(root_hdr) & 0xFFFF))
    assert len(root_hdr) == 24, f"根头长度异常: {len(root_hdr)}"
    buf.extend(root_hdr)

    # 2. 依次写入各文件记录
    for name, content in file_entries:
        name_bytes = name.encode("ascii")
        f_hdr = bytearray(b"\x01\x04\x5a\xa5\x5a\xa5")
        f_hdr.append(2)
        f_hdr.append(len(name_bytes))
        f_hdr.extend(name_bytes)
        f_hdr.extend(b"\x03\x04")
        f_hdr.extend(struct.pack("<I", len(content)))
        f_hdr.extend(b"\xfe\x02")
        f_hdr.extend(struct.pack("<H", sum(f_hdr) & 0xFFFF))
        buf.extend(f_hdr)
        buf.extend(content)

    # 3. 写入尾部 .airm2m_all_crc#.bin MD5 签名文件 (16 字节)
    crc_name = b".airm2m_all_crc#.bin"
    crc_hdr = bytearray(b"\x01\x04\x5a\xa5\x5a\xa5")
    crc_hdr.append(2)
    crc_hdr.append(len(crc_name))
    crc_hdr.extend(crc_name)
    crc_hdr.extend(b"\x03\x04")
    crc_hdr.extend(struct.pack("<I", 16))
    crc_hdr.extend(b"\xfe\x02")
    crc_hdr.extend(struct.pack("<H", sum(crc_hdr) & 0xFFFF))
    buf.extend(crc_hdr)

    # 计算此前所有数据的 MD5 摘要并追加
    md5_hash = hashlib.md5(buf).digest()
    buf.extend(md5_hash)

    # 4. 末尾填充至标准扇区对齐 (64KB 上限防护)
    total_len = len(buf)
    if total_len > 65536:
        raise ValueError(f"LuaDB 体积超标: {total_len} 字节 > 65536 字节 (64KB 上限)!")
    buf.extend(b"\x00" * (65536 - total_len))

    return bytes(buf)

def pack_sota_package(raw_luadb_bytes: bytes, target_version: Optional[str] = None, chip_type: str = "ec718") -> Tuple[bytes, Dict]:
    if not target_version:
        target_version = get_version_manifest().get("version", "1.2.9")
    """
    将标准 LuaDB 镜像封装为符合移芯 EC718/EC618 规范的 SOTA 容器升级包。
    - EC718/EC718P/EC718PV: Magic 0xeac37218, 基地址 0x00324000
    - EC618/Air780E/Air780EG: Magic 0xeaf18c16, 基地址 0x0024D000
    包含 92 字节 CoreUpgrade 头部与 CRC32 校验。
    """
    if not os.path.exists(SOC_TOOLS_EXE):
        raise FileNotFoundError(f"未找到 soc_tools.exe 工具链: {SOC_TOOLS_EXE}")

    is_ec618 = chip_type.lower() in ("ec618", "air780e", "air780eg")
    magic_hex = "eaf18c16" if is_ec618 else "eac37218"
    magic_num = 0xeaf18c16 if is_ec618 else FOTA_MAGIC
    script_addr = "24D000" if is_ec618 else "324000"

    dummy_bin = DUMMY_BIN
    if not os.path.exists(dummy_bin):
        with open(dummy_bin, "wb") as f:
            pass

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_script_bin = os.path.join(tmpdir, "script.bin")
        tmp_script_zip = os.path.join(tmpdir, "script_fota.zip")
        tmp_output_sota = os.path.join(tmpdir, "output.sota")

        with open(tmp_script_bin, "wb") as f:
            f.write(raw_luadb_bytes)

        # 压缩体并内嵌目标写入基地址
        cmd_zip = f'"{SOC_TOOLS_EXE}" zip_file {magic_hex} {script_addr} "{tmp_script_bin}" "{tmp_script_zip}" 40000 1'
        res_zip = subprocess.run(cmd_zip, shell=True, capture_output=True, text=True)
        if res_zip.returncode != 0 or not os.path.exists(tmp_script_zip):
            raise RuntimeError(f"soc_tools zip_file 失败: {res_zip.stderr or res_zip.stdout}")

        # 装配 92 字节 CoreUpgrade 头部
        cmd_ota = f'"{SOC_TOOLS_EXE}" make_ota_file {magic_hex} 0 0 0 0 0 "{tmp_script_zip}" "{dummy_bin}" "{tmp_output_sota}"'
        res_ota = subprocess.run(cmd_ota, shell=True, capture_output=True, text=True)
        if res_ota.returncode != 0 or not os.path.exists(tmp_output_sota):
            raise RuntimeError(f"soc_tools make_ota_file 失败: {res_ota.stderr or res_ota.stdout}")

        with open(tmp_output_sota, "rb") as f:
            ota_package = f.read()

        # 校验生成的 SOTA 结构
        assert len(ota_package) >= 96, "生成的 SOTA 升级包长度异常"
        magic, header_crc32 = struct.unpack("<II", ota_package[:8])
        assert magic == magic_num, f"SOTA 头部 Magic 异常: 0x{magic:08x} != 0x{magic_num:08x}"
        calc_crc32 = (~binascii.crc32(ota_package[8:92])) & 0xffffffff
        assert header_crc32 == calc_crc32, f"SOTA 头部 CRC32 异常: 0x{header_crc32:08x} != 0x{calc_crc32:08x}"

        meta = {
            "header_magic": magic_num,
            "header_crc32": header_crc32,
            "chip_type": "ec618" if is_ec618 else "ec718",
            "common_len": len(ota_package) - 92,
            "total_len": len(ota_package),
            "package_md5": hashlib.md5(ota_package).hexdigest(),
            "version": target_version
        }
        return ota_package, meta


if __name__ == "__main__":
    print("⚡ 测试运行 Lua 语法预检与 LuaDB 打包器...")
    ok, errs = validate_lua_syntax()
    if not ok:
        print("[-] 语法预检失败:")
        for e in errs:
            print("  -", e)
        sys.exit(1)
    print("[+] 语法预检 100% 通过！")

    bin_data = pack_luadb()
    print(f"[+] 成功打包标准 LuaDB (大小: {len(bin_data)} 字节, MD5: {hashlib.md5(bin_data).hexdigest()})")

    sota_data, sota_meta = pack_sota_package(bin_data)
    print(f"[+] 成功封装标准 SOTA 容器包 (大小: {sota_meta['total_len']} 字节, Magic: 0x{sota_meta['header_magic']:08x}, MD5: {sota_meta['package_md5']})")
