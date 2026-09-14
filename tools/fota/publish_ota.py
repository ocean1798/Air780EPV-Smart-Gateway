#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AIR-18: Air780EPV 固件空中热更新 (FOTA) 一键自动打包与七牛云发布工具
支持从 deploy/smart-gateway-780epv/ 源码一键编译 Stripped 字节码并打包标准 LuaDB，
或直接使用已编译的 script.bin，全自动生成符合 EC718P 规范的 SOTA 升级包并同步至 CDN。
"""

import os
import sys
import time
import json
import hashlib
import struct
import binascii
import re
import argparse
import tempfile
import subprocess
from datetime import datetime
import urllib.request
import qiniu

WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
CONFIG_PATH = os.path.join(WORKSPACE_DIR, "tools/qiniu_config.json")
DEFAULT_BIN_PATH = os.path.join(WORKSPACE_DIR, "tools/Luatools/_temp/ec_download/script.bin")
SOC_TOOLS_EXE = os.path.join(WORKSPACE_DIR, "tools/Luatools/_temp/tools/soc_tools.exe")
LUAC_EXE = os.path.join(WORKSPACE_DIR, "tools/Luatools/_temp/tools/luac_536.exe")
DUMMY_BIN_PATH = os.path.join(WORKSPACE_DIR, "tools/Luatools/_temp/dummy.bin")
SMART_GATEWAY_SRC_DIR = os.path.join(WORKSPACE_DIR, "deploy/smart-gateway-780epv")
OUTPUT_OTA_BIN = os.path.join(os.path.dirname(__file__), "script_ota.bin")
OUTPUT_VERSION_JSON = os.path.join(os.path.dirname(__file__), "version.json")

DEPLOY_LUA_FILES = [
    "call_service.lua",
    "config.lua",
    "fota_service.lua",
    "led.lua",
    "main.lua",
    "model.lua",
    "notify_service.lua",
    "reboot_service.lua",
    "serial_comm.lua",
    "sms_service.lua",
    "storage_service.lua"
]

def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"七牛云配置文件不存在: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def get_file_md5(filepath):
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

def compile_source_to_luadb(src_dir: str, target_version: str = None) -> bytes:
    """
    将部署目录下的 Lua 源文件使用 luac_536.exe -s (Stripped) 编译为无调试符号的 luac 字节码，
    并组装成符合合宙标准规范的 64KB LuaDB 二进制包。
    """
    if not os.path.exists(LUAC_EXE):
        raise FileNotFoundError(f"未找到 luac_536.exe: {LUAC_EXE}")
    if not os.path.exists(src_dir):
        raise FileNotFoundError(f"源码目录不存在: {src_dir}")

    file_entries = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for fn in DEPLOY_LUA_FILES:
            src_path = os.path.join(src_dir, fn)
            if not os.path.exists(src_path):
                raise FileNotFoundError(f"项目必须文件缺失: {src_path}")
            
            # 若传入 target_version，动态临时修补 main.lua、fota_service.lua 与 config.lua 中的版本号定义
            file_to_compile = src_path
            if fn in ("main.lua", "fota_service.lua", "config.lua") and target_version:
                with open(src_path, "r", encoding="utf-8") as sf:
                    content = sf.read()
                if fn == "main.lua":
                    content = re.sub(r'VERSION\s*=\s*"[^"]+"', lambda m: f'VERSION = "{target_version}"', content)
                elif fn == "fota_service.lua":
                    content = re.sub(r'local local_ver = _G\.GATEWAY_VERSION or "[^"]+"', lambda m: f'local local_ver = _G.GATEWAY_VERSION or "{target_version}"', content)
                elif fn == "config.lua":
                    content = re.sub(r'_G\.GATEWAY_VERSION\s*=\s*"[^"]+"', lambda m: f'_G.GATEWAY_VERSION = "{target_version}"', content)
                tmp_src = os.path.join(tmpdir, fn)
                with open(tmp_src, "w", encoding="utf-8") as tf:
                    tf.write(content)
                file_to_compile = tmp_src

            out_luac = os.path.join(tmpdir, fn.replace(".lua", ".luac"))
            cmd = f'"{LUAC_EXE}" -s -o "{out_luac}" "{file_to_compile}"'
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if res.returncode != 0 or not os.path.exists(out_luac):
                raise RuntimeError(f"编译 {fn} 失败: {res.stderr or res.stdout}")
            
            with open(out_luac, "rb") as fp:
                compiled_bytes = fp.read()
            luac_name = fn.replace(".lua", ".luac")
            file_entries.append((luac_name, compiled_bytes))

    # 组装 LuaDB 二进制
    buf = bytearray()
    
    # 1. 根头部 (24 字节)
    # Magic(6B): 01 04 5a a5 5a a5
    # Tag 02 (版本=2, len=2): 02 00
    # Tag 03 (头部长度=24, len=4): 18 00 00 00
    # Tag 04 (总文件数=len+1, len=2)
    # Tag fe (累加校验和, len=2)
    root_hdr = bytearray(b"\x01\x04\x5a\xa5\x5a\xa5\x02\x02\x02\x00\x03\x04\x18\x00\x00\x00\x04\x02")
    file_count = len(file_entries) + 1 # 包含末尾 .airm2m_all_crc#.bin
    root_hdr.extend(struct.pack("<H", file_count))
    root_hdr.extend(b"\xfe\x02")
    root_hdr.extend(struct.pack("<H", sum(root_hdr) & 0xFFFF))
    assert len(root_hdr) == 24
    buf.extend(root_hdr)

    # 2. 依次写入各 luac 文件记录
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

    # 3. 写入 .airm2m_all_crc#.bin 记录 (16 字节原始 MD5)
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

    # 计算此前所有数据的 MD5
    md5_hash = hashlib.md5(buf).digest()
    buf.extend(md5_hash)

    # 4. 末尾用 0x00 填充至标准的 65536 字节 (64KB)
    total_len = len(buf)
    if total_len > 65536:
        raise ValueError(f"LuaDB 字节数超标: {total_len} 字节 > 65536 字节 (64KB Flash 上限)!")
    buf.extend(b"\x00" * (65536 - total_len))

    print(f"  [Compile] 成功编译并组装标准 LuaDB: 包含 {len(file_entries)} 个模块, 有效负载 {total_len} 字节 (剩余安全裕量 {65536 - total_len} 字节)")
    return bytes(buf)

def build_fota_package(raw_bytes: bytes, target_version: str) -> tuple[bytes, dict]:
    """
    接收 LuaDB 纯二进制映像，封装成标准的 EC718/EC718P/EC718PV 纯脚本 FOTA 升级包 (.sota)。
    遵循 EC718 底层 Bootloader & FOTA 规范:
      1. 内部脚本版本号同步与 .airm2m_all_crc# MD5 重算校验
      2. 调用 soc_tools.exe zip_file eac37218 324000 <script.bin> <script_fota.zip> 40000 1 生成 LZMA 压缩包并内嵌目标写入基地址 0x00324000
      3. 调用 soc_tools.exe make_ota_file eac37218 0 0 0 0 0 <script_fota.zip> <dummy.bin> <output.sota> 装配 92 字节 CoreUpgrade 头部
      4. 严格自检 92 字节头部 MagicNum (0xeac37218) 与 CRC32 校验和
    """
    FOTA_MAGIC = 0xeac37218

    # 1. 若传入已携带 92 字节 CoreUpgrade 头部且负载后也为合规 script_fota.zip (0xeac37218)，直接校验返回
    if len(raw_bytes) >= 96:
        magic = struct.unpack("<I", raw_bytes[:4])[0]
        body_magic = struct.unpack("<I", raw_bytes[92:96])[0]
        if magic == FOTA_MAGIC and body_magic == FOTA_MAGIC:
            stored_crc = struct.unpack("<I", raw_bytes[4:8])[0]
            calc_crc = (~binascii.crc32(raw_bytes[8:92])) & 0xffffffff
            if stored_crc == calc_crc:
                print("  [Check] 输入文件已包含合规 CoreUpgrade SOTA 头部 (0xeac37218, CRC32 及 Body Magic 均通过)")
                return raw_bytes, {
                    "header_magic": FOTA_MAGIC,
                    "header_crc32": stored_crc,
                    "common_len": len(raw_bytes) - 92,
                    "total_len": len(raw_bytes),
                    "package_md5": hashlib.md5(raw_bytes).hexdigest()
                }

    # 2. 校验 LuaDB Magic (0x01 0x04 0x5a 0xa5 0x5a 0xa5)
    if raw_bytes[:6] != b"\x01\x04\x5a\xa5\x5a\xa5":
        print(f"  [Warning] 提示: 固件二进制头部非标准 LuaDB Magic: {raw_bytes[:6].hex()}")
    else:
        print("  [Check] 二进制结构校验通过: 确认标准 LuaDB 脚本镜像")

    body = bytearray(raw_bytes)

    # 3. 固件内部版本号动态匹配校准
    target_ver_bytes = target_version.encode("ascii")
    crc_idx = body.find(b".airm2m_all_crc#.bin")
    if crc_idx != -1 and len(target_ver_bytes) == 5:
        old_ver_pattern = rb'(?<!\d)(?:1\.[0-9]\.[0-9])(?!\d)'
        old_ver_matches = [m.start() for m in re.finditer(old_ver_pattern, bytes(body[:crc_idx]))]
        updated_count = 0
        for pos in old_ver_matches:
            if body[pos:pos+5] != target_ver_bytes:
                body[pos:pos+5] = target_ver_bytes
                updated_count += 1
        if updated_count > 0:
            print(f"  [Patch] 固件内部版本标识同步替换: {updated_count} 处版本串已更新为 v{target_version}")
            airm2m_md5_offset = crc_idx + 30
            new_airm2m_md5 = hashlib.md5(body[:airm2m_md5_offset]).digest()
            body[airm2m_md5_offset : airm2m_md5_offset + 16] = new_airm2m_md5
            print(f"  [Patch] LuaDB 末尾 .airm2m_all_crc# MD5 重新校验通过: {new_airm2m_md5.hex()}")

    # 4. 调用 soc_tools.exe 工具链生成符合底层规范的 script_fota.zip 与 output.sota
    if not os.path.exists(SOC_TOOLS_EXE):
        raise FileNotFoundError(f"未找到 soc_tools 编译工具: {SOC_TOOLS_EXE}")

    dummy_bin = DUMMY_BIN_PATH
    if not os.path.exists(dummy_bin):
        with open(dummy_bin, "wb") as f:
            pass

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_script_bin = os.path.join(tmpdir, "script_patched.bin")
        tmp_script_zip = os.path.join(tmpdir, "script_fota.zip")
        tmp_output_sota = os.path.join(tmpdir, "output.sota")

        with open(tmp_script_bin, "wb") as f:
            f.write(body)

        # 压缩体并内嵌目标刷写物理基址 0x00324000 的 script_fota.zip
        cmd_zip = f'"{SOC_TOOLS_EXE}" zip_file eac37218 324000 "{tmp_script_bin}" "{tmp_script_zip}" 40000 1'
        res_zip = subprocess.run(cmd_zip, shell=True, capture_output=True, text=True)
        if res_zip.returncode != 0 or not os.path.exists(tmp_script_zip):
            raise RuntimeError(f"soc_tools zip_file 执行失败 (code {res_zip.returncode}): {res_zip.stderr or res_zip.stdout}")

        # 装配并封装携带 92 字节 CoreUpgrade 头的标准 .sota 升级包
        cmd_ota = f'"{SOC_TOOLS_EXE}" make_ota_file eac37218 0 0 0 0 0 "{tmp_script_zip}" "{dummy_bin}" "{tmp_output_sota}"'
        res_ota = subprocess.run(cmd_ota, shell=True, capture_output=True, text=True)
        if res_ota.returncode != 0 or not os.path.exists(tmp_output_sota):
            raise RuntimeError(f"soc_tools make_ota_file 执行失败 (code {res_ota.returncode}): {res_ota.stderr or res_ota.stdout}")

        with open(tmp_output_sota, "rb") as f:
            ota_package = f.read()

    # 5. 严格自检生成的 OTA 升级包
    assert len(ota_package) >= 96, "生成的 SOTA 升级包长度异常"
    magic, header_crc32 = struct.unpack("<II", ota_package[:8])
    assert magic == FOTA_MAGIC, f"生成的 SOTA 头部 Magic 异常: 0x{magic:08x}"
    calc_crc32 = (~binascii.crc32(ota_package[8:92])) & 0xffffffff
    assert header_crc32 == calc_crc32, f"生成的 SOTA 头部 CRC32 异常: 0x{header_crc32:08x} != 0x{calc_crc32:08x}"
    body_magic = struct.unpack("<I", ota_package[92:96])[0]
    assert body_magic == FOTA_MAGIC, f"生成的 SOTA 负载 Magic 异常: 0x{body_magic:08x}"

    common_len = len(ota_package) - 92
    common_md5 = struct.unpack("<16s", ota_package[60:76])[0].hex()
    package_md5 = hashlib.md5(ota_package).hexdigest()

    print(f"  [Pack] EC718P 标准 SOTA 升级包生成完毕:")
    print(f"         Magic: 0x{FOTA_MAGIC:08x} | CRC32: 0x{header_crc32:08x}")
    print(f"         CommonDataLen (script_fota.zip): {common_len} 字节 | CommonMD5: {common_md5}")
    print(f"         OTA 总包大小: {len(ota_package)} 字节 | 整包 MD5: {package_md5}")

    return ota_package, {
        "header_magic": FOTA_MAGIC,
        "header_crc32": header_crc32,
        "common_len": common_len,
        "common_md5": common_md5,
        "total_len": len(ota_package),
        "package_md5": package_md5
    }

def upload_to_qiniu(cfg, local_path, key_name):
    q = qiniu.Auth(cfg["access_key"], cfg["secret_key"])
    token = q.upload_token(cfg["bucket_name"], key_name, 3600)
    ret, info = qiniu.put_file(token, key_name, local_path)
    if info.status_code != 200:
        raise RuntimeError(f"七牛云上传失败 [{key_name}]: {info.error} (Status: {info.status_code})")
    print(f"  [OK] 上传成功: {key_name} -> Hash: {ret.get('hash')}")
    return ret

def verify_public_url(url):
    print(f"  [Probe] 探测公开直链: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Air780EPV-FOTA-Probe/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"直链探测失败，HTTP状态码: {resp.status}")
        data = resp.read()
        print(f"  [OK] 直链可达 (200 OK, 响应字节数: {len(data)})")
        return data

def main():
    parser = argparse.ArgumentParser(description="Air780EPV FOTA 一键自动打包与发布")
    parser.add_argument("--version", "-v", required=True, help="发布的目标固件版本号 (例如 1.2.2)")
    parser.add_argument("--src-dir", "-s", default=SMART_GATEWAY_SRC_DIR, help=f"源码目录，从源码直接编译并打包 (默认: {SMART_GATEWAY_SRC_DIR})")
    parser.add_argument("--bin", "-b", default=None, help=f"指定现成 script.bin 路径 (若指定则跳过源码编译，直接打包此 bin)")
    parser.add_argument("--changelog", "-c", default="功能更新与底层系统优化", help="版本更新日志")
    parser.add_argument("--dry-run", action="store_true", help="仅本地编译生成与验证，不实际推流上传")
    args = parser.parse_args()

    print("=" * 65)
    print(f" Air780EPV FOTA 自动化发布流水线 - 目标版本: v{args.version}")
    print("=" * 65)

    cfg = load_config()
    domain = cfg["domain"].rstrip("/")

    # 1. 编译或加载二进制
    if args.bin:
        print(f">>> 使用外部预编译二进制文件: {args.bin}")
        if not os.path.exists(args.bin):
            raise FileNotFoundError(f"未找到固件文件: {args.bin}")
        with open(args.bin, "rb") as f:
            raw_bin_data = f.read()
    else:
        print(f">>> 从项目源码目录编译 Stripped 字节码并组装 LuaDB...")
        raw_bin_data = compile_source_to_luadb(args.src_dir, target_version=args.version)

    # 2. 封装为标准 EC718P SOTA 包
    ota_pkg_bytes, diag = build_fota_package(raw_bin_data, args.version)

    # 3. 写入本地产物
    with open(OUTPUT_OTA_BIN, "wb") as f:
        f.write(ota_pkg_bytes)
    print(f"  [Save] 本地生成合规 CoreUpgrade OTA 升级包: {OUTPUT_OTA_BIN} ({len(ota_pkg_bytes)} 字节)")

    bin_size = len(ota_pkg_bytes)
    bin_md5 = diag["package_md5"]

    bin_url = f"{domain}/script.bin"
    version_data = {
        "project": "smart_cellular_gateway",
        "version": args.version,
        "build_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "url": bin_url,
        "size": bin_size,
        "md5": bin_md5,
        "changelog": args.changelog
    }

    with open(OUTPUT_VERSION_JSON, "w", encoding="utf-8") as f:
        json.dump(version_data, f, ensure_ascii=False, indent=2)
    print(f"  [Build] 本地生成版本元数据: {OUTPUT_VERSION_JSON}")
    print(f"          版本: v{args.version} | 大小: {bin_size} 字节 | MD5: {bin_md5}")

    if args.dry_run:
        print("\n[Dry-run 模式完成，未向七牛云推流]")
        return

    print("\n>>> 开始上传至七牛云对象存储 (Bucket: %s, 区域: %s)..." % (cfg["bucket_name"], cfg["region"]))
    upload_to_qiniu(cfg, OUTPUT_OTA_BIN, "script.bin")
    upload_to_qiniu(cfg, OUTPUT_VERSION_JSON, "version.json")

    # 自动刷新七牛云 CDN 缓存
    try:
        auth = qiniu.Auth(cfg["access_key"], cfg["secret_key"])
        cdn_mgr = qiniu.CdnManager(auth)
        refresh_res = cdn_mgr.refresh_urls([f"{domain}/version.json", bin_url])
        print(f"  [CDN] 边缘节点缓存刷新请求已提交: Code {refresh_res[0].get('code')}")
    except Exception as e:
        print(f"  [CDN] 提示: 刷新 CDN 缓存异常: {e}")

    print("\n>>> 执行公网直链存活性校验...")
    v_data = verify_public_url(f"{domain}/version.json")
    parsed = json.loads(v_data.decode("utf-8"))
    assert parsed["version"] == args.version, "探测到的远端版本与发布版本不一致！"

    # 探测 script.bin，截取前 92 字节头部并验证 Magic 与 CRC
    print("  [Probe] 探测固件包公网直链并验证 CoreUpgrade 头部校对...")
    remote_header = None
    expected_crc = diag["header_crc32"]
    for attempt in range(1, 10):
        try:
            req = urllib.request.Request(bin_url, headers={"Range": "bytes=0-91", "User-Agent": "Air780EPV-FOTA-Probe/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status in (200, 206):
                    head = resp.read(92)
                    magic = struct.unpack("<I", head[:4])[0]
                    rcrc = struct.unpack("<I", head[4:8])[0]
                    if magic == 0xeac37218 and rcrc == expected_crc:
                        remote_header = head
                        break
        except Exception:
            pass
        print(f"  [CDN] 等待边缘 CDN 节点同步最新固件... (重试 {attempt}/10)")
        time.sleep(3)

    assert remote_header is not None, f"获取 script.bin 超时或未获取到匹配 CRC 0x{expected_crc:08x} 的最新固件"
    remote_magic = struct.unpack("<I", remote_header[:4])[0]
    assert remote_magic == 0xeac37218, f"远端固件 CoreUpgrade 标志异常: 0x{remote_magic:08x}"
    remote_crc = struct.unpack("<I", remote_header[4:8])[0]
    calc_crc = (~binascii.crc32(remote_header[8:92])) & 0xffffffff
    assert remote_crc == calc_crc, f"远端固件头部 CRC 校验失败: 0x{remote_crc:08x} != 0x{calc_crc:08x}"
    print(f"  [OK] 固件直链验证通过: {bin_url} (CoreUpgrade Header 0xeac37218 & CRC32 0x{remote_crc:08x} Valid)")

    print("\n" + "=" * 65)
    print(f"  FOTA 发布成功！")
    print(f"  版本: v{args.version}")
    print(f"  元数据直链: {domain}/version.json")
    print(f"  固件包直链: {bin_url}")
    print("=" * 65)

if __name__ == "__main__":
    main()
