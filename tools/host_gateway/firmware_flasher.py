#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780 系列智能通信网关 - 上位机多芯片通用固件线刷引擎 (Firmware Flasher Engine)
支持：
1. 全新裸板接入 Full Flash 模式（刷入 LuatOS 底层内核 + 业务脚本 + 射频校准表，自动识别或指定芯片架构）；
2. 在线模组 Script Flash 模式（极速 1~2 秒仅重刷应用业务 script.bin）；
3. 芯片架构全支持：EC718PV（Air780EPV/Air780EP）与 EC618（Air780E/Air780EG/Air700E）；
4. 资源自包含：优先使用固化的 tools/flasher 资产，脱离 Luatools 临时目录。
"""

import os
import sys
import time
import shutil
import tempfile
import subprocess
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
        meipass = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        bundled = os.path.join(meipass, sub_name)
        if os.path.exists(bundled):
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

if not os.path.exists(FLASHTOOL_CLI):
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
        "has_full_flash": True,
        "full_imglist": ["bootloader", "system", "cp_system", "flexfile2"],
        "target_dir_name": "ec718pv"
    },
    "ec618": {
        "name": "EC618",
        "models": ["Air780E", "Air780EG", "Air700E"],
        "burnaddr_script": "0x24D000",
        "script_magic": 0xeaf18c16,
        "script_base_addr": 0x0024D000,
        "has_full_flash": True,
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

def normalize_chip_type(hardware_model: Optional[str], chip_hint: Optional[str] = None) -> str:
    """根据硬件型号或芯片提示归一化为标准的芯片类型 ('ec718pv' 或 'ec618')"""
    if chip_hint:
        c = chip_hint.lower()
        if "718" in c:
            return "ec718pv"
        if "618" in c:
            return "ec618"
    if hardware_model:
        m = hardware_model.upper()
        if any(x in m for x in ["780EPV", "780EP", "EC718"]):
            return "ec718pv"
        if any(x in m for x in ["780E", "780EG", "700E", "EC618"]):
            return "ec618"
    # 默认兜底为最通用且全功能的 ec718pv
    return "ec718pv"

def find_bootloader_port(target_loc_prefix: Optional[str] = None) -> Optional[str]:
    """探测 ROM Bootloader 烧录端口（移芯全系平台 17D1:0001）
    :param target_loc_prefix: 可选 USB Hub 拓扑前缀（如 '1-13.1' 或 '1-8'），用于在多卡槽环境下精准过滤目标硬件的 Bootloader 端口，杜绝串刷。
    """
    for p in serial.tools.list_ports.comports():
        hwid = (p.hwid or "").upper()
        vid = p.vid
        pid = p.pid
        loc = getattr(p, "location", "") or ""
        is_boot = (vid == BOOT_VID and pid == BOOT_PID) or "17D1:0001" in hwid or ("17D1" in (hex(vid or 0).upper()) and "0001" in (hex(pid or 0).upper()))
        if is_boot:
            if target_loc_prefix:
                if loc.startswith(target_loc_prefix):
                    return p.device
            else:
                return p.device
    return None

def trigger_soft_reboot_to_boot(target_port: Optional[str] = None):
    """
    通过底层 AT/CDC 控制端口下发 AT+ECRST=delay,799 / rtos.reboot() 与 ~SYNC~
    安全引导芯片切入 ROM Bootloader 烧录模式
    """
    candidate_ports = []
    target_loc_prefix = None

    ports = list(serial.tools.list_ports.comports())
    if target_port:
        candidate_ports.append(target_port)
        for p in ports:
            if p.device == target_port:
                loc = getattr(p, "location", "") or ""
                if ":" in loc:
                    target_loc_prefix = loc.split(":")[0]
                elif loc:
                    target_loc_prefix = loc
                break

    for p in ports:
        loc = getattr(p, "location", "") or ""
        dev = p.device
        hwid = (p.hwid or "").upper()
        if dev in candidate_ports:
            continue
        if target_loc_prefix:
            if loc.startswith(target_loc_prefix):
                candidate_ports.append(dev)
        else:
            if "19D1:0001" in hwid and (":X.2" in loc.upper() or loc.endswith("x.2")):
                candidate_ports.append(dev)

    for cp in candidate_ports:
        try:
            log(f"向端口 {cp} 下发复位指令引导切入 Bootloader...")
            with serial.Serial(cp, baudrate=115200, timeout=0.5) as ser:
                ser.dtr = True
                ser.rts = True
                ser.write(b"AT+ECRST=delay,799\r\n")
                ser.flush()
                time.sleep(0.05)
                ser.write(b"rtos.reboot()\r\n")
                ser.flush()
                ser.write(b"~" + bytes([0, 2]) + b"~")
                ser.flush()
        except Exception:
            pass

def assemble_flasher_ini(work_dir: str, chip_type: str, port: str, mode: str = "script") -> str:
    """
    动态装配移芯 FlashToolCLI 所需的 config_pkg_product_usb.ini 配置文件
    所有镜像与配置均置于纯 ASCII 的 work_dir 内部，彻底规避 FlashToolCLI 路径含中文异常。
    :param work_dir: 临时工作目录 (位于系统 tempdir)
    :param chip_type: 'ec718pv' 或 'ec618'
    :param port: 烧录端口号 (如 'COM9')
    :param mode: 'script'（仅刷脚本）或 'full'（全量内核+脚本+射频表）
    :return: 写入的 ini 文件完整绝对路径
    """
    chip_cfg = CHIP_CONFIGS.get(chip_type, CHIP_CONFIGS["ec718pv"])
    target_dir = os.path.join(FLASHER_TARGETS_DIR, chip_cfg["target_dir_name"])
    
    # 查找并拷贝必要资产至 work_dir，确保纯英文相对路径
    src_pkg = os.path.join(target_dir, "luatos.binpkg")
    dst_pkg = os.path.join(work_dir, "luatos.binpkg")
    if os.path.exists(src_pkg) and not os.path.exists(dst_pkg):
        shutil.copy2(src_pkg, dst_pkg)

    src_agentboot = os.path.join(target_dir, "agentboot_usb.bin")
    if chip_type == "ec618":
        nonbl2_path = os.path.join(FLASHER_BIN_DIR, "product_sets", "ec618_products", "common_data", "agentboot_usb", "agentboot.bin_NONBL2")
        if os.path.exists(nonbl2_path):
            src_agentboot = nonbl2_path
    elif not os.path.exists(src_agentboot):
        src_agentboot = os.path.join(FLASHER_BIN_DIR, "agentboot_usb.bin")
    dst_agentboot = os.path.join(work_dir, "agentboot_usb.bin")
    if os.path.exists(src_agentboot) and not os.path.exists(dst_agentboot):
        shutil.copy2(src_agentboot, dst_agentboot)

    format_filename = f"format_{chip_cfg['target_dir_name']}.json"
    src_format = os.path.join(target_dir, format_filename)
    dst_format = os.path.join(work_dir, format_filename)
    if os.path.exists(src_format) and not os.path.exists(dst_format):
        shutil.copy2(src_format, dst_format)

    # 芯片架构物理隔离：EC618 必须用 fcelf_ec618.exe，EC718 必须用 fcelf.exe
    if chip_type == "ec618":
        src_fcelf = os.path.join(FLASHER_BIN_DIR, "fcelf_ec618.exe")
    else:
        src_fcelf = os.path.join(FLASHER_BIN_DIR, "fcelf.exe")
    dst_fcelf = os.path.join(work_dir, "fcelf.exe")
    if os.path.exists(src_fcelf) and not os.path.exists(dst_fcelf):
        shutil.copy2(src_fcelf, dst_fcelf)

    ini_content = [
        "[config]",
        f"line_0_com = {port}",
        "agbaud = 921600",
        "filter_embedusb = 0",
        "filter_externcom = 0",
        ""
    ]

    if mode == "full" and chip_type == "ec718pv":
        ini_content.extend([
            "[package_info]",
            "pkgflag = 1",
            "pkg_extract_exe = .\\fcelf.exe",
            "pkg_bins_regen_targetdir = .\\pkgimg_gen",
            "arg_pkg_path_val = .\\luatos.binpkg",
            "pkg_inicfg_regened_state = forward_regened",
            "select_product_support = 1",
            "old_package_defined_product = EC618_OLDPKG_DEFAULT",
            "selected_product = EC718P_PRD",
            "selected_base_inicfg_type = usb",
            "selected_base_inicfg_rec = .\\product_sets\\ec718_products\\ec718p\\base_config_files\\config_ec718p_prd_usb.baseini",
            ""
        ])
    elif mode == "full" and chip_type == "ec618":
        ini_content.extend([
            "[package_info]",
            "pkgflag = 1",
            "pkg_extract_exe = .\\fcelf.exe",
            "pkg_bins_regen_targetdir = .\\pkgimg_gen",
            "arg_pkg_path_val = .\\luatos.binpkg",
            ""
        ])
    else:
        ini_content.extend([
            "[package_info]",
            "pkgflag = 1",
            "pkg_extract_exe = .\\fcelf.exe",
            "arg_pkg_path_val = .\\luatos.binpkg",
            ""
        ])

    ini_content.extend([
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
        f"rom_version = {'0000000103040000' if chip_type == 'ec718pv' else '0000000102000000'}",
        f"rom_version_append_list = {'0000000203000000;0000000303000000;0000000103030000;0000000203030000' if chip_type == 'ec718pv' else '0000000102000000;0000000201000000;0000000302000000;0000000202000000'}",
        ""
    ])

    skip_val = 1 if mode == "script" else 0

    if chip_type == "ec618":
        rf_name = "MergeRfTable_ec618.bin"
        src_rf = os.path.join(target_dir, rf_name)
        dst_rf = os.path.join(work_dir, rf_name)
        if os.path.exists(src_rf) and not os.path.exists(dst_rf):
            shutil.copy2(src_rf, dst_rf)

        ini_content.extend([
            "[bootloader]",
            "blpath = .\\pkgimg_gen\\ap_bootloader.bin",
            f"blloadskip = {skip_val}",
            "burnaddr = 0x4000",
            "",
            "[system]",
            "syspath = .\\pkgimg_gen\\ap_demo-flash.bin",
            f"sysloadskip = {skip_val}",
            "burnaddr = 0x24000",
            "",
            "[cp_system]",
            "cp_syspath = .\\pkgimg_gen\\cp-demo-flash.bin",
            f"cp_sysloadskip = {skip_val}",
            "",
            "[flexfile0]",
            f"filepath = .\\{rf_name}",
            "burnaddr = 0xe7000",
            "storage_type = cp_flash",
            "",
            "[flexfile1]",
            f"filepath = .\\{rf_name}",
            "burnaddr = 0xce000",
            "storage_type = cp_flash",
            "",
            "[flexfile2]",
            "filepath = .\\script.bin",
            f"burnaddr = {chip_cfg['burnaddr_script']}",
            "storage_type = ap_flash",
            ""
        ])
    else:  # ec718pv
        ini_content.extend([
            "[bootloader]",
            "blpath = .\\pkgimg_gen\\ec718p_prd_images\\ap_bootloader.bin",
            f"blloadskip = {skip_val}",
            "burnaddr = 0x3000",
            "",
            "[system]",
            "syspath = .\\pkgimg_gen\\ec718p_prd_images\\ap.bin",
            f"sysloadskip = {skip_val}",
            "burnaddr = 0x7c000",
            "",
            "[cp_system]",
            "cp_syspath = .\\pkgimg_gen\\ec718p_prd_images\\cp-demo-flash.bin",
            f"cp_sysloadskip = {skip_val}",
            "burnaddr = 0x18000",
            "",
            "[flexfile0]",
            "filepath = .\\product_sets\\ec718_products\\ec718p\\rfCaliTb\\MergeRfTable.bin",
            "burnaddr = 0x3f2000",
            "",
            "[flexfile2]",
            "filepath = .\\script.bin",
            f"burnaddr = {chip_cfg['burnaddr_script']}",
            "storage_type = ap_flash",
            ""
        ])

    ini_path = os.path.join(work_dir, "config_pkg_product_usb.ini")
    with open(ini_path, "w", encoding="utf-8") as f:
        f.write("\n".join(ini_content))
    return ini_path

def flash_hardware_cli(
    script_bin_path: Optional[str] = None,
    current_vuart_port: Optional[str] = None,
    hardware_model: Optional[str] = None,
    chip_type: Optional[str] = None,
    mode: str = "script",
    progress_cb: Optional[Callable[[int, str], None]] = None,
    watchdog_timeout: int = 35
) -> Dict[str, Any]:
    """
    纯命令行静默烧录固件至硬件模组（通用多芯片架构驱动引擎）
    :param script_bin_path: 自定义 script.bin 路径（若为 None 则自动打包当前工程脚本）
    :param current_vuart_port: 当前业务端口（如 COM8/COM10）
    :param hardware_model: 硬件型号（如 'Air780EPV', 'Air780E'）
    :param chip_type: 芯片架构 ('ec718pv' 或 'ec618'，若为 None 自动根据型号识别)
    :param mode: 烧录模式：'script'（极速仅刷脚本，1~2s）或 'full'（全新模组全量刷入内核+脚本，8~12s）
    :param progress_cb: 进度回调 progress_cb(percent: int, message: str)
    :param watchdog_timeout: 总体看门狗超时（秒）
    :return: {"ok": bool, "msg": str, "port": str}
    """
    if sys.platform != "win32":
        return {"ok": False, "msg": "移芯 FlashToolCLI 烧录仅支持 Windows 环境"}

    if not os.path.exists(FLASHTOOL_CLI):
        return {"ok": False, "msg": f"未找到烧录工具 CLI 文件: {FLASHTOOL_CLI}"}

    chip = normalize_chip_type(hardware_model, chip_type)
    chip_cfg = CHIP_CONFIGS[chip]

    def emit(pct: int, txt: str):
        log(f"[{pct}%] [{chip_cfg['name']}] {txt}")
        if progress_cb:
            try:
                progress_cb(pct, txt)
            except Exception:
                pass

    # 1. 创建独立沙箱临时工作目录，置于系统 tempdir 确保纯 ASCII 路径，规避 FlashToolCLI 路径含中文异常
    work_dir = os.path.join(tempfile.gettempdir(), f"air780_flasher_{int(time.time()*1000)}_{os.getpid()}")
    os.makedirs(work_dir, exist_ok=True)

    try:
        emit(5, f"正在准备 {chip_cfg['name']} 固件镜像与打包 script.bin...")
        target_bin = os.path.join(work_dir, "script.bin")
        
        if script_bin_path and os.path.exists(script_bin_path):
            shutil.copy2(script_bin_path, target_bin)
        else:
            try:
                # 动态根据芯片架构打包匹配 Magic 与基地址的 LuaDB
                sys.path.insert(0, BASE_DIR)
                import luadb_packer
                raw_luadb = luadb_packer.pack_luadb(
                    magic=chip_cfg.get("script_magic"),
                    base_addr=chip_cfg.get("script_base_addr")
                )
                with open(target_bin, "wb") as f:
                    f.write(raw_luadb)
                log(f"自动打包 {chip_cfg['name']} LuaDB 完成: {len(raw_luadb)} 字节")
            except Exception as e:
                log(f"LuaDB 固件打包失败: {e}")
                return {"ok": False, "msg": f"LuaDB 固件打包失败: {e}"}

        # 2. 检查或触发 Bootloader
        emit(15, "正在检测或引导模组切入 Bootloader 模式...")
        target_loc_prefix = None
        if current_vuart_port:
            for p in serial.tools.list_ports.comports():
                if p.device == current_vuart_port:
                    loc = getattr(p, "location", "") or ""
                    if ":" in loc:
                        target_loc_prefix = loc.split(":")[0]
                    elif loc:
                        target_loc_prefix = loc
                    break

        boot_port = find_bootloader_port(target_loc_prefix)
        if not boot_port and current_vuart_port:
            trigger_soft_reboot_to_boot(current_vuart_port)

        start_wait = time.time()
        while not boot_port and (time.time() - start_wait < 10.0):
            time.sleep(0.2)
            boot_port = find_bootloader_port(target_loc_prefix)
        if not boot_port:
            return {"ok": False, "msg": "未能捕获 Bootloader 烧录端口 (VID 17D1:0001 / COM9)，请检查连接或手动重新上电"}

        emit(30, f"已连接 Bootloader 端口: {boot_port}，正在装配配置文件...")
        time.sleep(0.3)

        # 3. 动态组装 config_pkg_product_usb.ini 并同步关键资源至沙箱
        ini_path = assemble_flasher_ini(work_dir, chip, boot_port, mode)

        # 拷贝必要工具辅助 exe 与配置至工作目录
        for aux_tool in ["soc_tools.exe", "PrMgrCfg.json", "logging.conf", "cfg.digest"]:
            aux_src = os.path.join(FLASHER_BIN_DIR, aux_tool)
            if os.path.exists(aux_src):
                shutil.copy2(aux_src, os.path.join(work_dir, aux_tool))

        ps_src = os.path.join(FLASHER_BIN_DIR, "product_sets")
        ps_dst = os.path.join(work_dir, "product_sets")
        if os.path.exists(ps_src) and not os.path.exists(ps_dst):
            shutil.copytree(ps_src, ps_dst)

        # 4. 执行 pkg2img（若为全量模式且需要解包内核）
        if mode == "full":
            emit(40, "正在执行内核固件解包与镜像校验 (pkg2img)...")
            if chip == "ec618":
                # EC618 使用专用解包器 fcelf.exe (即 fcelf_ec618.exe) 直接执行 -E -input luatos.binpkg
                cmd_pkg = [os.path.join(work_dir, "fcelf.exe"), "-E", "-input", "luatos.binpkg"]
                p_pkg = subprocess.run(cmd_pkg, cwd=work_dir, capture_output=True, text=True)
                pkg_gen_dir = os.path.join(work_dir, "pkgimg_gen")
                os.makedirs(pkg_gen_dir, exist_ok=True)
                for f in ["ap.bin", "ap_bootloader.bin", "cp-demo-flash.bin"]:
                    src_f = os.path.join(work_dir, f)
                    if os.path.exists(src_f):
                        shutil.copy2(src_f, os.path.join(pkg_gen_dir, f))
                # 别名拷贝 ap.bin -> ap_demo-flash.bin
                ap_src = os.path.join(work_dir, "ap.bin")
                if os.path.exists(ap_src):
                    shutil.copy2(ap_src, os.path.join(pkg_gen_dir, "ap_demo-flash.bin"))
            else:
                cmd_pkg = [
                    FLASHTOOL_CLI,
                    "--cfgfile", "config_pkg_product_usb.ini",
                    "pkg2img"
                ]
                try:
                    p_pkg = subprocess.run(cmd_pkg, cwd=work_dir, capture_output=True, text=True, timeout=15.0)
                    if p_pkg.returncode != 0:
                        log(f"pkg2img 提示: {p_pkg.stderr or p_pkg.stdout} (尝试继续推进)")
                except subprocess.TimeoutExpired:
                    log("pkg2img 超时 (15s)，尝试继续推进")

        # 5. 执行 probe (仅 EC718PV 支持，EC618 官方原厂免 probe，由 burnbatch 内置 burnag 自驱握手)
        if chip == "ec718pv":
            emit(50, "正在与芯片 Bootloader 握手 (probe)...")
            cmd_probe = [
                FLASHTOOL_CLI,
                "--cfgfile", "config_pkg_product_usb.ini",
                "--port", boot_port,
                "probe"
            ]
            try:
                p_probe = subprocess.run(cmd_probe, cwd=work_dir, capture_output=True, text=True, timeout=12.0)
                if p_probe.returncode != 0:
                    return {"ok": False, "msg": f"芯片握手失败 (probe failed): {p_probe.stderr or p_probe.stdout}"}
            except subprocess.TimeoutExpired:
                return {"ok": False, "msg": f"芯片握手超时 (probe timed out after 12s on {boot_port})"}
        else:
            log("[EC618] 原厂流跳过独立 probe 指令，直接由 burnbatch 内部 burnag 驱动握手与固件写入")
            emit(50, "正在连接 EC618 Bootloader...")

        # 6. 执行 burnbatch (固件写入)
        imglist = chip_cfg["full_imglist"] if mode == "full" else ["flexfile2"]
        mode_str = "全量系统刷写 (Full Flash)" if mode == "full" else "应用脚本更新 (Script Flash)"
        emit(70, f"握手成功！正在执行{mode_str}...")

        cmd_burn = [
            FLASHTOOL_CLI,
            "--cfgfile", "config_pkg_product_usb.ini",
            "--port", boot_port,
        ]
        if chip == "ec718pv":
            cmd_burn.extend(["--skipconnect", "1"])

        cmd_burn.extend(["burnbatch", "--imglist"] + imglist)

        try:
            p_burn = subprocess.run(cmd_burn, cwd=work_dir, capture_output=True, text=True, timeout=float(watchdog_timeout))
            if p_burn.returncode != 0:
                return {"ok": False, "msg": f"固件写入失败 (burnbatch failed): {p_burn.stderr or p_burn.stdout}"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "msg": f"固件烧写超时 (burnbatch timed out after {watchdog_timeout}s on {boot_port})"}

        # 7. 执行 sysreset (安全软复位重启)
        emit(90, "固件写入完成，正在平滑复位重启模组...")
        cmd_reset = [
            FLASHTOOL_CLI,
            "--cfgfile", "config_pkg_product_usb.ini",
            "--port", boot_port,
            "--skipconnect", "1",
            "sysreset"
        ]
        try:
            subprocess.run(cmd_reset, cwd=work_dir, capture_output=True, text=True, timeout=5)
        except Exception:
            pass

        emit(100, f"模组已成功平滑重启！{chip_cfg['name']} 固件烧录圆满完成。")
        return {
            "ok": True,
            "msg": f"上位机线刷成功 ({chip_cfg['name']} · {mode_str})，模组已重启生效",
            "port": boot_port,
            "chip": chip,
            "mode": mode
        }

    finally:
        # 清理沙箱临时目录
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass

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
