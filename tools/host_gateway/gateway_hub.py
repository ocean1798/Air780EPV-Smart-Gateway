# -*- coding: utf-8 -*-
"""
Air780 系列智能通信网关 - 本地多设备共享中枢 (Multi-Dongle Cluster & Dynamic Session Pool)
功能：
1. 动态探测并管理 1~N 块移芯/合宙 4G 模组（Air780EPV / Air780EC / Air780E / Air700E 等）的用户通信口 (x.6/VUART_0)；
2. 为每个物理设备开辟独立的 DongleSession 链路与心跳重连机制，分配动态卡槽 (slot_1 ~ slot_n)，支持即插即用热插拔；
3. 绑定 127.0.0.1:17800 端口并实现 Windows Mutex 单例独占保护；
4. 将多板卡上报的 NDJSON 事件（状态、短信、验证码、来电）注入设备卡槽元数据，防串台广播给所有在线客户端；
5. 收到板端短信后借用电脑宽带代推全渠道通知，代推成功后定向向原卡板回写 Push ACK 确认；
6. 汇聚各客户端（Web 控制台、FastMCP）下发的指令，支持定向卡槽路由与缺省主卡向下兼容。
"""

import sys
import os
import re
import time
import uuid
import json
import socket
import select
import serial
import serial.tools.list_ports
import threading
import hashlib
import hmac
import base64
import urllib.request
import urllib.parse
import urllib.error
from typing import List, Dict, Any, Optional, Tuple

from cluster_health import ClusterHealthMonitor, HealthState
from cluster_router import ClusterRouter, RouteStrategy, detect_sim_carrier

HUB_HOST = "127.0.0.1"
HUB_PORT = 17800
SERIAL_BAUD = 115200
SERIAL_PORT: Optional[str] = None  # 兼容旧版，集群架构下默认由会话池自动探测

class _SafeStream:
    def write(self, msg): pass
    def flush(self): pass

if sys.stdout is None:
    sys.stdout = _SafeStream()
if sys.stderr is None:
    sys.stderr = _SafeStream()

def get_config_dir() -> str:
    """获取配置持久化目录：若在 PyInstaller 冻结环境，取 exe 所在目录；否则取源码目录"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

GATEWAY_CONFIG_PATH = os.path.join(get_config_dir(), "gateway_config.json")
DATA_DIR = get_config_dir()

def _redact_log_text(msg: Any) -> str:
    """脱敏日志中的手机号、验证码与密钥"""
    text = str(msg)
    text = re.sub(r"https?://[^\s\"'<>]+", "<endpoint>", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!\d)(?:\+?86[\s-]?)?1\d{10}(?!\d)", "<phone>", text)
    text = re.sub(r"(?i)(验证码|otp|pin|code)(\s*[:：=]\s*)\d{4,8}", r"\1\2<redacted>", text)
    text = re.sub(r"(?i)([?&](?:token|secret|sign|signature|password|key)=)[^&#\s]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)((?:\"|')?(?:secret|token|password|credential|webhook_secret)(?:\"|')?\s*[:=]\s*(?:\"|'))[^\"']*(\")", r"\1<redacted>\2", text)
    return text

def log(msg: str):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{now}] [Hub] {_redact_log_text(msg)}\n"
    if sys.stderr is not None:
        try:
            sys.stderr.write(formatted)
            sys.stderr.flush()
        except Exception:
            pass
    try:
        log_p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub_debug.log")
        with open(log_p, "a", encoding="utf-8") as f:
            f.write(formatted)
    except Exception:
        pass

def set_windows_clipboard(text: str) -> bool:
    """免依赖使用 ctypes 调用 Windows API 将文本原子写入系统剪贴板（含重试防抖）"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002

        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE

        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL

        opened = False
        for _ in range(5):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.05)
        if not opened:
            return False

        user32.EmptyClipboard()
        data = text.encode("utf-16le") + b"\x00\x00"
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h_mem:
            user32.CloseClipboard()
            return False

        p_mem = kernel32.GlobalLock(h_mem)
        if not p_mem:
            kernel32.GlobalFree(h_mem)
            user32.CloseClipboard()
            return False

        ctypes.memmove(p_mem, data, len(data))
        kernel32.GlobalUnlock(h_mem)
        user32.SetClipboardData(CF_UNICODETEXT, h_mem)
        user32.CloseClipboard()
        return True
    except Exception as e:
        log(f"写入 Windows 剪贴板异常: {e}")
        try:
            import ctypes
            ctypes.windll.user32.CloseClipboard()
        except Exception:
            pass
        return False

def get_windows_clipboard() -> Optional[str]:
    """免依赖使用 ctypes 调用 Windows API 从系统剪贴板读取文本"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        CF_UNICODETEXT = 13
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE

        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        opened = False
        for _ in range(5):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.05)
        if not opened:
            return None

        h_mem = user32.GetClipboardData(CF_UNICODETEXT)
        if not h_mem:
            user32.CloseClipboard()
            return None

        p_mem = kernel32.GlobalLock(h_mem)
        if not p_mem:
            user32.CloseClipboard()
            return None

        text = ctypes.wstring_at(p_mem)
        kernel32.GlobalUnlock(h_mem)
        user32.CloseClipboard()
        return text
    except Exception as e:
        try:
            import ctypes
            ctypes.windll.user32.CloseClipboard()
        except Exception:
            pass
        return None


def show_windows_toast(title: str, message: str):
    """通过 PowerShell 异步向 Windows 屏幕右下角弹出一个原生系统 Toast 通知"""
    if sys.platform != "win32":
        return
    ps_cmd = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$toastXml = [xml]$template.GetXml()
$toastXml.GetElementsByTagName('text')[0].AppendChild($toastXml.CreateTextNode('{title}')) | Out-Null
$toastXml.GetElementsByTagName('text')[1].AppendChild($toastXml.CreateTextNode('{message}')) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($toastXml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Air780Gateway').Show($toast)
"""
    def _run():
        try:
            import subprocess
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True,
                timeout=5,
                **kwargs
            )
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()

def find_cellular_vuart_port(ports_list: Optional[List[Any]] = None) -> Optional[str]:
    """
    查找首个有效的 4G VUART 通信端口 (严格排除 17D1 烧录端口与非 x.6 端口)。
    """
    if ports_list is None:
        try:
            ports_list = list(serial.tools.list_ports.comports())
        except Exception:
            ports_list = []
    for p in ports_list:
        hwid = (getattr(p, "hwid", "") or "").upper()
        vid_attr = getattr(p, "vid", 0) or 0
        vid = hex(vid_attr).upper() if isinstance(vid_attr, int) and vid_attr else str(vid_attr).upper()
        loc = getattr(p, "location", "") or ""
        if "17D1" in vid or "17D1" in hwid:
            continue
        if "19D1:0001" in hwid or ("19D1" in vid and ("0001" in str(getattr(p, "pid", 0)) or getattr(p, "pid", 0) == 1)):
            if loc.endswith("x.6") or ":X.6" in loc.upper() or "MI_06" in hwid or "X.6" in hwid or getattr(p, "pid", 0) == 1:
                return getattr(p, "device", None)
    return None

def scan_all_cellular_ports() -> List[Dict[str, Any]]:
    """
    智能扫描系统所有合宙/移芯 4G 模组的用户通信主端口 (VID:PID = 19D1:0001, x.6 / MI_06 / VUART_0)。
    严格过滤掉 x.2 (AT/控制口)、x.4 (Trace/日志口) 以及 x.0 刷机口，确保仅返回业务数据通信口。
    """
    results = []
    try:
        ports = list(serial.tools.list_ports.comports())
        for p in ports:
            hwid = (p.hwid or "").upper()
            vid = hex(p.vid).upper() if p.vid else ""
            pid = hex(p.pid).upper() if p.pid else ""
            loc = getattr(p, "location", "") or ""

            if "19D1:0001" in hwid or ("19D1" in vid and ("0001" in pid or "1" in pid)):
                # 必须满足用户通信口判定：location 以 x.6 结尾或包含 :X.6 或 MI_06
                if loc.endswith("x.6") or ":X.6" in loc.upper() or "MI_06" in hwid or "X.6" in hwid:
                    results.append({
                        "port": p.device,
                        "desc": p.description,
                        "hwid": p.hwid,
                        "loc": loc
                    })
    except Exception as e:
        log(f"扫描串口异常: {e}")
    return results

def test_channel_push(channel: str, cfg: Dict[str, Any], device_desc: Optional[str] = None) -> Dict[str, Any]:
    """单渠道连通性测试 (Test Ping)"""
    url = (cfg.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "URL 不能为空", "cost_ms": 0}

    secret = (cfg.get("secret") or "").strip()
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    dev_label = device_desc or "Air780 集群网关"
    title = f"🔔 {dev_label} 连通性测试"
    payload_dict = {}
    target_url = url

    if channel == "feishu":
        card = {
            "schema": "2.0",
            "config": {
                "update_multi": True,
                "style": {"text_size": {"normal_v2": {"default": "normal", "pc": "normal", "mobile": "heading"}}}
            },
            "header": {
                "title": {"tag": "plain_text", "content": "🔔 连通性测试正常"},
                "template": "green"
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [
                    {"tag": "markdown", "content": f"**测试渠道：** 飞书自定义机器人\n**测试时间：** {now_str}"},
                    {"tag": "hr"},
                    {"tag": "markdown", "content": "**验证码示例：**\n```text\n886622\n```"},
                    {"tag": "hr"},
                    {"tag": "div", "text": {"tag": "lark_md", "content": f"<font color='grey'>测试设备: {dev_label} （电脑本地宽带代推）</font>"}}
                ]
            }
        }
        payload_dict = {"msg_type": "interactive", "card": card}
        if secret:
            ts = str(int(time.time()))
            sign_str = f"{ts}\n{secret}"
            hmac_code = hmac.new(sign_str.encode("utf-8"), digestmod=hashlib.sha256).digest()
            payload_dict["timestamp"] = ts
            payload_dict["sign"] = base64.b64encode(hmac_code).decode("utf-8")

    elif channel == "wecom":
        payload_dict = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"### 🔔 {dev_label} 连通性测试\n> **测试渠道**: 企业微信机器人\n> **测试时间**: {now_str}\n> **测试状态**: <font color=\"info\">连通正常</font>"
            }
        }
    elif channel == "dingtalk":
        payload_dict = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": f"### 🔔 {dev_label} 连通性测试\n> **测试渠道**: 钉钉机器人\n> **测试时间**: {now_str}\n> **测试状态**: 连通正常"
            }
        }
        if secret:
            ts = str(round(time.time() * 1000))
            string_to_sign = f"{ts}\n{secret}"
            hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code).decode("utf-8"))
            target_url = f"{url}&timestamp={ts}&sign={sign}" if "?" in url else f"{url}?timestamp={ts}&sign={sign}"

    elif channel == "bark":
        group = (cfg.get("group") or "Air780Gateway").strip()
        sound = (cfg.get("sound") or "minuet").strip()
        payload_dict = {
            "title": title,
            "body": f"测试时间: {now_str}\n渠道状态: 连通正常",
            "group": group,
            "sound": sound,
            "copy": "886622"
        }

    elif channel == "webhook":
        payload_dict = {
            "device": dev_label,
            "type": "test_ping",
            "time": now_str,
            "timestamp": int(time.time()),
            "message": f"{dev_label} 连通性测试正常"
        }

    try:
        data_bytes = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(target_url, data=data_bytes, headers={"Content-Type": "application/json; charset=utf-8"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=5) as resp:
            cost_ms = int((time.time() - t0) * 1000)
            return {"ok": resp.status == 200, "status_code": resp.status, "cost_ms": cost_ms}
    except urllib.error.HTTPError as he:
        return {"ok": False, "status_code": he.code, "error": f"HTTP {he.code}", "cost_ms": 0}
    except Exception as e:
        return {"ok": False, "error": str(e), "cost_ms": 0}


def probe_dongle_fingerprint_from_companion(loc: str, port: str) -> Dict[str, str]:
    """
    当主通信串口 (x.6 / VUART_0) 未上报 imei 或 iccid 时（如旧固件 v1.2.4），
    通过模组的同物理 USB 拓扑定位伴生 REPL / CDC 端口 (x.2) 或 AT 端口，
    下发探测指令获取真机物理指纹并固化。
    """
    if not loc:
        return {}

    target_loc_prefix = loc.split(":")[0] if ":" in loc else ""
    candidate_port = None
    try:
        for p in serial.tools.list_ports.comports():
            ploc = getattr(p, "location", "") or ""
            if target_loc_prefix and ploc.startswith(target_loc_prefix):
                # 优先匹配 x.2 (REPL / AT 伴生口)
                if ":X.2" in ploc.upper() or ploc.endswith("x.2"):
                    candidate_port = p.device
                    break
    except Exception:
        pass

    if not candidate_port:
        return {}

    result = {}
    try:
        with serial.Serial(candidate_port, baudrate=115200, timeout=1.0) as ser:
            ser.dtr = True
            ser.rts = True
            # 1. 优先通过 LuatOS REPL 模式探测真机硬件指纹
            ser.write(b'print("FP_START", mobile and mobile.imei and mobile.imei(), mobile and mobile.iccid and mobile.iccid(), "FP_END")\r\n')
            time.sleep(0.2)
            raw = ser.read(1024).decode("utf-8", errors="ignore")
            if "FP_START" in raw and "FP_END" in raw:
                parts = raw.split("FP_START")[1].split("FP_END")[0].strip().split()
                if len(parts) >= 2:
                    if parts[0] != "nil" and len(parts[0]) >= 14:
                        result["imei"] = parts[0]
                    if parts[1] != "nil" and len(parts[1]) >= 15:
                        result["iccid"] = parts[1]

            # 2. 若 REPL 模式未读出，兼容标准 AT 模式探测
            if not result.get("imei") or not result.get("iccid"):
                ser.write(b"AT+CGSN\r\n")
                time.sleep(0.1)
                at_cgsn = ser.read(256).decode("utf-8", errors="ignore")
                for line in at_cgsn.splitlines():
                    digits = re.sub(r"\D", "", line)
                    if len(digits) == 15 and not result.get("imei"):
                        result["imei"] = digits
                        break

                ser.write(b"AT+ICCID\r\n")
                time.sleep(0.1)
                at_iccid = ser.read(256).decode("utf-8", errors="ignore")
                for line in at_iccid.splitlines():
                    digits = re.sub(r"\D", "", line)
                    if len(digits) in (19, 20) and not result.get("iccid"):
                        result["iccid"] = digits
                        break
    except Exception:
        pass

    return result


# =========================================================================
# 核心类：DongleSession (单模组硬件物理串口独立会话)
# =========================================================================

class DongleSession:
    """
    单个 4G 模组硬件的物理串口独立会话实例。
    每个 Session 独占管理一个物理 COM 端口的打开、收发、半帧缓冲与生命周期。
    """
    def __init__(self, port: str, loc: str, slot_id: str, hub: 'GatewayHub', baud: int = SERIAL_BAUD):
        self.port = port
        self.loc = loc
        self.slot_id = slot_id
        self.hub = hub
        self.baud = baud

        self.ser: Optional[serial.Serial] = None
        self.serial_lock = threading.Lock()
        self.is_connected = False
        self.is_flashing = False
        self.running = False
        self.rx_thread: Optional[threading.Thread] = None
        self.rx_buffer = bytearray()

        # 板端元数据与状态缓存
        self.meta: Dict[str, Any] = {
            "slot": slot_id,
            "port": port,
            "loc": loc,
            "model": "Air780 Series",
            "bsp": "Unknown",
            "imei": "",
            "iccid": "",
            "version": "",
            "chip": "",
            "capabilities": {},
            "phone": "",
            "csq": 0,
            "rsrp": 0,
            "temp": "",
            "vbat": "",
            "net_ready": False,
            "online": False
        }
        self.latest_status: Dict[str, Any] = {}
        self.board_cellular_data: bool = False
        self._companion_probed: bool = False

    @property
    def capabilities(self) -> Dict[str, Any]:
        return self.meta.get("capabilities", {})

    @property
    def model(self) -> str:
        return self.meta.get("model", "")

    def start(self):
        """启动会话的后台接收与保活线程"""
        self.running = True
        self.rx_thread = threading.Thread(target=self._session_loop, daemon=True)
        self.rx_thread.start()

    def stop(self):
        """优雅关闭会话与物理串口"""
        self.running = False
        with self.serial_lock:
            if self.ser:
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None
            self.is_connected = False
            self.meta["online"] = False

    def send_line(self, line: str) -> bool:
        """线程安全向专属物理串口写入一行指令（自动追加 \\r\\n）"""
        clean_line = line.strip()
        if not clean_line:
            return False
        with self.serial_lock:
            if not self.ser or not self.ser.is_open:
                return False
            try:
                self.ser.write((clean_line + "\r\n").encode("utf-8"))
                self.ser.flush()
                return True
            except Exception as e:
                log(f"[{self.slot_id} | {self.port}] 写入串口异常: {e}")
                try:
                    if self.ser:
                        self.ser.close()
                except Exception:
                    pass
                self.ser = None
                self.is_connected = False
                self.meta["online"] = False
                return False

    def ack_push(self, msg_id: str, status: str = "ok"):
        """定向向本板回写 Push ACK 确认"""
        if not msg_id:
            return
        ack_pkt = json.dumps({"type": "cmd", "id": f"ack_{int(time.time()*1000)}", "cmd": "notify_ack", "data": {"id": msg_id, "status": status}})
        self.send_line(ack_pkt)

    def _probe_companion_fingerprint_once(self, force: bool = False):
        """若固件响应未包含 imei/iccid，通过伴生端口安全探测真机指纹 (IMEI 与当前 SIM 的 ICCID)"""
        if not force and self.meta.get("imei") and self.meta.get("iccid"):
            return
        now = time.time()
        last_attempt = getattr(self, "_last_companion_attempt", 0)
        if not force and (now - last_attempt < 2.0):  # 2 秒冷却防频繁
            return
        self._last_companion_attempt = now
        try:
            fps = probe_dongle_fingerprint_from_companion(self.loc, self.port)
            if fps.get("imei"):
                self.meta["imei"] = fps["imei"]
                log(f"[{self.slot_id}] 伴生端口探测成功补齐机身号 IMEI: {fps['imei']}")
                if self.loc and hasattr(self.hub, "session_pool"):
                    self.hub.session_pool.fingerprint_cache[self.loc] = fps["imei"]
            if fps.get("iccid"):
                self.meta["iccid"] = fps["iccid"]
                log(f"[{self.slot_id}] 伴生端口探测成功补齐卡号 ICCID: {fps['iccid']}")
        except Exception as e:
            log(f"[{self.slot_id}] 伴生端口探测异常: {e}")

    def pause_for_flash(self):
        """挂起物理串口以供上位机 FlashToolCLI 独占烧录"""
        self.is_flashing = True
        with self.serial_lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.write(b"AT+ECRST=delay,799\r\n")
                    self.ser.flush()
                    time.sleep(0.05)
                    self.ser.write(b"~\x00\x02~")
                    self.ser.flush()
                    time.sleep(0.1)
                    self.ser.close()
                except Exception:
                    pass
            self.ser = None
            self.is_connected = False
            self.meta["online"] = False
        log(f"[{self.slot_id}] 物理串口已安全释放，等待进入 Bootloader 模式")

    def resume_after_flash(self):
        """烧录完成后恢复串口轮询与守护重连"""
        self.is_flashing = False
        self._ensure_serial_opened()
        log(f"[{self.slot_id}] 硬件烧录任务结束，已恢复物理串口守护")

    def _ensure_serial_opened(self) -> bool:
        if getattr(self, "is_flashing", False):
            return False
        with self.serial_lock:
            if self.ser and self.ser.is_open:
                try:
                    _ = self.ser.in_waiting
                    if not self.is_connected:
                        self.is_connected = True
                        self.meta["online"] = True
                        log(f"[{self.slot_id}] 物理串口连接正常: {self.port}")
                        # 模组初次连接或重新连接上线时，强制探测一次以防插拔换卡导致 ICCID 残留旧值
                        self._probe_companion_fingerprint_once(force=True)
                    return True
                except Exception:
                    try:
                        self.ser.close()
                    except Exception:
                        pass
                    self.ser = None
                    self.is_connected = False
                    self.meta["online"] = False

            try:
                self.ser = serial.Serial(self.port, self.baud, timeout=0.5)
                self.ser.dtr = True
                self.ser.rts = True
                self.is_connected = True
                self.meta["online"] = True
                log(f"[{self.slot_id}] 成功打开物理串口: {self.port} @ {self.baud}")
                self._probe_companion_fingerprint_once(force=True)
                return True
            except Exception as e:
                self.ser = None
                self.is_connected = False
                self.meta["online"] = False
                return False

    def _session_loop(self):
        """会话专属的读写循环，处理 NDJSON 解包、粘包和断线重连"""
        log(f"[{self.slot_id}] 会话守护线程已启动 (目标端口: {self.port})")
        has_probed = False

        while self.running:
            if not self._ensure_serial_opened():
                has_probed = False
                time.sleep(1.0)
                continue

            if not has_probed:
                has_probed = True
                self.is_flashing = False
                time.sleep(0.2)
                self.send_line(json.dumps({"type": "cmd", "id": f"init_{self.slot_id}", "cmd": "get_status"}))
                time.sleep(1.0)
                continue

            try:
                # 状态自愈探针：若尚未识别出型号或每隔 4 秒，主动下发 get_status 保持指标鲜活
                now = time.time()
                if self.ser and self.ser.is_open:
                    is_tx_busy = (now < getattr(self, "_sms_tx_busy_until", 0.0))
                    need_poll = not getattr(self, "is_flashing", False) and not is_tx_busy and (
                        (self.meta.get("bsp") in ("Unknown", "", None)) or 
                        (now - getattr(self, "_last_status_poll", 0.0) >= 4.0)
                    )
                    if need_poll and (now - getattr(self, "_last_poll_send", 0.0) >= 1.5):
                        self._last_poll_send = now
                        self._last_status_poll = now
                        try:
                            probe_pkt = json.dumps({"type": "cmd", "id": f"poll_{self.slot_id}", "cmd": "get_status"}) + "\r\n"
                            with self.serial_lock:
                                self.ser.write(probe_pkt.encode("utf-8"))
                                self.ser.flush()
                        except Exception:
                            pass

                line_bytes = b""
                if self.ser and self.ser.is_open:
                    line_bytes = self.ser.readline()
                else:
                    time.sleep(0.5)
                    continue

                if not line_bytes:
                    continue

                self.rx_buffer.extend(line_bytes)
                if len(self.rx_buffer) > 64 * 1024:
                    self.rx_buffer.clear()
                    log(f"[{self.slot_id}] 串口半帧超过 64 KiB，已丢弃缓冲")
                    continue

                while b"\n" in self.rx_buffer:
                    line_bytes, _, remaining = self.rx_buffer.partition(b"\n")
                    self.rx_buffer = bytearray(remaining)
                    try:
                        line = line_bytes.rstrip(b"\r").decode("utf-8", errors="ignore")
                    except Exception:
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(obj, dict):
                        continue

                    # 1. 为上报报文打上专属卡槽与端口元数据标签
                    obj["slot"] = self.slot_id
                    obj["port"] = self.port
                    if isinstance(obj.get("data"), dict):
                        obj["data"]["slot"] = self.slot_id
                        obj["data"]["port"] = self.port

                    # 2. 更新本会话内部状态
                    self._update_session_state(obj)

                    # 3. 提交给 Hub 统一流转与分发
                    self.hub.on_session_frame(self, obj, json.dumps(obj, ensure_ascii=False))

            except (serial.SerialException, OSError) as e:
                log(f"[{self.slot_id}] 物理串口发生瞬断: {e}")
                with self.serial_lock:
                    if self.ser:
                        try:
                            self.ser.close()
                        except Exception:
                            pass
                        self.ser = None
                    self.is_connected = False
                    self.meta["online"] = False
                time.sleep(1.0)
            except Exception as e:
                log(f"[{self.slot_id}] 会话未捕获异常: {e}")
                time.sleep(0.5)

    def _update_session_state(self, obj: Dict[str, Any]):
        """从板端报文中提取最新状态指标"""
        frame_type = obj.get("type")
        evt = obj.get("event")
        data = obj.get("data")
        if not isinstance(data, dict):
            return

        is_status_frame = frame_type in ("response", "res") or (frame_type == "event" and evt in ("status", "state_change", "gateway_ready"))
        if is_status_frame:
            self.latest_status.update(data)
            if "bsp" in data and data["bsp"]: self.meta["bsp"] = data["bsp"]
            if "version" in data and data["version"]: self.meta["version"] = data["version"]
            if "imei" in data and data["imei"]: self.meta["imei"] = data["imei"]
            if "iccid" in data and data["iccid"]: self.meta["iccid"] = data["iccid"]
            if "number" in data and data["number"]: self.meta["phone"] = data["number"]
            if "phone" in data and data["phone"]: self.meta["phone"] = data["phone"]
            if "csq" in data and data["csq"] is not None:
                try: self.meta["csq"] = int(data["csq"])
                except Exception: pass
            if "rsrp" in data and data["rsrp"] is not None:
                try: self.meta["rsrp"] = int(data["rsrp"])
                except Exception: pass
            if "temp" in data and data["temp"] is not None: self.meta["temp"] = str(data["temp"])
            if "vbat" in data and data["vbat"] is not None: self.meta["vbat"] = str(data["vbat"])
            if "uptime_seconds" in data: self.meta["uptime"] = data["uptime_seconds"]
            elif "uptime" in data: self.meta["uptime"] = data["uptime"]
            if "blackbox_count" in data: self.meta["sms_count"] = data["blackbox_count"]
            elif "sms_count" in data: self.meta["sms_count"] = data["sms_count"]
            if "net_ready" in data: self.meta["net_ready"] = bool(data["net_ready"])
            if "cellular_data" in data: self.board_cellular_data = bool(data["cellular_data"])
            if "rndis" in data: self.meta["rndis"] = bool(data["rndis"])
            elif "rndis_enable" in data: self.meta["rndis"] = bool(data["rndis_enable"])
            if "capabilities" in data and isinstance(data["capabilities"], dict):
                self.meta["capabilities"] = data["capabilities"]
            self.meta["online"] = True

            # 若固件响应未包含 imei/iccid，触发伴生口探测补充
            if not self.meta.get("imei") or not self.meta.get("iccid"):
                self._probe_companion_fingerprint_once(force=False)

            # 智能型号归一化
            raw_model = str(data.get("model") or "")
            bsp = str(self.meta.get("bsp", ""))
            if "780EPV" in raw_model or "780EPV" in bsp or "EC718P" in bsp:
                self.meta["model"] = "Air780EPV"
            elif "780EC" in raw_model or "780EC" in bsp:
                self.meta["model"] = "Air780EC"
            elif "780E" in raw_model or "780E" in bsp or "EC618" in bsp:
                self.meta["model"] = "Air780E"
            elif "700E" in raw_model or "700E" in bsp:
                self.meta["model"] = "Air700E"
            elif raw_model:
                self.meta["model"] = raw_model

            # 若为初始状态探测帧，主动广播最新的集群卡槽汇总
            if str(obj.get("id", "")).startswith("init_"):
                self.hub.broadcast_json({
                    "type": "event",
                    "event": "cluster_status",
                    "slot": self.slot_id,
                    "data": {"slots": self.hub.session_pool.list_all_summaries()}
                })

        elif frame_type == "event":
            evt = obj.get("event")
            if evt == "gateway_ready":
                if "bsp" in data: self.meta["bsp"] = data["bsp"]
                if "version" in data: self.meta["version"] = data["version"]
                if "imei" in data and data["imei"]: self.meta["imei"] = data["imei"]
                if "iccid" in data and data["iccid"]: self.meta["iccid"] = data["iccid"]
                if "capabilities" in data: self.meta["capabilities"] = data["capabilities"]
                bsp = str(self.meta.get("bsp", ""))
                if "780EPV" in bsp or "EC718P" in bsp: self.meta["model"] = "Air780EPV"
                elif "780EC" in bsp: self.meta["model"] = "Air780EC"
                elif "780E" in bsp or "EC618" in bsp: self.meta["model"] = "Air780E"
            elif evt == "state_change":
                if "csq" in data: self.meta["csq"] = data["csq"]
                if "rsrp" in data: self.meta["rsrp"] = data["rsrp"]
                if "temp" in data: self.meta["temp"] = str(data["temp"])
                if "vbat" in data: self.meta["vbat"] = str(data["vbat"])
                if "cellular_data" in data: self.board_cellular_data = bool(data["cellular_data"])

    def get_summary(self) -> Dict[str, Any]:
        """获取当前会话的对外简要看板信息"""
        return {
            "slot": self.slot_id,
            "port": self.port,
            "loc": self.loc,
            "model": self.meta.get("model", "Air780"),
            "bsp": self.meta.get("bsp", ""),
            "imei": self.meta.get("imei", ""),
            "iccid": self.meta.get("iccid", ""),
            "phone": self.meta.get("phone", ""),
            "version": self.meta.get("version", ""),
            "online": self.is_connected,
            "net_ready": self.meta.get("net_ready", False),
            "csq": self.meta.get("csq", 0),
            "rsrp": self.meta.get("rsrp", 0),
            "temp": self.meta.get("temp", ""),
            "vbat": self.meta.get("vbat", ""),
            "uptime": self.meta.get("uptime", 0),
            "sms_count": self.meta.get("sms_count", 0),
            "rndis": bool(self.meta.get("rndis", False)),
            "cellular_data": self.board_cellular_data,
            "capabilities": self.meta.get("capabilities", {})
        }


# =========================================================================
# 核心类：DongleSessionPool (1~N 模组动态会话池管理器)
# =========================================================================

class DongleSessionPool:
    """
    负责 1~N 个模组的自动探测、卡槽分配 (slot_1..N)、会话增删及命令路由。
    """
    def __init__(self, hub: 'GatewayHub'):
        self.hub = hub
        self.sessions: Dict[str, DongleSession] = {}  # port -> DongleSession
        self.pool_lock = threading.RLock()
        self.running = False
        self.scanner_thread: Optional[threading.Thread] = None

        # 卡槽历史记忆 (以 loc 或 port 为 key，确保拔插后分配同一卡槽)
        self.slot_history: Dict[str, str] = {}
        # 模组硬件指纹缓存池 (拔插/复位不丢 IMEI/ICCID)
        self.fingerprint_cache: Dict[str, Dict[str, str]] = {}
        # 未分配/出厂态模组识别列表 (AIR-35)
        self.unassigned_dongles: List[Dict[str, Any]] = []
        self._last_unassigned_broadcast: float = 0.0

    def start(self):
        self.running = True
        # 初始同步扫描一次
        self._sync_ports()
        self.scanner_thread = threading.Thread(target=self._scan_loop, daemon=True)
        self.scanner_thread.start()

    def stop(self):
        self.running = False
        with self.pool_lock:
            for s in list(self.sessions.values()):
                s.stop()
            self.sessions.clear()

    def _allocate_slot(self, port: str, loc: str) -> str:
        """为新发现的端口分配稳定卡槽 ID（拓扑亲和性，防止卡槽漂移倒错）"""
        with self.pool_lock:
            used_slots = set(s.slot_id for s in self.sessions.values())

            # 1. 物理拓扑亲和性：1-8 固定为主机卡槽 slot_1，1-13 固定为拓展 Hub 卡槽 slot_2
            if loc:
                clean_loc = loc.split(":")[0] if ":" in loc else loc
                if clean_loc.startswith("1-8") and "slot_1" not in used_slots:
                    if loc: self.slot_history[loc] = "slot_1"
                    self.slot_history[port] = "slot_1"
                    return "slot_1"
                elif clean_loc.startswith("1-13") and "slot_2" not in used_slots:
                    if loc: self.slot_history[loc] = "slot_2"
                    self.slot_history[port] = "slot_2"
                    return "slot_2"

            # 2. 优先复用历史分配记录
            hist = self.slot_history.get(loc) or self.slot_history.get(port)
            if hist and hist not in used_slots:
                return hist

            # 3. 贪心寻找最小未被使用的 slot_1, slot_2, ...
            idx = 1
            while True:
                candidate = f"slot_{idx}"
                if candidate not in used_slots:
                    if loc: self.slot_history[loc] = candidate
                    self.slot_history[port] = candidate
                    return candidate
                idx += 1

    def _sync_ports(self):
        """执行一次全量串口扫描并执行增量会话同步"""
        detected_list = scan_all_cellular_ports()
        detected_ports = set(d["port"] for d in detected_list)

        with self.pool_lock:
            current_ports = set(self.sessions.keys())

            # 发现新接入端口
            for d in detected_list:
                p = d["port"]
                if p not in current_ports:
                    loc = d["loc"]
                    slot_id = self._allocate_slot(p, loc)
                    session = DongleSession(p, loc, slot_id, self.hub)
                    cached_imei = self.fingerprint_cache.get(loc) or self.fingerprint_cache.get(p)
                    if cached_imei and not session.meta.get("imei"):
                        session.meta["imei"] = cached_imei
                    self.sessions[p] = session
                    session.start()
                    log(f"⚡ [CLUSTER] 发现新卡板上线: {p} (位置: {loc}) -> 分配卡槽: 【{slot_id}】")
                    # 广播模组连接事件
                    self.hub.broadcast_json({
                        "type": "event",
                        "event": "dongle_connected",
                        "slot": slot_id,
                        "data": session.get_summary()
                    })

            # 检测拔出断开端口
            for p in list(current_ports):
                if p not in detected_ports:
                    session = self.sessions.pop(p)
                    slot_id = session.slot_id
                    session.stop()
                    log(f"⚠️ [CLUSTER] 检测到卡板断开拔出: {p} (曾用卡槽: 【{slot_id}】)")
                    # 广播模组断开事件
                    self.hub.broadcast_json({
                        "type": "event",
                        "event": "dongle_disconnected",
                        "slot": slot_id,
                        "data": {"slot": slot_id, "port": p, "online": False}
                    })

            # AIR-35: 探测未绑定的移芯出厂态/Bootloader 端口 (全量安全嗅探，无竞争)
            try:
                self._sniff_unassigned_dongles(current_ports)
            except Exception as e:
                log(f"未分配模组探测异常: {e}")

    def _sniff_unassigned_dongles(self, bound_ports: set):
        """
        AIR-35: 扫描系统上未绑定到 DongleSession 的移芯/合宙 4G 模组端口
        识别出厂标准 AT 固件 (19D1) 或 ROM Bootloader 态 (17D1)
        """
        unassigned = []
        all_coms = list(serial.tools.list_ports.comports())
        
        # 1. 寻找未绑定的 Bootloader (17D1:0001)
        for p in all_coms:
            hwid = (p.hwid or "").upper()
            vid = p.vid
            pid = p.pid
            is_boot = (vid == 0x17D1 and pid == 0x0001) or "17D1:0001" in hwid or ("17D1" in (hex(vid or 0).upper()) and "0001" in (hex(pid or 0).upper()))
            if is_boot and p.device not in bound_ports:
                unassigned.append({
                    "port": p.device,
                    "desc": p.description or "Air780 Bootloader",
                    "mode": "bootloader",
                    "chip": "unknown",
                    "model_guess": "合宙移芯模组 (刷机模式)",
                    "recommend_flash": "full"
                })

        # 2. 寻找未绑定到会话池的 19D1 端口（如全新裸板插上，出厂仅有 AT 固件，或用户通信口 x.6 未运行智能网关固件）
        active_session_loc_prefixes = set()
        for s in self.sessions.values():
            if s.loc and ":" in s.loc:
                active_session_loc_prefixes.add(s.loc.split(":")[0])

        for p in all_coms:
            dev = p.device
            if dev in bound_ports:
                continue
            hwid = (p.hwid or "").upper()
            vid = hex(p.vid or 0).upper()
            pid = hex(p.pid or 0).upper()
            loc = getattr(p, "location", "") or ""
            
            # 若是已在线卡槽的伴生口 (x.2 / x.4)，跳过不作为全新模块报出
            loc_prefix = loc.split(":")[0] if (loc and ":" in loc) else ""
            if loc_prefix and loc_prefix in active_session_loc_prefixes:
                continue

            # 仅嗅探 19D1 的 AT 控制口 (x.2) 或主通信口 (x.6)
            if ("19D1:0001" in hwid or ("19D1" in vid and "0001" in pid)):
                if loc.endswith("x.2") or ":X.2" in loc.upper() or loc.endswith("x.6") or ":X.6" in loc.upper() or "MI_02" in hwid or "MI_06" in hwid:
                    # 轻量下发 ATI 测试是否为出厂 AT 态
                    model = "移芯模组 (出厂 AT 态)"
                    chip = "ec718pv"
                    try:
                        with serial.Serial(dev, baudrate=115200, timeout=0.3) as ser:
                            ser.write(b"ATI\r\n")
                            ser.flush()
                            time.sleep(0.1)
                            raw = ser.read(ser.in_waiting or 256).decode("utf-8", errors="ignore")
                            if "Air780EP" in raw or "EC718" in raw:
                                model = "合宙 Air780EPV/EP (出厂 AT 固件)"
                                chip = "ec718pv"
                            elif "Air780E" in raw or "EC618" in raw:
                                model = "合宙 Air780E (出厂 AT 固件)"
                                chip = "ec618"
                            elif "Air700E" in raw:
                                model = "合宙 Air700E (出厂 AT 固件)"
                                chip = "ec618"
                    except Exception:
                        pass

                    unassigned.append({
                        "port": dev,
                        "desc": p.description,
                        "mode": "factory_at",
                        "chip": chip,
                        "model_guess": model,
                        "recommend_flash": "full"
                    })

        with self.pool_lock:
            self.unassigned_dongles = unassigned

        # 若发现新设备且距离上次广播超过 3 秒，广播事件通知前端
        now = time.time()
        if unassigned and (now - self._last_unassigned_broadcast > 3.0):
            self._last_unassigned_broadcast = now
            self.hub.broadcast_json({
                "type": "event",
                "event": "unassigned_dongles_detected",
                "data": unassigned
            })

    def get_unassigned_dongles(self) -> List[Dict[str, Any]]:
        with self.pool_lock:
            return list(self.unassigned_dongles)

    def _scan_loop(self):
        """后台轮询扫描，感知热插拔"""
        while self.running:
            try:
                self._sync_ports()
            except Exception as e:
                log(f"会话池扫描循环异常: {e}")
            time.sleep(2.5)

    def get_session(self, target: Optional[str] = None, active_only: bool = True) -> Optional[DongleSession]:
        """
        根据 slot_id (如 'slot_1') 或 port (如 'COM8') 查找活跃会话。
        【防串台铁律】：若显式指定了 target，未找到或不在线时必须严格返回 None，绝对不可降级回退到 slot_1 或其他卡槽！
        仅当 target 为空/None 时，才允许缺省匹配：优先 slot_1，其次首个可用在线会话。
        """
        with self.pool_lock:
            if target and str(target).strip():
                target_str = str(target).strip()
                # 尝试匹配 slot
                for s in self.sessions.values():
                    if s.slot_id.lower() == target_str.lower():
                        return s if (not active_only or s.is_connected) else None
                # 尝试匹配 port
                for s in self.sessions.values():
                    if s.port.upper() == target_str.upper():
                        return s if (not active_only or s.is_connected) else None
                # 显式指定的目标不存在，直接返回 None，杜绝跨卡槽串台！
                return None

            # 缺省降级匹配（仅在未显式指定 target 时生效）：优先 slot_1
            for s in self.sessions.values():
                if s.slot_id == "slot_1" and (not active_only or s.is_connected):
                    return s
            # 否则取任意在线设备
            for s in self.sessions.values():
                if not active_only or s.is_connected:
                    return s
        return None

    def list_all_summaries(self) -> List[Dict[str, Any]]:
        with self.pool_lock:
            sorted_sessions = sorted(self.sessions.values(), key=lambda s: s.slot_id)
            return [s.get_summary() for s in sorted_sessions]


# =========================================================================
# 核心类：GatewayHub (网关多路共享中枢与广播总线)
# =========================================================================

class GatewayHub:
    def __init__(self, host: str = HUB_HOST, port: int = HUB_PORT, com: Optional[str] = None, baud: int = SERIAL_BAUD):
        self.host = host
        self.port = port
        self.preferred_com = com
        self.preferred_baud = baud
        self.running = False
        self.server_sock: Optional[socket.socket] = None
        self.clients: List[socket.socket] = []
        self.clients_lock = threading.Lock()
        self.serial_paused = False

        # 动态会话池
        self.session_pool = DongleSessionPool(self)

        # 集群健康监控与智能出站路由器 (AIR-22)
        self.health_monitor = ClusterHealthMonitor()
        self.router = ClusterRouter(self.session_pool, self.health_monitor)

        # 缓存与配置
        self.latest_otp: Optional[Dict[str, Any]] = None
        self.recent_sms: List[Dict[str, Any]] = []
        self.auto_copy_otp: bool = True
        self.mcp_config: Dict[str, Any] = {"enabled": False, "port": 17800}
        self.notify_config: Dict[str, Any] = self._load_notify_config()
        self.state_lock = threading.Lock()

        # 本地会话免检令牌 (方案 D: 内部免检信任环，仅保存在内存与本地文件中)
        self.internal_session_token = str(uuid.uuid4().hex)
        self._save_session_token()
        # 记录各客户端 socket 属性: { sock: {"source": "web"|"mcp", "authenticated": bool} }
        self.client_meta: Dict[socket.socket, Dict[str, Any]] = {}

    def _save_session_token(self):
        """将内部免检令牌安全写入宿主本地数据目录"""
        try:
            token_path = os.path.join(DATA_DIR, ".hub_session_token")
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(self.internal_session_token)
        except Exception as e:
            log(f"写入 .hub_session_token 异常: {e}")

    def _load_notify_config(self) -> Dict[str, Any]:
        """加载通知配置"""
        json_path = GATEWAY_CONFIG_PATH
        cfg = {
            "system": {"auto_copy_otp": True},
            "feishu": {"enable": 0, "url": "", "secret": ""},
            "wecom": {"enable": 0, "url": ""},
            "dingtalk": {"enable": 0, "url": "", "secret": ""},
            "bark": {"enable": 0, "url": "", "group": "Air780Gateway", "sound": "minuet"},
            "webhook": {"enable": 0, "url": "", "method": "POST"},
            "mcp": {"enabled": False, "port": 17800}
        }
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                for k, v in loaded.items():
                    if k in cfg and isinstance(v, dict):
                        cfg[k].update(v)
                    elif k == "system" and isinstance(v, dict):
                        cfg["system"].update(v)
                    elif k == "mcp" and isinstance(v, dict):
                        cfg["mcp"].update(v)
                self.auto_copy_otp = bool(cfg.get("system", {}).get("auto_copy_otp", True))
                self.mcp_config = dict(cfg.get("mcp", {"enabled": False, "port": 17800}))
                enabled_list = [ch for ch, item in cfg.items() if ch not in ("system", "mcp") and item.get("enable") in (1, True, "1") and item.get("url")]
                mcp_on = self.mcp_config.get("enabled", False)
                log(f"成功加载网关配置: 自动复制验证码={self.auto_copy_otp}, MCP物理开关={mcp_on}, 已启用渠道={enabled_list}")
                return cfg
            except Exception as e:
                log(f"解析 gateway_config.json 异常: {e}")
        return cfg

    def reload_notify_config(self) -> Dict[str, Any]:
        with self.state_lock:
            self.notify_config = self._load_notify_config()
            self.auto_copy_otp = bool(self.notify_config.get("system", {}).get("auto_copy_otp", True))
            self.mcp_config = dict(self.notify_config.get("mcp", {"enabled": False, "port": 17800}))
            log(f"通知与MCP配置热重载完成 (自动复制: {self.auto_copy_otp}, MCP开启: {self.mcp_config.get('enabled', False)})")
            return dict(self.notify_config)

    def start(self):
        # 1. 尝试绑定本地 TCP 端口（单例独占保护）
        try:
            self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            elif os.name != "nt":
                self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_sock.bind((self.host, self.port))
            self.server_sock.listen(15)
        except OSError as e:
            log(f"端口 {self.host}:{self.port} 已被占用，已有 Hub 实例运行，本进程安全退出。({e})")
            sys.exit(0)

        self.running = True
        log(f"多设备网关中枢启动就绪，监听 IPC 端口: {self.host}:{self.port}")

        # 2. 启动动态串口会话池
        self.session_pool.start()

        # 3. 启动 TCP 监听主循环
        self._tcp_server_loop()

    def stop(self):
        """停止 Hub 服务及所有卡槽会话"""
        self.running = False
        try:
            if self.server_sock:
                self.server_sock.close()
        except Exception:
            pass
        with self.clients_lock:
            for c in list(self.clients):
                try:
                    c.close()
                except Exception:
                    pass
            self.clients.clear()
        if self.session_pool:
            self.session_pool.stop()

    def broadcast_text(self, text: str, is_sms_event: bool = False):
        """向所有在线客户端广播纯文本行 (需自带 \\n)；若为短信事件且 MCP 未开启，则阻断向 MCP 广播"""
        data = text.encode("utf-8")
        mcp_enabled = bool(self.mcp_config.get("enabled", False))
        with self.clients_lock:
            dead = []
            for c in self.clients:
                # 若为实时短信事件且 MCP 物理开关关闭，严禁向外部 MCP 客户端广播明文短信 (方案 D 隐私防线)
                if is_sms_event and not mcp_enabled:
                    meta = self.client_meta.get(c, {})
                    if not (meta.get("authenticated") and meta.get("source") == "web"):
                        continue
                try:
                    c.sendall(data)
                except Exception:
                    dead.append(c)
            for dc in dead:
                self.clients.remove(dc)
                self.client_meta.pop(dc, None)
                try:
                    dc.close()
                except Exception:
                    pass

    def broadcast_json(self, obj: Dict[str, Any]):
        """向所有在线客户端广播格式化 JSON"""
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        is_sms = obj.get("type") == "event" and obj.get("event") in ("sms_rx", "sms_received")
        self.broadcast_text(line, is_sms_event=is_sms)

    def on_session_frame(self, session: DongleSession, obj: Dict[str, Any], raw_line: str):
        """会话上报 NDJSON 帧的总线接收入口"""
        frame_type = obj.get("type")
        evt = obj.get("event")
        data = obj.get("data") or {}

        # 1. 拦截与提取验证码 (OTP)
        if frame_type == "event" and evt == "sms_rx":
            code = data.get("code")

            if code:
                with self.state_lock:
                    self.latest_otp = {
                        "code": code,
                        "slot": session.slot_id,
                        "from": data.get("from"),
                        "content": data.get("content"),
                        "time": obj.get("ts", int(time.time()))
                    }
                # 构造自然大白话设备标签 (AIR-31)
                dev_model = getattr(session, "model", "") or ("Air780E" if session.slot_id == "slot_2" else "Air780EPV")
                dev_model = str(dev_model).replace("合宙", "").strip()
                s_phone = str(getattr(session, "phone", "") or getattr(session, "number", "") or "")
                clean_p = re.sub(r"\D", "", s_phone)
                s_tail = clean_p[-4:] if len(clean_p) >= 4 else ""
                slot_label = f"[{dev_model}·{s_tail}]" if s_tail else f"[{dev_model}]"
                if getattr(self, "auto_copy_otp", True):
                    if set_windows_clipboard(code):
                        log(f"⚡ [CLIPBOARD] 验证码 {slot_label} [{code}] 已自动存入 Windows 剪贴板")
                        show_windows_toast("Air780 网关验证码", f"⚡ {slot_label} 捕获验证码：{code} (已存入剪贴板，直接按 Ctrl+V 粘贴)")
                else:
                    log(f"⚡ [OTP] 捕获验证码 {slot_label} [{code}]")
                    show_windows_toast("Air780 网关验证码", f"⚡ {slot_label} 捕获验证码：{code}")

        # 2. 触发宿主宽带代推
        if frame_type == "event" and evt in ("sms_rx", "call_rx", "gateway_ready", "state_change"):
            self._dispatch_host_proxy_push(session, evt, data)

        # 3. 注入会话所属 slot 元数据并广播给全量客户端
        if "slot" not in obj:
            obj["slot"] = session.slot_id
        if isinstance(data, dict) and "slot" not in data:
            data["slot"] = session.slot_id

        # 4. 更新集群健康监控状态机 (AIR-22)
        try:
            if frame_type == "event":
                if evt in ("state_change", "gateway_ready"):
                    csq = data.get("csq")
                    if csq is not None:
                        self.health_monitor.update_csq(session.slot_id, int(csq))
                elif evt == "sms_sent":
                    ok = bool(data.get("ok", True))
                    if ok:
                        self.health_monitor.record_send_success(session.slot_id)
                    else:
                        err = str(data.get("error") or data.get("msg") or "SMS_SEND_FAILED")
                        self.health_monitor.record_send_failure(session.slot_id, err)
            elif frame_type == "res" and isinstance(data, dict):
                csq = data.get("csq")
                if csq is not None:
                    self.health_monitor.update_csq(session.slot_id, int(csq))
        except Exception as e:
            log(f"[{session.slot_id}] 健康状态机更新异常: {e}")

        broadcast_line = json.dumps(obj, ensure_ascii=False)
        is_sms = frame_type == "event" and evt in ("sms_rx", "sms_received")
        self.broadcast_text(broadcast_line + "\n", is_sms_event=is_sms)

    def _dispatch_host_proxy_push(self, session: DongleSession, event_type: str, data: Dict[str, Any]):
        """借用宿主电脑本地宽带优先代推全渠道通知，并向模组回送 Push ACK 握手回执"""
        # 只要宿主在线，无论板端是否开启蜂窝数据，一律由宿主电脑本地宽带优先代推（杜绝消耗 SIM 流量）

        def _format_phone_number(raw_num: Any) -> str:
            if not raw_num: return "未知"
            m = re.search(r"(?:\+86)?(\d{11})", str(raw_num))
            return f"{m.group(1)} +86" if m else str(raw_num)

        slot_tag = f"【{session.meta.get('model', 'Air780')} ({session.slot_id.upper()})】"
        dev_model = data.get("model") or data.get("bsp") or session.meta.get("model") or session.meta.get("bsp") or "Air780"
        dev_imei = data.get("imei") or session.meta.get("imei") or ""
        imei_part = f" · IMEI: {dev_imei}" if dev_imei else ""
        dev_desc = f"[{session.slot_id.upper()}] {dev_model}{imei_part}"

        title = ""
        plain_text = ""
        md_text = ""
        extra_otp = ""

        if event_type == "sms_rx":
            sender = data.get("from", "未知")
            content = data.get("content", "")
            code = data.get("code", "")
            extra_otp = code or ""
            title = f"📩 [{session.slot_id.upper()}] 收到新短信"
            otp_str = f"\r\n🔑 提取验证码: 【{code}】" if code else ""
            otp_md = f"\n> **提取验证码**: <font color=\"warning\">{code}</font>" if code else ""
            plain_text = f"发件人: {sender}\r\n内容: {content}{otp_str}\r\n\r\n设备: {dev_desc} （电脑本地宽带代推）"
            md_text = f"### {title}\n> **发件人**: {sender}\n> **短信正文**: {content}{otp_md}\n\n> **来源设备**: {dev_desc} （电脑本地宽带代推）"

        elif event_type == "call_rx":
            sender = data.get("from", "未知号码")
            is_fota = bool(data.get("fota_trigger"))
            if is_fota:
                title = f"⚡ [{session.slot_id.upper()}] 识别暗号呼叫：激活 FOTA 空中更新"
                plain_text = f"呼入号码: {sender}\r\n动作: 识别管理员暗号呼叫，已 0 话费拒接，正在激活 4G 蜂窝空中热更新探测...\r\n\r\n设备: {dev_desc} （电脑本地宽带代推）"
                md_text = f"### {title}\n> **呼入号码**: {sender}\n> **处理动作**: 识别管理员暗号呼叫，已 0 话费拒接，正在激活 4G 空中热更新...\n\n> **来源设备**: {dev_desc} （电脑本地宽带代推）"
            else:
                title = f"📞 [{session.slot_id.upper()}] 拦截到呼入电话"
                plain_text = f"呼入号码: {sender}\r\n动作: 已自动秒级拒接 (双方0元话费)\r\n\r\n设备: {dev_desc} （电脑本地宽带代推）"
                md_text = f"### {title}\n> **呼入号码**: {sender}\n> **拦截动作**: 已自动秒级拒接 (双方0元话费)\n\n> **来源设备**: {dev_desc} （电脑本地宽带代推）"

        elif event_type in ("gateway_ready", "state_change"):
            title = f"🚀 [{session.slot_id.upper()}] 智能网关已上线" if event_type == "gateway_ready" else f"⚙️ [{session.slot_id.upper()}] 配置状态变更"
            bsp = data.get("bsp", session.meta.get("bsp", "Air780"))
            num = _format_phone_number(data.get("formatted_number") or data.get("number"))
            csq = data.get("csq") if data.get("csq") is not None else "未知"
            rsrp = data.get("rsrp") if data.get("rsrp") is not None else "未知"
            temp = data.get("temp") if data.get("temp") is not None else "未知"
            vbat = data.get("vbat") if data.get("vbat") is not None else "未知"
            ver = data.get("version") if data.get("version") is not None else "未知"
            plain_text = (
                f"设备来源：{dev_desc} （电脑本地宽带代推）\r\n"
                f"端口路径：{session.port}\r\n"
                f"本机号码：{num}\r\n"
                f"信号强度：CSQ {csq} (RSRP {rsrp} dBm)\r\n"
                f"固件版本：{ver}\r\n"
                f"核心温度：{temp} ℃ | 电压：{vbat} V"
            )
            md_text = f"### {title}\n```\n{plain_text}\n```"
        else:
            return

        msg_id = data.get("id")
        channels_to_send = []

        # 1. 飞书推送
        feishu_cfg = self.notify_config.get("feishu", {})
        if feishu_cfg.get("enable") and feishu_cfg.get("url"):
            card_template = "blue"
            card_title = title
            if event_type == "sms_rx":
                card_template = "orange" if extra_otp else "blue"
                card_title = f"🔑 {slot_tag} 收到短信验证码" if extra_otp else f"📩 {slot_tag} 收到新短信"
                sender = data.get("from", "未知")
                content = data.get("content", "")
                now_str = time.strftime("%Y-%m-%d %H:%M:%S")
                body_elements = [
                    {"tag": "markdown", "content": f"**卡槽来源：** {slot_tag}\n**发件人：** `{sender}`\n**接收时间：** {now_str}"},
                    {"tag": "hr"}
                ]
                if extra_otp:
                    body_elements.extend([
                        {"tag": "markdown", "content": f"**提取验证码：**\n```text\n{extra_otp}\n```"},
                        {"tag": "hr"}
                    ])
                body_elements.append({"tag": "markdown", "content": f"**短信正文：**\n{content}"})
                body_elements.append({"tag": "hr"})
                body_elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"<font color='grey'>来源设备: {dev_desc} （电脑本地宽带代推）</font>"
                    }
                })
            else:
                body_elements = [{"tag": "markdown", "content": md_text}]
                body_elements.append({"tag": "hr"})
                body_elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"<font color='grey'>来源设备: {dev_desc} （电脑本地宽带代推）</font>"
                    }
                })

            card_payload = {
                "schema": "2.0",
                "config": {"update_multi": True, "style": {"text_size": {"normal_v2": {"default": "normal", "pc": "normal", "mobile": "heading"}}}},
                "header": {"title": {"tag": "plain_text", "content": card_title}, "template": card_template},
                "body": {"direction": "vertical", "padding": "12px 12px 12px 12px", "elements": body_elements}
            }
            pdict = {"msg_type": "interactive", "card": card_payload}
            secret = (feishu_cfg.get("secret") or "").strip()
            if secret:
                ts = str(int(time.time()))
                sign_str = f"{ts}\n{secret}"
                hmac_code = hmac.new(sign_str.encode("utf-8"), digestmod=hashlib.sha256).digest()
                pdict["timestamp"] = ts
                pdict["sign"] = base64.b64encode(hmac_code).decode("utf-8")
            channels_to_send.append(("feishu", feishu_cfg["url"], pdict))

        # 2. 企业微信
        wecom_cfg = self.notify_config.get("wecom", {})
        if wecom_cfg.get("enable") and wecom_cfg.get("url"):
            channels_to_send.append(("wecom", wecom_cfg["url"], {"msgtype": "markdown", "markdown": {"content": md_text}}))

        # 3. 钉钉
        ding_cfg = self.notify_config.get("dingtalk", {})
        if ding_cfg.get("enable") and ding_cfg.get("url"):
            durl = ding_cfg["url"]
            sec = (ding_cfg.get("secret") or "").strip()
            if sec:
                ts = str(round(time.time() * 1000))
                st = f"{ts}\n{sec}"
                hc = hmac.new(sec.encode("utf-8"), st.encode("utf-8"), digestmod=hashlib.sha256).digest()
                sgn = urllib.parse.quote_plus(base64.b64encode(hc).decode("utf-8"))
                durl = f"{durl}&timestamp={ts}&sign={sgn}" if "?" in durl else f"{durl}?timestamp={ts}&sign={sgn}"
            channels_to_send.append(("dingtalk", durl, {"msgtype": "markdown", "markdown": {"title": title, "text": md_text}}))

        # 4. Bark
        bark_cfg = self.notify_config.get("bark", {})
        if bark_cfg.get("enable") and bark_cfg.get("url"):
            bark_data = {
                "title": title,
                "body": plain_text,
                "group": bark_cfg.get("group", "Air780Gateway"),
                "sound": bark_cfg.get("sound", "minuet")
            }
            if extra_otp:
                bark_data["copy"] = str(extra_otp)
                bark_data["automaticallyCopy"] = "1"
            channels_to_send.append(("bark", bark_cfg["url"], bark_data))

        # 5. 通用 Webhook
        webhook_cfg = self.notify_config.get("webhook", {})
        if webhook_cfg.get("enable") and webhook_cfg.get("url"):
            wh_body = {
                "event": event_type,
                "slot": session.slot_id,
                "port": session.port,
                "model": dev_model,
                "imei": dev_imei,
                "device_desc": dev_desc,
                "timestamp": int(time.time()),
                "data": data,
                "source_mode": "cluster_broadband_proxy"
            }
            channels_to_send.append(("webhook", webhook_cfg["url"], wh_body))

        if not channels_to_send:
            # 未开启任何渠道，定向给该设备回执 ok 以免其误进入降级自推
            if msg_id:
                session.ack_push(msg_id, "ok")
            return

        total_channels = len(channels_to_send)
        completed_count = [0]
        success_count = [0]
        ack_sent = [False]
        ack_lock = threading.Lock()

        def _send_ack_safe(status: str):
            if not ack_sent[0] and msg_id:
                ack_sent[0] = True
                session.ack_push(msg_id, status)
                log(f"[{session.slot_id}] 宽带代推已定向回执 ACK -> 【{status}】(msg_id: {msg_id})")

        def _send_channel(ch_name: str, url: str, payload_dict: Dict[str, Any]):
            payload_bytes = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
            succ = False
            try:
                req = urllib.request.Request(url, data=payload_bytes, headers={"Content-Type": "application/json; charset=utf-8"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        succ = True
                        log(f"[{session.slot_id}] 宿主宽带代推 [{event_type}] 到 {ch_name} 成功 (HTTP 200)")
            except Exception as e:
                log(f"[{session.slot_id}] 宿主宽带代推 [{event_type}] 到 {ch_name} 失败: {e}")

            with ack_lock:
                completed_count[0] += 1
                if succ:
                    success_count[0] += 1
                    _send_ack_safe("ok")
                elif completed_count[0] >= total_channels and success_count[0] == 0:
                    _send_ack_safe("failed")

        for ch_name, ch_url, ch_body in channels_to_send:
            threading.Thread(target=_send_channel, args=(ch_name, ch_url, ch_body), daemon=True).start()

        # 飞书纯数字气泡
        if feishu_cfg.get("enable") and feishu_cfg.get("url") and extra_otp:
            def _send_pure_otp_bubble():
                time.sleep(0.35)
                pure_dict = {"msg_type": "text", "content": {"text": str(extra_otp)}}
                sec2 = (feishu_cfg.get("secret") or "").strip()
                if sec2:
                    ts2 = str(int(time.time()))
                    s2 = f"{ts2}\n{sec2}"
                    hm2 = hmac.new(s2.encode("utf-8"), digestmod=hashlib.sha256).digest()
                    pure_dict["timestamp"] = ts2
                    pure_dict["sign"] = base64.b64encode(hm2).decode("utf-8")
                try:
                    r = urllib.request.Request(feishu_cfg["url"], data=json.dumps(pure_dict).encode("utf-8"), headers={"Content-Type": "application/json; charset=utf-8"})
                    with urllib.request.urlopen(r, timeout=5) as resp:
                        pass
                except Exception:
                    pass
            threading.Thread(target=_send_pure_otp_bubble, daemon=True).start()

    def get_cluster_overview(self) -> Dict[str, Any]:
        """聚合集群全景驾驶舱数据 (AIR-22)"""
        summaries = self.session_pool.list_all_summaries()
        health_summary = self.health_monitor.get_summary().get("slots", {})
        cards = []
        for s in summaries:
            s_id = s.get("slot")
            h = health_summary.get(s_id, {})
            sim_carrier = detect_sim_carrier(s.get("iccid", ""))
            cards.append({
                "slot": s_id,
                "port": s.get("port"),
                "loc": s.get("loc"),
                "model": s.get("model", "Air780"),
                "bsp": s.get("bsp", ""),
                "imei": s.get("imei", ""),
                "iccid": s.get("iccid", ""),
                "carrier": sim_carrier,
                "online": s.get("online", False),
                "csq": s.get("csq", 0),
                "temp": s.get("temp", ""),
                "vbat": s.get("vbat", ""),
                "state": h.get("state", "healthy"),
                "routable": h.get("routable", True),
                "consecutive_failures": h.get("consecutive_failures", 0),
                "total_sent": h.get("total_sent", 0),
                "total_success": h.get("total_success", 0),
                "total_failed": h.get("total_failed", 0),
                "cellular_data": s.get("cellular_data", False),
                "capabilities": s.get("capabilities", {})
            })
        return {
            "total_slots": len(cards),
            "online_slots": sum(1 for c in cards if c.get("online")),
            "healthy_slots": sum(1 for c in cards if c.get("state") == "healthy" and c.get("online")),
            "cards": cards,
            "timestamp": time.time()
        }

    def _handle_client_send(self, msg_str: str):
        """兼容单机/测试下发指令"""
        try:
            cmd_data = json.loads(msg_str)
            cmd = cmd_data.get("cmd")
            if cmd in ("pause_serial", "pause_for_flash"):
                self.serial_paused = True
                with self.session_pool.pool_lock:
                    for sess in self.session_pool.sessions.values():
                        sess.pause_for_flash()
            elif cmd in ("resume_serial", "resume_after_flash"):
                self.serial_paused = False
                with self.session_pool.pool_lock:
                    for sess in self.session_pool.sessions.values():
                        sess.resume_after_flash()
            else:
                self.handle_client_command(None, msg_str)
        except Exception:
            pass

    def _ensure_serial_connected(self) -> bool:
        """检查串口连接健康度，处于暂停避让态时返回 False"""
        if getattr(self, "serial_paused", False):
            return False
        with self.session_pool.pool_lock:
            return any(sess.is_connected for sess in self.session_pool.sessions.values())

    def handle_client_command(self, line: str, sock: Optional[socket.socket] = None):
        """处理来自 Web 控制台、MCP 等客户端的 JSON 指令"""
        clean_line = line.strip()
        if not clean_line or not clean_line.startswith("{") or not clean_line.endswith("}"):
            return

        try:
            cmd_obj = json.loads(clean_line)
        except Exception:
            return

        cmd_name = cmd_obj.get("cmd")
        req_id = cmd_obj.get("id")
        params = cmd_obj.get("params") or {}
        target_slot = cmd_obj.get("slot") or params.get("slot") or cmd_obj.get("target")

        # 方案 D：调用者身份鉴权与内部免检信任环
        source = str(cmd_obj.get("source") or "").lower()
        token = str(cmd_obj.get("token") or "")

        # 若携带正确内部 Session Token，赋予 Web/Internal 绝对免检信任
        is_internal_master = False
        if token and token == self.internal_session_token:
            is_internal_master = True
            if sock:
                with self.clients_lock:
                    self.client_meta[sock] = {"source": "web", "authenticated": True}
        elif sock:
            with self.clients_lock:
                meta = self.client_meta.get(sock, {})
                if meta.get("authenticated") and meta.get("source") == "web":
                    is_internal_master = True

        # Fail-closed 默认拒绝准则：非内部免检信任源，统一定性为外部 source: "mcp"
        if not is_internal_master:
            source = "mcp"
            if sock:
                with self.clients_lock:
                    self.client_meta[sock] = {"source": "mcp", "authenticated": False}

        log(f"Received client cmd: {cmd_name}, id={req_id}, target_slot={target_slot}, source={source}, is_master={is_internal_master}")

        # 方案 D 物理管控黑名单拦截：当 MCP 开关关闭且来源为外部 MCP 时，100% 物理拦截并返回自解释人话提示
        mcp_enabled = bool(self.mcp_config.get("enabled", False))
        mcp_blocked_cmds = (
            "send_sms", "call_dial", "call_hangup", "get_history",
            "set_rndis", "set_cellular_data", "reboot", "clear_history"
        )
        if not is_internal_master and not mcp_enabled and cmd_name in mcp_blocked_cmds:
            deny_resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": False,
                "code": 403,
                "msg": "MCP_ACCESS_DENIED",
                "error": "❌ 物理调用被拒绝：上位机管理员已在控制台中关闭了 AI MCP 调用权限。如需使用，请前往 Web 控制台 (http://127.0.0.1:17801) 的【系统设置】抽屉中开启「AI 智能体通信服务 (MCP)」开关。"
            }, ensure_ascii=False) + "\n"
            if sock: sock.sendall(deny_resp.encode("utf-8"))
            else: self.broadcast_text(deny_resp)
            return

        # 1. Hub 内部控制指令处理
        if cmd_name in ("get_slots", "list_dongles", "get_sessions"):
            summaries = self.session_pool.list_all_summaries()
            # 注入 mcp_action_allowed 字段方便 AI 客户端获悉当前权限状态进行友好引导
            resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": True,
                "code": 0,
                "data": {
                    "slots": summaries,
                    "count": len(summaries),
                    "mcp_action_allowed": mcp_enabled
                }
            }, ensure_ascii=False) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return

        if cmd_name == "reload_notify_config":
            new_cfg = self.reload_notify_config()
            resp = json.dumps({"type": "response", "cmd": "reload_notify_config", "status": "ok", "config": new_cfg}) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return

        if cmd_name == "set_notify_config":
            data = cmd_obj.get("data")
            if isinstance(data, dict):
                cur = self._load_notify_config()
                for k, v in data.items():
                    if k in cur and isinstance(v, dict): cur[k].update(v)
                    elif isinstance(v, dict): cur[k] = v
                with open(GATEWAY_CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cur, f, ensure_ascii=False, indent=2)
                self.notify_config = cur
                log("Hub 已接收并更新本地通知配置，正透传至各在线板卡...")
                # 广播下发给所有在线板卡
                with self.session_pool.pool_lock:
                    for s in self.session_pool.sessions.values():
                        s.send_line(clean_line)
                return

        if cmd_name in ("ping_device", "check_hardware"):
            has_any_online = any(s.is_connected for s in self.session_pool.sessions.values())
            resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": has_any_online,
                "online": has_any_online,
                "code": 0 if has_any_online else -1,
                "msg": "PONG" if has_any_online else "HARDWARE_DISCONNECTED",
                "slots": self.session_pool.list_all_summaries()
            }) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return

        if cmd_name == "get_cluster_health":
            resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": True,
                "code": 0,
                "data": self.health_monitor.get_summary()
            }) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return

        if cmd_name == "get_cluster_overview":
            resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": True,
                "code": 0,
                "data": self.get_cluster_overview()
            }) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return

        if cmd_name == "get_unassigned_dongles":
            resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": True,
                "code": 0,
                "data": {
                    "unassigned": self.session_pool.get_unassigned_dongles()
                }
            }) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return

        # 2. 短信发送智能路由分流 (AIR-22)
        if cmd_name == "send_sms":
            target_phone = str(cmd_obj.get("phone") or params.get("phone") or cmd_obj.get("number") or params.get("number") or "")
            strategy = str(cmd_obj.get("strategy") or params.get("strategy") or "operator_affinity")
            direct_slot = cmd_obj.get("slot") or params.get("slot")
            dry_run = bool(cmd_obj.get("dry_run") or params.get("dry_run"))

            route_res = self.router.route_outbound(target_phone, strategy=strategy, direct_slot=direct_slot)
            if not route_res.ok:
                err_resp = json.dumps({
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "code": -1,
                    "msg": "ROUTE_FAILED",
                    "error": route_res.error,
                    "slot": direct_slot
                }) + "\n"
                if sock: sock.sendall(err_resp.encode("utf-8"))
                else: self.broadcast_text(err_resp)
                return

            if dry_run:
                ok_resp = json.dumps({
                    "type": "res",
                    "id": req_id,
                    "ok": True,
                    "code": 0,
                    "slot": route_res.slot_id,
                    "status": "routed",
                    "routed_strategy": route_res.strategy_used,
                    "fallback_used": route_res.fallback_used,
                    "panic_mode": route_res.panic_mode,
                    "msg": "DRY_RUN_ROUTED_OK"
                }) + "\n"
                if sock: sock.sendall(ok_resp.encode("utf-8"))
                else: self.broadcast_text(ok_resp)
                return

            session = route_res.session
            target_content = str(cmd_obj.get("content") or params.get("content") or cmd_obj.get("text") or params.get("text") or "")
            # 标记射频与串口发送静默期（6秒内暂停后台定时探测，避免基带与总线竞争）
            session._sms_tx_busy_until = time.time() + 6.0

            # 精简下发给板端串口的数据包，同时携带顶层与 data 字典参数以兼容历史固件
            board_cmd = {
                "type": "cmd",
                "id": req_id,
                "cmd": "send_sms",
                "phone": target_phone,
                "content": target_content,
                "data": {
                    "phone": target_phone,
                    "to": target_phone,
                    "content": target_content,
                    "text": target_content
                }
            }
            clean_line = json.dumps(board_cmd, ensure_ascii=False)
            session.send_line(clean_line)
            return

        # 3. 电话呼叫与硬件能力门禁 (AIR-30)
        if cmd_name == "call_dial":
            session = None
            # 智能优选或定向选择支持 VoLTE 的卡槽
            if not target_slot or target_slot in ("auto", ""):
                for s_id, sess in self.session_pool.sessions.items():
                    if sess.is_connected and sess.capabilities.get("volte"):
                        target_slot = s_id
                        session = sess
                        break
            else:
                session = self.session_pool.get_session(target_slot)

            if not session or not session.is_connected:
                err_resp = json.dumps({
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "code": -1,
                    "msg": "NO_VOLTE_SLOT",
                    "error": "集群中无可用的 VoLTE 语音通话模组卡槽" if not target_slot else f"目标卡槽 [{target_slot}] 不在线或未连接",
                    "slot": target_slot
                }) + "\n"
                if sock: sock.sendall(err_resp.encode("utf-8"))
                else: self.broadcast_text(err_resp)
                return

            if not session.capabilities.get("volte"):
                err_resp = json.dumps({
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "code": -1,
                    "msg": "HARDWARE_UNSUPPORTED",
                    "error": f"卡槽 [{session.slot_id}] 模组 ({session.model}) 硬件缺乏 VoLTE 语音协议栈，不支持拨打电话",
                    "slot": session.slot_id
                }) + "\n"
                if sock: sock.sendall(err_resp.encode("utf-8"))
                else: self.broadcast_text(err_resp)
                return

            # 透传 call_dial 到选中的 VoLTE 硬件会话
            cmd_obj["slot"] = session.slot_id
            if "type" not in cmd_obj:
                cmd_obj["type"] = "cmd"
            clean_line = json.dumps(cmd_obj, ensure_ascii=False)
            session.send_line(clean_line)
            return

        if cmd_name == "call_hangup":
            session = None
            if not target_slot or target_slot in ("auto", ""):
                for s_id, sess in self.session_pool.sessions.items():
                    if sess.is_connected and sess.capabilities.get("volte"):
                        target_slot = s_id
                        session = sess
                        break
            else:
                session = self.session_pool.get_session(target_slot)

            if not session or not session.is_connected:
                err_resp = json.dumps({
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "code": -1,
                    "msg": "HARDWARE_DISCONNECTED",
                    "error": f"卡槽 [{target_slot}] 不在线或未连接",
                    "slot": target_slot
                }) + "\n"
                if sock: sock.sendall(err_resp.encode("utf-8"))
                else: self.broadcast_text(err_resp)
                return

            cmd_obj["slot"] = session.slot_id
            if "type" not in cmd_obj:
                cmd_obj["type"] = "cmd"
            clean_line = json.dumps(cmd_obj, ensure_ascii=False)
            session.send_line(clean_line)
            # 同时回复 ACK 确保客户端即刻获得响应
            ack_resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": True,
                "code": 0,
                "msg": "HANGUP_SENT",
                "slot": session.slot_id
            }) + "\n"
            if sock: sock.sendall(ack_resp.encode("utf-8"))
            else: self.broadcast_text(ack_resp)
            return

        # 3. 定向或缺省路由到底层硬件会话（串口让渡与恢复指令豁免活跃连接态检查）
        is_flash_manage_cmd = cmd_name in ("pause_for_flash", "resume_after_flash")
        session = self.session_pool.get_session(target_slot, active_only=not is_flash_manage_cmd)
        if not session:
            err_msg = f"目标卡槽 [{target_slot}] 不存在" if target_slot else "无可用 4G 模组会话"
            err_resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": False,
                "code": -1,
                "msg": "SLOT_NOT_FOUND",
                "error": err_msg
            }) + "\n"
            if sock: sock.sendall(err_resp.encode("utf-8"))
            else: self.broadcast_text(err_resp)
            return

        # 底层物理线刷 (FlashToolCLI) 串口独占挂起与恢复：允许在未连接/已释放态安全恢复
        if cmd_name == "pause_for_flash":
            session.pause_for_flash()
            resp = json.dumps({"type": "res", "id": req_id, "ok": True, "slot": session.slot_id, "msg": "SERIAL_PAUSED_FOR_FLASH"}) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return
        elif cmd_name == "resume_after_flash":
            session.resume_after_flash()
            resp = json.dumps({"type": "res", "id": req_id, "ok": True, "slot": session.slot_id, "msg": "SERIAL_RESUMED_AFTER_FLASH"}) + "\n"
            if sock: sock.sendall(resp.encode("utf-8"))
            else: self.broadcast_text(resp)
            return

        if not session.is_connected:
            err_msg = f"目标卡槽 [{target_slot}] 不在线或未插入" if target_slot else "无可用的在线 4G 模组"
            err_resp = json.dumps({
                "type": "res",
                "id": req_id,
                "ok": False,
                "code": -1,
                "msg": "HARDWARE_DISCONNECTED",
                "error": err_msg
            }) + "\n"
            if sock: sock.sendall(err_resp.encode("utf-8"))
            else: self.broadcast_text(err_resp)
            return

        # SOTA 固件升级状态同步与后台探针避让
        if cmd_name in ("ota_start", "ota_finish"):
            session.is_flashing = True
            log(f"[{session.slot_id}] ⚡ SOTA 固件热更进行中，保持后台心跳探针与出站路由避让")
        elif cmd_name == "ota_abort":
            session.is_flashing = False
            log(f"[{session.slot_id}] ⚡ SOTA 固件热更中止，已恢复后台心跳探针与出站路由")

        # 透传发送给选中的硬件会话
        if "type" not in cmd_obj:
            cmd_obj["type"] = "cmd"
        clean_line = json.dumps(cmd_obj, ensure_ascii=False)
        session.send_line(clean_line)

    def _tcp_server_loop(self):
        log("TCP IPC 服务线程就绪，等待客户端连接...")
        while self.running:
            try:
                client_sock, addr = self.server_sock.accept()
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                log(f"新客户端已接入: {addr}")
                with self.clients_lock:
                    self.clients.append(client_sock)

                # 初始向新客户端推送当前会话池全景摘要
                summaries = self.session_pool.list_all_summaries()
                init_event = json.dumps({
                    "type": "event",
                    "event": "cluster_status",
                    "data": {"slots": summaries, "count": len(summaries)}
                }) + "\n"
                try:
                    client_sock.sendall(init_event.encode("utf-8"))
                except Exception:
                    pass

                # 为客户端分配专属工作线程
                threading.Thread(target=self._client_worker, args=(client_sock, addr), daemon=True).start()
            except Exception as e:
                if self.running:
                    log(f"TCP accept 异常: {e}")
                    time.sleep(0.5)

    def _client_worker(self, sock: socket.socket, addr):
        buffer = ""
        while self.running:
            try:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="ignore")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if line:
                        try:
                            self.handle_client_command(line, sock)
                        except Exception as e:
                            import traceback
                            log(f"handle_client_command 异常: {e}\n{traceback.format_exc()}")
            except Exception as e:
                log(f"_client_worker 异常: {e}")
                break

        log(f"客户端已断开: {addr}")
        with self.clients_lock:
            if sock in self.clients:
                self.clients.remove(sock)
            self.client_meta.pop(sock, None)
        try:
            sock.close()
        except Exception:
            pass


# =========================================================================
# 入口与 Windows Mutex 互斥保护
# =========================================================================

_single_instance_mutex = None

def _acquire_single_instance_mutex():
    global _single_instance_mutex
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _single_instance_mutex = kernel32.CreateMutexW(None, True, "Air780_Multi_Dongle_Hub_Mutex")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            log("已有 Hub 实例正在运行 (Windows Mutex 独占)，本进程安全退出。")
            sys.exit(0)

def main():
    _acquire_single_instance_mutex()
    hub = GatewayHub()
    try:
        hub.start()
    except KeyboardInterrupt:
        log("接收到退出信号，正在关闭 Hub...")
        hub.running = False
        hub.session_pool.stop()
        if hub.server_sock:
            try:
                hub.server_sock.close()
            except Exception:
                pass
        log("Hub 已安全退出")

if __name__ == "__main__":
    main()
