#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780 系列智能通信网关 - 上位机多芯片通用固件线刷引擎 (Firmware Flasher Engine)
支持：
1. 在线模组 Script Flash 模式（极速 1~2 秒仅重刷应用业务 script.bin）；
2. 芯片架构支持：EC718PV（Air780EPV/Air780EP）与 EC618（Air780E/Air780EG/Air700E）；
3. 资源自包含：优先使用固化的 tools/flasher 资产，脱离 Luatools 临时目录。
"""

import os
import sys
import time
import shutil
import tempfile
import subprocess
import copy
import json
import hashlib
import configparser
from typing import Optional, Callable, Dict, Any, List
import serial
import serial.tools.list_ports

class _SafeStream:
    """包装标准输出流，防止在 Windows 无控制台进程中抛出 [Errno 22] Invalid argument"""
    def __init__(self, target):
        self.target = target
    def write(self, s):
        try:
            if self.target and hasattr(self.target, "write"):
                self.target.write(s)
        except Exception:
            pass
    def flush(self):
        try:
            if self.target and hasattr(self.target, "flush"):
                self.target.flush()
        except Exception:
            pass
    def reconfigure(self, **kwargs):
        try:
            if self.target and hasattr(self.target, "reconfigure"):
                self.target.reconfigure(**kwargs)
        except Exception:
            pass
    def isatty(self):
        try:
            return self.target.isatty() if self.target and hasattr(self.target, "isatty") else False
        except Exception:
            return False
    def __getattr__(self, name):
        if self.target and hasattr(self.target, name):
            return getattr(self.target, name)
        raise AttributeError(f"'_SafeStream' object has no attribute '{name}'")

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 全局管道保护
sys.stderr = _SafeStream(sys.stderr)
sys.stdout = _SafeStream(sys.stdout)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, "..", ".."))

def get_bundle_resource_dir(sub_name: str, fallback_path: str) -> str:
    """支持 PyInstaller 打包资源与开发源码双路径解析"""
    if getattr(sys, "frozen", False):
        if not hasattr(sys, "_MEIPASS"):
            raise RuntimeError("Frozen environment missing sys._MEIPASS")
        meipass = getattr(sys, "_MEIPASS")
        bundled = os.path.join(meipass, sub_name)
        if not os.path.exists(bundled):
            raise FileNotFoundError(f"Bundled resource directory not found in _MEIPASS: {bundled}")
        return bundled
    return fallback_path

# 固化的独立 flasher 根目录
FLASHER_DIR = get_bundle_resource_dir("flasher", os.path.join(PROJECT_ROOT, "tools", "flasher"))
FLASHER_BIN_DIR = os.path.join(FLASHER_DIR, "bin")
FLASHER_TARGETS_DIR = os.path.join(FLASHER_DIR, "targets")

FLASHTOOL_CLI = os.path.join(FLASHER_BIN_DIR, "FlashToolCLI.exe")

# 向下兼容旧测试套件属性
EC_DOWNLOAD_DIR = os.path.join(PROJECT_ROOT, "tools", "Luatools", "_temp", "ec_download") if os.path.exists(os.path.join(PROJECT_ROOT, "tools", "Luatools", "_temp", "ec_download")) else FLASHER_BIN_DIR
BASE_CFG_PATH = os.path.join(FLASHER_TARGETS_DIR, "ec618", "config_ec618_usb.ini")

# 备用 Luatools 临时目录（若独立资产缺失时向下兼容）
FALLBACK_TEMP_JCT = os.path.join(tempfile.gettempdir(), "luatools_jct", "p21c43db6b88b")
FALLBACK_EC_DIR = os.path.join(PROJECT_ROOT, "tools", "Luatools", "_temp", "ec_download")

if not getattr(sys, "frozen", False) and not os.path.exists(FLASHTOOL_CLI):
    if os.path.exists(os.path.join(FALLBACK_TEMP_JCT, "FlashToolCLI.exe")):
        FLASHER_BIN_DIR = FALLBACK_TEMP_JCT
        FLASHTOOL_CLI = os.path.join(FLASHER_BIN_DIR, "FlashToolCLI.exe")
    elif os.path.exists(os.path.join(FALLBACK_EC_DIR, "FlashToolCLI.exe")):
        FLASHER_BIN_DIR = FALLBACK_EC_DIR
        FLASHTOOL_CLI = os.path.join(FLASHER_BIN_DIR, "FlashToolCLI.exe")

BOOT_VID = 0x17D1
BOOT_PID = 0x0001

# 芯片平台配置矩阵
CHIP_CONFIGS = {
    "ec718pv": {
        "name": "EC718PV",
        "models": ["Air780EPV", "Air780EP"],
        "burnaddr_script": "0x324000",
        "script_magic": 0xeac37218,
        "script_base_addr": 0x00324000,
        "has_full_flash": False,
        "full_imglist": ["bootloader", "system", "cp_system", "flexfile2"],
        "target_dir_name": "ec718pv"
    },
    "ec618": {
        "name": "EC618",
        "models": ["Air780E", "Air780EG", "Air700E"],
        "burnaddr_script": "0x24D000",
        "script_magic": 0xeaf18c16,
        "script_base_addr": 0x0024D000,
        "has_full_flash": False,
        "full_imglist": ["bootloader", "system", "cp_system", "flexfile0", "flexfile1", "flexfile2"],
        "target_dir_name": "ec618"
    }
}

def log(msg: str):
    try:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        sys.stderr.write(f"[{now}] [Flasher] {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass

MODEL_TO_CHIP: Dict[str, str] = {
    "AIR780EPV": "ec718pv",
    "AIR780EP": "ec718pv",
    "AIR780E": "ec618",
    "AIR780EG": "ec618",
    "AIR700E": "ec618",
}
VALID_CHIPS = {"ec718pv", "ec618"}

SCRIPT_FLASH_PROOFS: Dict[str, Dict[str, Any]] = {}

def normalize_chip_type(hardware_model: Optional[str], chip_hint: Optional[str] = None) -> str:
    """根据硬件型号与芯片提示严格归一化为标准的芯片类型 ('ec718pv' 或 'ec618')"""
    normalized_hint = None
    if chip_hint is not None:
        raw_hint = str(chip_hint).strip().lower()
        if raw_hint not in VALID_CHIPS:
            raise ValueError(f"Invalid chip_hint: {chip_hint!r}, expected one of {sorted(VALID_CHIPS)}")
        normalized_hint = raw_hint

    if not hardware_model or not isinstance(hardware_model, str) or not hardware_model.strip():
        raise ValueError("Missing hardware_model for chip type normalization")

    raw_model = hardware_model.strip().upper()
    resolved_chip = MODEL_TO_CHIP.get(raw_model)
    if not resolved_chip:
        raise ValueError(f"Unknown hardware_model: {hardware_model!r}")

    if normalized_hint and resolved_chip != normalized_hint:
        raise ValueError(
            f"Hardware model {hardware_model!r} resolves to {resolved_chip!r}, "
            f"which conflicts with chip_hint {chip_hint!r}"
        )

    return resolved_chip

def _clean_usb_location(loc: Optional[str]) -> Optional[str]:
    """剥除 USB 端口位置中的接口后缀（冒号及之后的部分）"""
    if not loc or not isinstance(loc, str):
        return None
    s = loc.strip()
    if not s:
        return None
    if ":" in s:
        s = s.split(":", 1)[0].strip()
    return s if s else None

def _match_usb_binding(port_info: Any, identity: Dict[str, Any]) -> bool:
    """复用 USB 绑定匹配：位置去除接口后缀后完整相等，serial 完整相等"""
    bound_loc_raw = identity.get("usb_location")
    bound_serial_raw = identity.get("usb_serial")
    bound_loc = _clean_usb_location(bound_loc_raw) if isinstance(bound_loc_raw, str) else None
    bound_serial = bound_serial_raw.strip() if isinstance(bound_serial_raw, str) and bound_serial_raw.strip() else None

    if not bound_loc and not bound_serial:
        return False

    cand_loc = _clean_usb_location(getattr(port_info, "location", None))
    cand_serial_raw = getattr(port_info, "serial_number", None)
    cand_serial = cand_serial_raw.strip() if isinstance(cand_serial_raw, str) and cand_serial_raw.strip() else None

    if bound_loc:
        if cand_loc != bound_loc:
            return False
        if bound_serial and cand_serial:
            if cand_serial != bound_serial:
                return False
        return True
    else:
        if not cand_serial or cand_serial != bound_serial:
            return False
        return True

def find_bootloader_port(identity: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """探测 ROM Bootloader 烧录端口（移芯全系平台 17D1:0001，严格基于已核物理身份唯一匹配）
    :param identity: 捕获的设备物理身份字典，必须包含非空有效 usb_location 或 usb_serial
    """
    if not isinstance(identity, dict):
        return None
    bound_loc = identity.get("usb_location")
    bound_serial = identity.get("usb_serial")
    valid_loc = isinstance(bound_loc, str) and bool(bound_loc.strip())
    valid_serial = isinstance(bound_serial, str) and bool(bound_serial.strip())
    if not valid_loc and not valid_serial:
        return None

    matched_ports: List[str] = []
    for p in serial.tools.list_ports.comports():
        # 仅 VID/PID 精确 17D1:0001 的 Boot 候选，禁止 hwid 文本/hex 猜测
        if getattr(p, "vid", None) == BOOT_VID and getattr(p, "pid", None) == BOOT_PID:
            if _match_usb_binding(p, identity):
                if p.device:
                    matched_ports.append(p.device)

    if len(matched_ports) == 1:
        return matched_ports[0]
    return None

def trigger_soft_reboot_to_boot(current_port: Optional[str] = None, identity: Optional[Dict[str, Any]] = None) -> bool:
    """
    通过底层已捕获控制端口下发 AT+ECRST=delay,799 / rtos.reboot() 与 ~SYNC~
    安全引导指定模组切入 ROM Bootloader 烧录模式
    """
    if not isinstance(identity, dict):
        raise ValueError("identity must be a dict")

    control_port = identity.get("control_port")
    if not isinstance(control_port, str) or not control_port.strip():
        raise ValueError("Missing or invalid control_port in identity")
    control_port = control_port.strip()

    if not current_port or not isinstance(current_port, str) or not current_port.strip():
        raise ValueError("current_port must be a non-empty string")
    current_port = current_port.strip()

    identity_port = identity.get("port")
    if not isinstance(identity_port, str) or not identity_port.strip():
        raise ValueError("identity['port'] must be a non-empty string")
    identity_port = identity_port.strip()

    if current_port != identity_port:
        raise ValueError(f"current_port ({current_port!r}) does not match identity['port'] ({identity_port!r})")

    bound_loc = identity.get("usb_location")
    bound_serial = identity.get("usb_serial")
    valid_loc = isinstance(bound_loc, str) and bool(bound_loc.strip())
    valid_serial = isinstance(bound_serial, str) and bool(bound_serial.strip())
    if not valid_loc and not valid_serial:
        raise ValueError("Identity must contain valid non-empty usb_location or usb_serial")

    matched_records = []
    for p in serial.tools.list_ports.comports():
        if p.device == control_port:
            is_boot = (getattr(p, "vid", None) == BOOT_VID and getattr(p, "pid", None) == BOOT_PID)
            if not is_boot and _match_usb_binding(p, identity):
                matched_records.append(p)

    if len(matched_records) != 1:
        raise RuntimeError(
            f"Expected exactly 1 active non-boot control port matching {control_port!r} with identity bindings, found {len(matched_records)}"
        )

    log(f"向控制端口 {control_port} 下发复位指令引导切入 Bootloader...")
    with serial.Serial(control_port, baudrate=115200, timeout=0.5) as ser:
        ser.dtr = True
        ser.rts = True
        # 1. 兼容网关运行态 JSON 命令
        ser.write(b'{"type":"cmd","cmd":"reboot","id":"flasher_rst","data":{"reason":"flasher_reboot","delay_ms":500}}\n')
        ser.flush()
        time.sleep(0.05)
        # 2. 兼容标准 AT 固件复位指令
        ser.write(b"AT+ECRST=delay,799\r\n")
        ser.flush()
        time.sleep(0.05)
        # 3. 兼容标准 LuatOS REPL 控制台
        ser.write(b"rtos.reboot()\r\n")
        ser.flush()
        ser.write(b"~" + bytes([0, 2]) + b"~")
        ser.flush()

    return True

def assemble_flasher_ini(
    work_dir: str,
    chip_type: str,
    port: str,
    mode: str = "script",
    preflight: Optional[Dict[str, Any]] = None,
) -> str:
    """
    动态装配移芯 FlashToolCLI 脚本烧录配置文件 config_pkg_product_usb.ini
    严格限定 script 单一模式与已核资产，只定义 flexfile2 作为写入项。
    """
    if mode != "script":
        raise ValueError(f"Unsupported mode: {mode!r}, only 'script' mode is permitted")

    if not isinstance(preflight, dict):
        raise ValueError("preflight must be a valid non-empty dict")

    if not isinstance(chip_type, str) or chip_type not in VALID_CHIPS:
        raise ValueError(f"Unknown or invalid chip_type: {chip_type!r}")

    pf_chip = preflight.get("chip")
    if chip_type != pf_chip:
        raise ValueError(f"chip_type {chip_type!r} does not match preflight chip {pf_chip!r}")

    proof_id = preflight.get("proof_id")
    if not isinstance(proof_id, str) or not proof_id:
        raise ValueError("Missing proof_id in preflight")

    if proof_id not in SCRIPT_FLASH_PROOFS:
        raise ValueError(f"unsupported: proof_id {proof_id!r} not in SCRIPT_FLASH_PROOFS")

    proof_entry = SCRIPT_FLASH_PROOFS[proof_id]
    if not isinstance(proof_entry, dict) or not proof_entry.get("evidence"):
        raise ValueError(f"Invalid proof_entry for proof_id {proof_id!r}")

    resources = preflight.get("resources")
    resource_hashes = preflight.get("resource_hashes")
    if not isinstance(resources, dict) or not isinstance(resource_hashes, dict):
        raise ValueError("Missing resources or resource_hashes in preflight")

    for role in ("cli", "fcelf", "binpkg", "format", "agentboot"):
        rpath = resources.get(role)
        if not rpath or not os.path.isfile(rpath):
            raise FileNotFoundError(f"Missing resource file for {role}: {rpath}")
        h = hashlib.sha256()
        with open(rpath, "rb") as rf:
            while True:
                chunk = rf.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        actual_hash = h.hexdigest()
        expected_hash = resource_hashes.get(role)
        if actual_hash != expected_hash:
            raise ValueError(f"Resource {role} hash mismatch: actual {actual_hash} != expected {expected_hash}")

    pkg = preflight.get("package")
    if not isinstance(pkg, dict) or not isinstance(pkg.get("manifest"), dict):
        raise ValueError("Invalid package in preflight")
    manifest = pkg["manifest"]

    script_meta = manifest.get("script")
    if not isinstance(script_meta, dict):
        raise ValueError("Missing script metadata in manifest")

    script_path = pkg.get("script_path")
    if not script_path or not os.path.isfile(script_path):
        raise FileNotFoundError(f"Missing script file: {script_path}")

    expected_size = script_meta.get("size")
    expected_sha = script_meta.get("sha256")
    actual_size = os.path.getsize(script_path)
    if actual_size != expected_size:
        raise ValueError(f"Script size mismatch: actual {actual_size} != manifest {expected_size}")

    h_script = hashlib.sha256()
    with open(script_path, "rb") as sf:
        while True:
            chunk = sf.read(65536)
            if not chunk:
                break
            h_script.update(chunk)
    if h_script.hexdigest() != expected_sha:
        raise ValueError(f"Script SHA256 mismatch: actual {h_script.hexdigest()} != manifest {expected_sha}")

    target = manifest.get("target")
    if not isinstance(target, dict):
        raise ValueError("Missing target in manifest")

    script_offset = target.get("script_offset")
    script_capacity = target.get("script_capacity")
    if not isinstance(script_offset, int) or not isinstance(script_capacity, int) or script_capacity <= 0:
        raise ValueError("Invalid script_offset or script_capacity in manifest target")

    if actual_size > script_capacity:
        raise ValueError(f"Script size {actual_size} exceeds script_capacity {script_capacity}")

    if not isinstance(work_dir, str) or not work_dir.strip():
        raise ValueError("work_dir must be a non-empty string path")
    os.makedirs(work_dir, exist_ok=True)

    format_filename = os.path.basename(resources["format"])
    shutil.copy2(resources["binpkg"], os.path.join(work_dir, "luatos.binpkg"))
    shutil.copy2(resources["agentboot"], os.path.join(work_dir, "agentboot_usb.bin"))
    shutil.copy2(resources["format"], os.path.join(work_dir, format_filename))
    shutil.copy2(resources["fcelf"], os.path.join(work_dir, "fcelf.exe"))
    shutil.copy2(script_path, os.path.join(work_dir, "script.bin"))

    rom_version = "0000000103040000" if chip_type == "ec718pv" else "0000000302000000"
    rom_version_append = (
        "0000000203000000;0000000303000000;0000000103030000;0000000203030000"
        if chip_type == "ec718pv"
        else "0000000102000000;0000000201000000;0000000302000000;0000000202000000"
    )

    ini_content = [
        "[config]",
        f"line_0_com = {port}",
        "agbaud = 921600",
        "filter_embedusb = 0",
        "filter_externcom = 0",
        "",
        "[package_info]",
        "pkgflag = 1",
        "pkg_extract_exe = .\\fcelf.exe",
        "arg_pkg_path_val = .\\luatos.binpkg",
        "",
        "[agentboot]",
        "tool_basedir = 0",
        "agpath = .\\agentboot_usb.bin",
        "",
        "[storage_cfg]",
        'opt_storage_list = "cp_flash"',
        f"format_path = .\\{format_filename}",
        "",
        "[control]",
        "prempt_detect_time = 6",
        "msg_waittime = 2",
        "max_preamble_cnt = 8",
        "lpc_recover_en = 0",
        "pullup_qspi = 1",
        f"rom_version = {rom_version}",
        f"rom_version_append_list = {rom_version_append}",
        "",
        "[flexfile2]",
        "filepath = .\\script.bin",
        f"burnaddr = {hex(script_offset)}",
        "storage_type = ap_flash",
        "",
    ]

    ini_path = os.path.join(work_dir, "config_pkg_product_usb.ini")
    with open(ini_path, "w", encoding="utf-8") as f:
        f.write("\n".join(ini_content))
    return ini_path

def preflight_script_flash(package_dir: str, identity: Dict[str, Any], work_dir: str) -> Dict[str, Any]:
    """
    脚本刷写前置准入校验（完全在暂停/串口/复位/CLI前执行）
    返回: {package, identity, chip, resources, resource_hashes, proof_id}
    """
    if not isinstance(package_dir, str) or not os.path.isdir(package_dir):
        raise ValueError(f"Invalid package_dir: {package_dir!r}")

    if not isinstance(work_dir, str) or not work_dir.strip():
        raise ValueError("work_dir must be a non-empty path string")
    try:
        work_dir.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError(f"work_dir must be an ASCII-only path: {work_dir!r}")

    if BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)
    import luadb_packer

    pkg_before = luadb_packer.load_release_package(package_dir)
    if not isinstance(pkg_before, dict) or not isinstance(pkg_before.get("manifest"), dict):
        raise ValueError("Invalid release package: missing manifest")
    manifest = pkg_before["manifest"]

    if not isinstance(identity, dict):
        raise ValueError("identity must be a dict")

    for key in ("device_id", "model", "core_version", "port", "control_port"):
        val = identity.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"identity missing or empty required field: {key!r}")

    loc = identity.get("usb_location")
    ser = identity.get("usb_serial")
    valid_loc = isinstance(loc, str) and bool(loc.strip())
    valid_ser = isinstance(ser, str) and bool(ser.strip())
    if not valid_loc and not valid_ser:
        raise ValueError("identity must contain a valid non-empty usb_location or usb_serial")

    target = manifest.get("target")
    if not isinstance(target, dict):
        raise ValueError("manifest missing target dict")

    target_chip = target.get("chip")
    if not isinstance(target_chip, str) or not target_chip.strip():
        raise ValueError("manifest target missing chip")

    id_model = identity["model"].strip()
    id_chip = identity.get("chip")
    if id_chip is not None and isinstance(id_chip, str):
        id_chip = id_chip.strip() or None
    resolved_chip = normalize_chip_type(id_model, id_chip)
    if resolved_chip != target_chip.strip():
        raise ValueError(
            f"Normalized chip {resolved_chip!r} does not match package target chip {target_chip!r}"
        )

    target_models = target.get("models")
    if not isinstance(target_models, list) or not target_models:
        raise ValueError("manifest target models must be a non-empty list")
    if id_model not in target_models:
        raise ValueError(f"Device model {id_model!r} not in target models: {target_models!r}")

    id_core = identity["core_version"].strip()
    target_cores = target.get("core_versions")
    if not isinstance(target_cores, list) or not target_cores:
        raise ValueError("manifest target core_versions must be a non-empty list")
    if id_core not in target_cores:
        raise ValueError(f"Device core_version {id_core!r} not in target core_versions: {target_cores!r}")

    chip_cfg = CHIP_CONFIGS.get(resolved_chip)
    if not chip_cfg:
        raise ValueError(f"Unknown chip configuration for {resolved_chip!r}")

    target_dir = os.path.join(FLASHER_TARGETS_DIR, chip_cfg["target_dir_name"])

    cli_path = FLASHTOOL_CLI
    if resolved_chip == "ec618":
        fcelf_path = os.path.join(FLASHER_BIN_DIR, "fcelf_ec618.exe")
    else:
        fcelf_path = os.path.join(FLASHER_BIN_DIR, "fcelf.exe")

    binpkg_path = os.path.join(target_dir, "luatos.binpkg")
    format_filename = f"format_{chip_cfg['target_dir_name']}.json"
    format_path = os.path.join(target_dir, format_filename)

    src_agentboot = os.path.join(target_dir, "agentboot_usb.bin")
    if resolved_chip == "ec618":
        std_ag = os.path.join(
            FLASHER_BIN_DIR, "product_sets", "ec618_products", "common_data", "agentboot_usb", "agentboot.bin"
        )
        if os.path.exists(std_ag):
            src_agentboot = std_ag
        elif not os.path.exists(src_agentboot):
            src_agentboot = os.path.join(FLASHER_BIN_DIR, "agentboot_usb.bin")
    elif not os.path.exists(src_agentboot):
        src_agentboot = os.path.join(FLASHER_BIN_DIR, "agentboot_usb.bin")
    agentboot_path = src_agentboot

    resources = {
        "cli": cli_path,
        "fcelf": fcelf_path,
        "binpkg": binpkg_path,
        "format": format_path,
        "agentboot": agentboot_path,
    }

    resource_hashes = {}
    for role in ("cli", "fcelf", "binpkg", "format", "agentboot"):
        rpath = resources[role]
        if not isinstance(rpath, str) or not os.path.isfile(rpath):
            raise FileNotFoundError(f"Missing required flasher resource for {role!r}: {rpath!r}")
        if os.path.getsize(rpath) <= 0:
            raise ValueError(f"Resource file for {role!r} is empty: {rpath!r}")
        h = hashlib.sha256()
        with open(rpath, "rb") as rf:
            while True:
                chunk = rf.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        resource_hashes[role] = h.hexdigest()

    expected_format_sha = target.get("format_sha256")
    if not isinstance(expected_format_sha, str) or not expected_format_sha.strip():
        raise ValueError("manifest target missing format_sha256")
    if resource_hashes["format"].lower() != expected_format_sha.strip().lower():
        raise ValueError(
            f"Resource format SHA256 mismatch: actual {resource_hashes['format']}, expected {expected_format_sha}"
        )

    aux_files = ("soc_tools.exe", "PrMgrCfg.json", "logging.conf", "cfg.digest")
    for aux in aux_files:
        aux_path = os.path.join(FLASHER_BIN_DIR, aux)
        if not os.path.isfile(aux_path):
            raise FileNotFoundError(f"Missing required toolchain aux file: {aux_path}")
        if os.path.getsize(aux_path) <= 0:
            raise ValueError(f"Toolchain aux file is empty: {aux_path}")

    ps_dir = os.path.join(FLASHER_BIN_DIR, "product_sets")
    if not os.path.isdir(ps_dir):
        raise FileNotFoundError(f"Missing required toolchain directory product_sets: {ps_dir}")

    bin_real = os.path.realpath(FLASHER_BIN_DIR)
    tree_records = []
    for root, _, files in os.walk(FLASHER_BIN_DIR):
        for fname in files:
            file_full = os.path.join(root, fname)
            file_real = os.path.realpath(file_full)
            if not (file_real == bin_real or file_real.startswith(bin_real + os.sep)):
                raise ValueError(f"Toolchain file realpath escapes FLASHER_BIN_DIR: {file_real}")
            rel_path = os.path.relpath(file_full, FLASHER_BIN_DIR).replace("\\", "/")
            h_item = hashlib.sha256()
            with open(file_real, "rb") as rf:
                while True:
                    chunk = rf.read(65536)
                    if not chunk:
                        break
                    h_item.update(chunk)
            tree_records.append((rel_path, h_item.hexdigest()))

    tree_records.sort(key=lambda x: x[0])
    manifest_lines = [f"{rp}:{sh}" for rp, sh in tree_records]
    canonical_tree_sha = hashlib.sha256("\n".join(manifest_lines).encode("utf-8")).hexdigest()
    resource_hashes["toolchain_tree"] = canonical_tree_sha

    proof_payload = {
        "resources": resource_hashes,
        "target": target,
    }
    proof_canonical_json = json.dumps(
        proof_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    proof_id = hashlib.sha256(proof_canonical_json.encode("utf-8")).hexdigest()

    script_offset = target.get("script_offset")
    script_capacity = target.get("script_capacity")
    if not isinstance(script_offset, int) or not isinstance(script_capacity, int) or script_capacity <= 0:
        raise ValueError("unsupported: target 缺少script擦除证据 (missing or invalid script_offset/script_capacity)")

    expected_script_range = [script_offset, script_offset + script_capacity]

    if proof_id not in SCRIPT_FLASH_PROOFS:
        raise ValueError(f"unsupported: proof_id {proof_id} 缺少script擦除证据")

    proof_entry = SCRIPT_FLASH_PROOFS[proof_id]
    if not isinstance(proof_entry, dict):
        raise ValueError(f"unsupported: proof_entry for {proof_id} 缺少script擦除证据 (not dict)")

    evidence = proof_entry.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError(f"unsupported: proof_entry for {proof_id} 缺少script擦除证据 (empty evidence)")

    write_range = proof_entry.get("write_range")
    erase_range = proof_entry.get("erase_range")
    if write_range != expected_script_range or erase_range != expected_script_range:
        raise ValueError(
            f"unsupported: proof_entry for {proof_id} 缺少script擦除证据 (invalid write_range or erase_range)"
        )

    os.makedirs(work_dir, exist_ok=True)

    verified_pkg = luadb_packer.verify_release_package(package_dir, work_dir)
    if not isinstance(verified_pkg, dict) or not isinstance(verified_pkg.get("manifest"), dict):
        raise ValueError("verify_release_package returned invalid package structure")

    pkg_id_before = manifest.get("package_id")
    pkg_id_after = verified_pkg["manifest"].get("package_id")
    if not pkg_id_before or pkg_id_before != pkg_id_after:
        raise ValueError(
            f"Package verification package_id mismatch: before={pkg_id_before!r}, after={pkg_id_after!r}"
        )

    pf = {
        "package": verified_pkg,
        "identity": copy.deepcopy(identity),
        "chip": resolved_chip,
        "resources": resources,
        "resource_hashes": resource_hashes,
        "proof_id": proof_id,
    }

    for aux in aux_files:
        shutil.copy2(os.path.join(FLASHER_BIN_DIR, aux), os.path.join(work_dir, aux))
    shutil.copytree(
        os.path.join(FLASHER_BIN_DIR, "product_sets"),
        os.path.join(work_dir, "product_sets"),
        dirs_exist_ok=True,
    )

    ini_path = assemble_flasher_ini(work_dir, resolved_chip, identity["port"], "script", preflight=pf)

    cp = configparser.ConfigParser()
    if not cp.read(ini_path, encoding="utf-8"):
        raise ValueError(f"Failed to read generated INI: {ini_path}")

    expected_sections = {"config", "package_info", "agentboot", "storage_cfg", "control", "flexfile2"}
    if set(cp.sections()) != expected_sections:
        raise ValueError(f"INI sections mismatch: expected {expected_sections}, got {set(cp.sections())}")

    for sec in cp.sections():
        if sec != "flexfile2":
            if sec.startswith("flexfile") or cp.has_option(sec, "burnaddr"):
                raise ValueError(f"Unexpected write section or burnaddr in INI section {sec!r}")

    if cp.get("flexfile2", "filepath", fallback=None) != ".\\script.bin":
        raise ValueError(f"flexfile2 filepath must be '.\\script.bin', got {cp.get('flexfile2', 'filepath', fallback=None)!r}")

    burnaddr_raw = cp.get("flexfile2", "burnaddr", fallback=None)
    if burnaddr_raw is None:
        raise ValueError("flexfile2 missing burnaddr in INI")
    try:
        burnaddr_val = int(burnaddr_raw, 0)
    except Exception as ex:
        raise ValueError(f"Invalid burnaddr format {burnaddr_raw!r}: {ex}")
    if burnaddr_val != script_offset:
        raise ValueError(f"flexfile2 burnaddr mismatch: {burnaddr_val} != target script_offset {script_offset}")

    work_script_path = os.path.join(work_dir, "script.bin")
    if not os.path.isfile(work_script_path):
        raise FileNotFoundError(f"Missing script.bin in work_dir: {work_script_path}")
    actual_ws_size = os.path.getsize(work_script_path)
    manifest_script = manifest.get("script", {})
    if actual_ws_size != manifest_script.get("size"):
        raise ValueError(f"work_dir script.bin size mismatch: actual {actual_ws_size} != manifest {manifest_script.get('size')}")
    h_ws = hashlib.sha256()
    with open(work_script_path, "rb") as wsf:
        while True:
            chunk = wsf.read(65536)
            if not chunk:
                break
            h_ws.update(chunk)
    if h_ws.hexdigest().lower() != str(manifest_script.get("sha256", "")).lower():
        raise ValueError(f"work_dir script.bin SHA256 mismatch: actual {h_ws.hexdigest()} != manifest {manifest_script.get('sha256')}")

    pf["ini_path"] = ini_path
    return pf

def flash_hardware_cli(
    script_bin_path: Optional[str] = None,
    current_vuart_port: Optional[str] = None,
    hardware_model: Optional[str] = None,
    chip_type: Optional[str] = None,
    mode: str = "script",
    progress_cb: Optional[Callable[[int, str], None]] = None,
    watchdog_timeout: int = 90,
    package_dir: Optional[str] = None,
    identity: Optional[Dict[str, Any]] = None,
    job_id: Optional[str] = None,
    work_dir: Optional[str] = None,
    maintenance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    纯命令行受控烧录固件脚本至硬件模组（通用多芯片架构驱动引擎）
    严格仅支持已核冻结发布包在 Hub ACK 占用维护下的 script 模式线刷。
    """
    dev_id = identity.get("device_id") if isinstance(identity, dict) else None
    package_id = None
    device_contacted = False

    def ret(ok: bool, phase: str, msg: str, stopped: bool = True, **extra) -> Dict[str, Any]:
        res = {
            "ok": ok,
            "phase": phase,
            "msg": msg,
            "job_id": job_id,
            "device_id": dev_id,
            "package_id": package_id,
            "work_dir": work_dir,
            "process_stopped": stopped,
        }
        res.update(extra)
        return res

    try:
        if mode != "script":
            return ret(False, "failed", f"模式 {mode!r} 当前硬性禁用，仅支持 'script' 应用脚本烧录")
        if script_bin_path is not None:
            return ret(False, "failed", "禁止外部指定 script_bin_path，必须严格使用发布包脚本")
        if not isinstance(identity, dict):
            return ret(False, "failed", "缺少有效设备物理身份字典 (identity must be a dict)")
        if not package_dir or not isinstance(package_dir, str) or not os.path.isdir(package_dir):
            return ret(False, "failed", f"缺少有效冻结发布包目录: {package_dir!r}")
        if not job_id or not isinstance(job_id, str) or not job_id.strip():
            return ret(False, "failed", "缺少有效任务标识 (job_id must be non-empty string)")
        if not work_dir or not isinstance(work_dir, str) or not work_dir.strip():
            return ret(False, "failed", "缺少指定工作目录 (work_dir must be non-empty string)")
        try:
            work_dir.encode("ascii")
        except UnicodeEncodeError:
            return ret(False, "failed", f"工作目录必须为纯 ASCII 路径: {work_dir!r}")

        if not isinstance(maintenance, dict) or maintenance.get("ok") is not True:
            return ret(False, "failed", "缺少有效 Hub 维护占用记录或未成功 ACK")
        if maintenance.get("job_id") != job_id:
            return ret(False, "failed", f"维护记录 job_id 冲突: {maintenance.get('job_id')!r} != {job_id!r}")
        if maintenance.get("device_id") != dev_id:
            return ret(False, "failed", f"维护记录 device_id 冲突: {maintenance.get('device_id')!r} != {dev_id!r}")
        if maintenance.get("mode") != mode:
            return ret(False, "failed", f"维护记录 mode 冲突: {maintenance.get('mode')!r} != {mode!r}")

        if current_vuart_port is not None and current_vuart_port.strip() != str(identity.get("port", "")).strip():
            return ret(False, "failed", "current_vuart_port 与 identity['port'] 不一致")
        if hardware_model is not None and hardware_model.strip().upper() != str(identity.get("model", "")).strip().upper():
            return ret(False, "failed", "hardware_model 与 identity['model'] 不一致")

        resolved_chip = normalize_chip_type(identity.get("model"), identity.get("chip"))
        if chip_type is not None and chip_type.strip().lower() != resolved_chip:
            return ret(False, "failed", "chip_type 与 identity 芯片类型不一致")

        if not isinstance(watchdog_timeout, (int, float)) or watchdog_timeout <= 0 or watchdog_timeout != watchdog_timeout or watchdog_timeout == float("inf"):
            return ret(False, "failed", f"watchdog_timeout 必须为合法正有限数: {watchdog_timeout!r}")
        if sys.platform != "win32":
            return ret(False, "failed", "移芯 FlashToolCLI 烧录仅支持 Windows 环境")
        if not os.path.exists(FLASHTOOL_CLI):
            return ret(False, "failed", f"未找到烧录工具 CLI 文件: {FLASHTOOL_CLI}")

        preflight_res = preflight_script_flash(package_dir, identity, work_dir)
        package_id = preflight_res["package"]["manifest"].get("package_id")
        if maintenance.get("package_id") != package_id:
            return ret(False, "failed", f"维护记录 package_id 冲突: {maintenance.get('package_id')!r} != {package_id!r}")

        chip = preflight_res["chip"]
        chip_cfg = CHIP_CONFIGS[chip]

        def emit(pct: int, txt: str):
            capped = min(pct, 95)
            log(f"[{capped}%] [{chip_cfg['name']}] {txt}")
            if progress_cb:
                try:
                    progress_cb(capped, txt)
                except Exception:
                    pass

        emit(15, "正在检测或引导模组切入 Bootloader 模式...")
        boot_port = find_bootloader_port(identity)
        if not boot_port:
            device_contacted = True
            trigger_soft_reboot_to_boot(identity["port"], identity)
            start_wait = time.time()
            last_hint = 0.0
            while not boot_port and (time.time() - start_wait < 60.0):
                time.sleep(0.2)
                boot_port = find_bootloader_port(identity)
                if not boot_port and (time.time() - last_hint > 2.5):
                    last_hint = time.time()
                    emit(20, f"正在守候唯一物理绑定 Bootloader 端口 ({int(60 - (time.time() - start_wait))}s)...")

        if not boot_port:
            return ret(False, "uncertain" if device_contacted else "failed", "未能捕获唯一物理绑定 Bootloader 端口 (60s 等待超时)", stopped=not device_contacted)

        device_contacted = True
        emit(30, f"已捕获 Bootloader 端口 {boot_port}，正在执行探针打开校验...")
        port_ready = False
        for _ in range(20):
            try:
                with serial.Serial(boot_port, baudrate=115200, timeout=0.1):
                    port_ready = True
                    break
            except Exception:
                time.sleep(0.15)
        if not port_ready:
            return ret(False, "uncertain", f"Bootloader 端口 {boot_port} 实际打开探针失败，终止操作")
        time.sleep(0.3)

        assemble_flasher_ini(work_dir, chip, boot_port, mode, preflight=preflight_res)

        cli_exe = preflight_res["resources"]["cli"]
        for role in ("cli", "fcelf", "binpkg", "format", "agentboot"):
            rpath = preflight_res["resources"][role]
            h_chk = hashlib.sha256()
            with open(rpath, "rb") as rf:
                while True:
                    c = rf.read(65536)
                    if not c:
                        break
                    h_chk.update(c)
            if h_chk.hexdigest() != preflight_res["resource_hashes"][role]:
                return ret(False, "uncertain", f"Boot 确定后资源哈希再校验异常: {role}")

        cmd_step_idx = 0
        def run_tool_cmd(cmd_args: List[str], timeout_limit: float, step_tag: str) -> subprocess.CompletedProcess:
            nonlocal cmd_step_idx
            cmd_step_idx += 1
            log_entry = {
                "step": step_tag,
                "args": cmd_args,
                "timeout": timeout_limit,
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "timed_out": False,
                "error": None,
            }
            try:
                res_proc = subprocess.run(cmd_args, cwd=work_dir, capture_output=True, shell=False, timeout=timeout_limit)
                log_entry["returncode"] = res_proc.returncode
                log_entry["stdout"] = res_proc.stdout.decode("utf-8", errors="backslashreplace") if res_proc.stdout else ""
                log_entry["stderr"] = res_proc.stderr.decode("utf-8", errors="backslashreplace") if res_proc.stderr else ""
                return res_proc
            except subprocess.TimeoutExpired as te:
                log_entry["timed_out"] = True
                log_entry["returncode"] = None
                log_entry["stdout"] = te.stdout.decode("utf-8", errors="backslashreplace") if getattr(te, "stdout", None) else ""
                log_entry["stderr"] = te.stderr.decode("utf-8", errors="backslashreplace") if getattr(te, "stderr", None) else ""
                log_entry["error"] = f"TimeoutExpired after {timeout_limit}s"
                raise
            except Exception as ex:
                log_entry["error"] = str(ex)
                raise
            finally:
                try:
                    with open(os.path.join(work_dir, f"cmd_{cmd_step_idx:02d}_{step_tag}.json"), "w", encoding="utf-8") as lf:
                        json.dump(log_entry, lf, indent=2, ensure_ascii=False)
                except Exception:
                    pass

        if chip == "ec718pv":
            emit(50, "正在与芯片 Bootloader 握手 (probe)...")
            cmd_probe = [cli_exe, "--cfgfile", "config_pkg_product_usb.ini", "--port", boot_port, "probe"]
            try:
                p_probe = run_tool_cmd(cmd_probe, 12.0, "probe")
                if p_probe.returncode != 0:
                    err = p_probe.stderr.decode("utf-8", errors="backslashreplace") or p_probe.stdout.decode("utf-8", errors="backslashreplace")
                    return ret(False, "uncertain", f"芯片握手失败 (probe rc={p_probe.returncode}): {err[:200]}")
            except subprocess.TimeoutExpired:
                return ret(False, "uncertain", f"芯片握手超时 (probe timed out after 12s on {boot_port})", stopped=False)
        else:
            log("[EC618] 原厂流跳过独立 probe 指令，直接由 burnbatch 内部 burnag 驱动握手与固件写入")
            emit(50, "正在连接 EC618 Bootloader...")

        emit(70, "握手完成，正在执行应用脚本烧写 (burnbatch flexfile2)...")
        cmd_burn = [cli_exe, "--cfgfile", "config_pkg_product_usb.ini", "--port", boot_port]
        if chip == "ec718pv":
            cmd_burn.extend(["--skipconnect", "1"])
        cmd_burn.extend(["burnbatch", "--imglist", "flexfile2"])
        try:
            p_burn = run_tool_cmd(cmd_burn, float(watchdog_timeout), "burnbatch")
            if p_burn.returncode != 0:
                err = p_burn.stderr.decode("utf-8", errors="backslashreplace") or p_burn.stdout.decode("utf-8", errors="backslashreplace")
                return ret(False, "uncertain", f"固件写入失败 (burnbatch rc={p_burn.returncode}): {err[:200]}")
        except subprocess.TimeoutExpired:
            return ret(False, "uncertain", f"固件烧写超时 (burnbatch timed out after {watchdog_timeout}s on {boot_port})", stopped=False)

        emit(90, "固件写入完成，正在执行模组平滑软复位重启...")
        cmd_reset = [cli_exe, "--cfgfile", "config_pkg_product_usb.ini", "--port", boot_port, "--skipconnect", "1", "sysreset"]
        try:
            p_reset = run_tool_cmd(cmd_reset, 5.0, "sysreset")
            if p_reset.returncode != 0:
                err = p_reset.stderr.decode("utf-8", errors="backslashreplace") or p_reset.stdout.decode("utf-8", errors="backslashreplace")
                return ret(False, "uncertain", f"模组复位失败 (sysreset rc={p_reset.returncode}): {err[:200]}")
        except subprocess.TimeoutExpired:
            return ret(False, "uncertain", "模组复位超时 (sysreset timed out after 5s)", stopped=False)

        emit(95, f"模组已下发平滑重启，{chip_cfg['name']} 固件写入与复位完成，待同设备新验证")
        return ret(
            True,
            "confirming",
            f"模组应用脚本线刷写入与复位已完成 ({chip_cfg['name']})，待同设备新 boot/version/build 验证",
            write_completed=True,
            reset_completed=True,
            port=boot_port,
            chip=chip,
            mode=mode,
            percent=95,
        )
    except Exception as e:
        is_to = isinstance(e, subprocess.TimeoutExpired)
        return ret(
            False,
            "uncertain" if device_contacted else "failed",
            f"flash_hardware_cli 执行异常: {e}",
            stopped=False if is_to else True,
        )

if __name__ == "__main__":
    print("=" * 65)
    print("🚀 上位机多芯片通用固件线刷引擎 (FlashToolCLI) 自检")
    print("=" * 65)
    boot = find_bootloader_port()
    print(f"• 当前 Bootloader 端口: {boot or '未进入 (就绪待触发)'}")
    print(f"• FlashToolCLI 就绪: {os.path.exists(FLASHTOOL_CLI)}")
    print(f"• 独立资产根目录: {FLASHER_DIR}")
    print(f"• 支持芯片矩阵: {list(CHIP_CONFIGS.keys())}")
    for k, v in CHIP_CONFIGS.items():
        td = os.path.join(FLASHER_TARGETS_DIR, v["target_dir_name"])
        print(f"  - [{k}] {v['name']}: {os.path.exists(td)} ({td})")
