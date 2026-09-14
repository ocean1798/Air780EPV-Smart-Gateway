# -*- coding: utf-8 -*-
"""
Air780EPV 智能通信网关 - 本地共享中枢 (Gateway Core Hub)
功能：
1. 独占管理 COM8 物理串口链路，具备断线防抖与自动重连能力；
2. 绑定 127.0.0.1:17800，单例运行，端口被占时安全自杀；
3. 将板端上报的 NDJSON 事件（状态、短信、验证码、来电）多路实时广播给所有在线客户端；
4. 汇聚各客户端（桌面控制台、FastMCP、后台守护）下发的指令并加锁写入物理串口。
"""

import sys
import os
import re
import time
import json
import socket
import select
import serial
import threading
import hashlib
import hmac
import base64
import urllib.request
import urllib.parse
import urllib.error
from typing import List, Dict, Any, Optional, Tuple

# 确保在 Windows 下标准输出为 UTF-8，且在 windowed 模式下有安全回退流
class _SafeStream:
    def write(self, *args, **kwargs): pass
    def flush(self): pass
    def isatty(self): return False

if sys.stdout is None:
    sys.stdout = _SafeStream()
if sys.stderr is None:
    sys.stderr = _SafeStream()

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

HUB_HOST = "127.0.0.1"
HUB_PORT = 17800
SERIAL_PORT = "COM8"
SERIAL_BAUD = 115200

def find_cellular_vuart_port(preferred: Optional[str] = None) -> Optional[str]:
    """
    智能自动探测当前系统中的合宙/移芯 4G 模组 VUART 用户通信端口 (x.6 / MI_06 / VUART_0)。
    1. 若提供了 preferred 且该端口确实存在于当前系统串口列表中，优先使用 preferred；
    2. 否则通过 USB VID:PID (19D1:0001 / 17D1:0001) 以及 location (x.6) 自动精准匹配；
    3. 找到后返回有效端口名 (如 'COM16' 或 'COM8')，无匹配则返回 None。
    """
    try:
        import serial.tools.list_ports
        ports = list(serial.tools.list_ports.comports())
    except Exception as e:
        log(f"扫描系统串口异常: {e}")
        return preferred

    # 1. 优先检查 preferred 是否依然在线且非空
    if preferred:
        for p in ports:
            if p.device.upper() == preferred.upper():
                return preferred

    # 2. 精准匹配合宙/移芯 4G 模组通信口 (x.6 / MI_06)
    candidates = []
    for p in ports:
        hwid = (p.hwid or "").upper()
        vid = hex(p.vid).upper() if p.vid else ""
        pid = hex(p.pid).upper() if p.pid else ""
        loc = getattr(p, "location", "") or ""

        # 匹配合宙/移芯模组 VID:PID 19D1:0001 或 17D1:0001
        is_cellular = ("19D1:0001" in hwid or "17D1:0001" in hwid or 
                       (("19D1" in vid or "17D1" in vid) and ("0001" in pid or "1" in pid)))
        if is_cellular:
            # 优先命中 VUART 用户虚拟串口 (x.6 或 MI_06)
            if loc.endswith("x.6") or ":X.6" in loc.upper() or "MI_06" in hwid or "X.6" in hwid:
                return p.device
            candidates.append(p.device)

    # 3. 若无明确 x.6 标识但有候选模组串口，返回首个
    for c in candidates:
        return c

    return None

def get_config_dir() -> str:
    """获取配置持久化目录：若在 PyInstaller 冻结环境，取 exe 所在目录；否则取源码目录"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

GATEWAY_CONFIG_PATH = os.path.join(get_config_dir(), "gateway_config.json")

def log(msg: str):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if sys.stderr is not None:
        try:
            sys.stderr.write(f"[{now}] [Hub] {msg}\n")
            sys.stderr.flush()
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


def get_windows_clipboard() -> str:
    """免依赖使用 ctypes 读取 Windows 系统剪贴板中的文本（含重试防抖）"""
    if sys.platform != "win32":
        return ""
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
            return ""

        h_data = user32.GetClipboardData(CF_UNICODETEXT)
        if not h_data:
            user32.CloseClipboard()
            return ""
        p_data = kernel32.GlobalLock(h_data)
        if not p_data:
            user32.CloseClipboard()
            return ""
        val = ctypes.wstring_at(p_data)
        kernel32.GlobalUnlock(h_data)
        user32.CloseClipboard()
        return val
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.CloseClipboard()
        except Exception:
            pass
        return ""


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
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Air780EPV').Show($toast)
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


def test_channel_push(channel: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """单渠道连通性测试 (Test Ping)"""
    url = (cfg.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "URL 不能为空", "cost_ms": 0}

    secret = (cfg.get("secret") or "").strip()
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    title = "🔔 Air780EPV 网关连通性测试"
    payload_dict = {}
    target_url = url

    if channel == "feishu":
        # 升级为飞书 Card Schema 2.0，确保 PC 端悬停一键复制，彻底消除反引号外露
        card = {
            "schema": "2.0",
            "config": {
                "update_multi": True,
                "style": {
                    "text_size": {
                        "normal_v2": {
                            "default": "normal",
                            "pc": "normal",
                            "mobile": "heading"
                        }
                    }
                }
            },
            "header": {
                "title": {"tag": "plain_text", "content": "🔔 连通性测试正常"},
                "template": "green"
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": f"**测试渠道：** 飞书自定义机器人\n**测试时间：** {now_str}"
                    },
                    {"tag": "hr"},
                    {
                        "tag": "markdown",
                        "content": "**验证码示例：**\n```text\n886622\n```"
                    },
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": "<font color='grey'>设备ID: Air780EPV （上位机推送）</font>"
                        }
                    }
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
                "content": f"### 🔔 Air780EPV 网关连通性测试\n> **测试渠道**: 企业微信群机器人\n> **测试时间**: {now_str}\n> **状态**: <font color=\"info\">连通正常</font>"
            }
        }

    elif channel == "dingtalk":
        if secret:
            ts = str(round(time.time() * 1000))
            sign_str = f"{ts}\n{secret}".encode("utf-8")
            hmac_code = hmac.new(secret.encode("utf-8"), sign_str, digestmod=hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code).decode("utf-8"))
            sep = "&" if "?" in target_url else "?"
            target_url = f"{target_url}{sep}timestamp={ts}&sign={sign}"
        payload_dict = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": f"### 🔔 Air780EPV 网关连通性测试\n> **测试渠道**: 钉钉机器人\n> **测试时间**: {now_str}\n> **状态**: 连通正常"
            }
        }

    elif channel == "bark":
        payload_dict = {
            "title": "🔔 Air780EPV 连通性测试",
            "body": f"测试时间: {now_str}\n连通性测试正常！收到验证码将支持自动复制到剪贴板。",
            "group": cfg.get("group") or "Air780EPV",
            "sound": cfg.get("sound") or "minuet",
            "copy": "886622"
        }

    elif channel == "webhook":
        payload_dict = {
            "device": "Air780EPV",
            "type": "test_ping",
            "time": now_str,
            "timestamp": int(time.time()),
            "message": "Air780EPV 智能网关连通性测试正常"
        }

    else:
        return {"ok": False, "error": f"未知渠道类型: {channel}", "cost_ms": 0}

    t0 = time.time()
    try:
        body_bytes = json.dumps(payload_dict, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            target_url,
            data=body_bytes,
            headers={"Content-Type": "application/json; charset=utf-8"}
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            cost_ms = round((time.time() - t0) * 1000, 1)
            resp_bytes = resp.read()
            resp_text = resp_bytes.decode("utf-8", errors="ignore")
            # 飞书/钉钉可能在 HTTP 200 下返回包含业务错误码的 JSON
            try:
                rj = json.loads(resp_text)
                if isinstance(rj, dict):
                    if rj.get("code", 0) != 0 and "msg" in rj:
                        return {
                            "ok": False,
                            "status": resp.status,
                            "error": f"平台错误 (code {rj.get('code')}): {rj.get('msg')}",
                            "cost_ms": cost_ms
                        }
                    if rj.get("errcode", 0) != 0 and "errmsg" in rj:
                        return {
                            "ok": False,
                            "status": resp.status,
                            "error": f"平台错误 (errcode {rj.get('errcode')}): {rj.get('errmsg')}",
                            "cost_ms": cost_ms
                        }
            except Exception:
                pass

            return {
                "ok": True,
                "status": resp.status,
                "cost_ms": cost_ms,
                "message": f"连通性测试成功 (HTTP {resp.status}, 耗时 {cost_ms}ms)"
            }
    except urllib.error.HTTPError as e:
        cost_ms = round((time.time() - t0) * 1000, 1)
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        return {
            "ok": False,
            "status": e.code,
            "error": f"HTTP {e.code}: {e.reason} {err_body}".strip(),
            "cost_ms": cost_ms
        }
    except Exception as e:
        cost_ms = round((time.time() - t0) * 1000, 1)
        return {
            "ok": False,
            "status": 0,
            "error": str(e),
            "cost_ms": cost_ms
        }

class GatewayHub:
    def __init__(self, host: str = HUB_HOST, port: int = HUB_PORT, com: str = SERIAL_PORT, baud: int = SERIAL_BAUD):
        self.host = host
        self.port = port
        detected_com = find_cellular_vuart_port(com)
        self.com = detected_com if detected_com else com
        self.baud = baud

        self.running = False
        self.server_sock: Optional[socket.socket] = None
        self.clients: List[socket.socket] = []
        self.clients_lock = threading.Lock()

        self.ser: Optional[serial.Serial] = None
        self.serial_lock = threading.Lock()
        self.serial_online: bool = False  # 物理串口真实通信在线状态

        # 状态与近期事件缓存
        self.latest_status: Dict[str, Any] = {
            "online": False,
            "csq": 0,
            "rsrp": 0,
            "temp": 0,
            "vbat": 0,
            "sms_count": 0,
            "uptime": 0,
            "model": "Air780EPV",
            "rndis": False,
            "cellular_data": False
        }
        self.latest_otp: Optional[Dict[str, Any]] = None
        self.recent_sms: List[Dict[str, Any]] = []
        self.board_cellular_data: bool = False  # 默认板端蜂窝数据关闭 (0流量保号态)
        self.notify_config: Dict[str, Any] = self._load_notify_config()
        self.state_lock = threading.Lock()

    def _load_notify_config(self) -> Dict[str, Any]:
        """优先从上位机本地 gateway_config.json 加载；若不存在则回退解析板端 config.lua 并生成初始配置"""
        json_path = GATEWAY_CONFIG_PATH
        cfg = {
            "feishu": {"enable": 0, "url": "", "secret": ""},
            "wecom": {"enable": 0, "url": ""},
            "dingtalk": {"enable": 0, "url": "", "secret": ""},
            "bark": {"enable": 0, "url": "", "group": "Air780EPV", "sound": "minuet"},
            "webhook": {"enable": 0, "url": "", "method": "POST"}
        }

        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                for k, v in loaded.items():
                    if k in cfg and isinstance(v, dict):
                        cfg[k].update(v)
                enabled_list = [ch for ch, item in cfg.items() if item.get("enable") in (1, True, "1") and item.get("url")]
                log(f"成功加载上位机动态通知配置 (gateway_config.json): 已启用渠道={enabled_list}")
                return cfg
            except Exception as e:
                log(f"解析 gateway_config.json 异常: {e}，回退至 config.lua")

        # 回退解析 config.lua (若在源码开发环境且存在)
        cur_dir = os.path.dirname(os.path.abspath(__file__)) if not getattr(sys, "frozen", False) else get_config_dir()
        cfg_path = os.path.normpath(os.path.join(cur_dir, "..", "..", "deploy", "smart-gateway-780epv", "config.lua"))
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                for ch in ["feishu", "wecom", "dingtalk", "bark", "webhook"]:
                    pattern = rf"{ch}\s*=\s*\{{([^}}]+)\}}"
                    m = re.search(pattern, content)
                    if m:
                        block = m.group(1)
                        en = re.search(r"enable\s*=\s*(\d+)", block)
                        url = re.search(r"url\s*=\s*['\"]([^'\"]*)['\"]", block)
                        if en: cfg[ch]["enable"] = int(en.group(1))
                        if url: cfg[ch]["url"] = url.group(1)
                        if ch == "bark":
                            group = re.search(r"group\s*=\s*['\"]([^'\"]*)['\"]", block)
                            sound = re.search(r"sound\s*=\s*['\"]([^'\"]*)['\"]", block)
                            if group: cfg[ch]["group"] = group.group(1)
                            if sound: cfg[ch]["sound"] = sound.group(1)
                # 首次自动持久化为本地 gateway_config.json
                try:
                    with open(json_path, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, ensure_ascii=False, indent=2)
                    log("已从 config.lua 初始化生成本地 gateway_config.json")
                except Exception as ex:
                    log(f"初始化写入 gateway_config.json 失败: {ex}")

                enabled_list = [ch for ch, item in cfg.items() if item.get("enable") in (1, True, "1") and item.get("url")]
                log(f"成功加载通知配置: 已启用渠道={enabled_list}")
            except Exception as e:
                log(f"读取 config.lua 失败: {e}")
        return cfg

    def reload_notify_config(self) -> Dict[str, Any]:
        """热重载通知渠道配置"""
        with self.state_lock:
            self.notify_config = self._load_notify_config()
            log("通知渠道配置已完成热重载")
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
            self.server_sock.listen(10)
        except OSError as e:
            # 端口已被占用，说明已有 Hub 实例在运行，安静退出
            log(f"端口 {self.host}:{self.port} 已被独占，已有 Hub 实例运行，本进程安全退出。({e})")
            sys.exit(0)

        self.running = True
        log(f"网关共享中枢启动就绪，监听 IPC 端口: {self.host}:{self.port}")

        # 2. 启动串口监听与重连线程
        self.serial_thread = threading.Thread(target=self._serial_loop, daemon=True)
        self.serial_thread.start()

        # 3. 启动 TCP 监听主循环
        self._tcp_server_loop()

    def stop(self):
        """安全停止共享中枢及串口、IPC 套接字"""
        self.running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass
        with self.serial_lock:
            if self.ser:
                try:
                    self.ser.close()
                except Exception:
                    pass
        with self.clients_lock:
            for c in self.clients:
                try:
                    c.close()
                except Exception:
                    pass
            self.clients.clear()

    def _ensure_serial_connected(self) -> bool:
        with self.serial_lock:
            if self.ser and self.ser.is_open:
                try:
                    # Windows 上 USB 设备热拔插重枚举后通过 in_waiting 探测句柄有效性
                    _ = self.ser.in_waiting
                    return True
                except Exception as e:
                    log(f"检测到蜂窝串口句柄失效 ({e})，准备重新连接...")
                    try:
                        self.ser.close()
                    except Exception:
                        pass
                    self.ser = None

            # 动态探测并自愈端口漂移 (如 COM8 -> COM16)
            detected = find_cellular_vuart_port(self.com)
            if detected and detected.upper() != (self.com or "").upper():
                log(f"检测到蜂窝串口漂移自愈: 原 {self.com} -> 现 {detected}")
                self.com = detected
            elif not detected and not self.com:
                self._mark_serial_disconnected("未检测到有效蜂窝虚拟串口")
                return False

            target_port = self.com or detected
            try:
                self.ser = serial.Serial(target_port, self.baud, timeout=0.5)
                self.ser.dtr = True
                self.ser.rts = True
                self.com = target_port
                log(f"成功打开蜂窝串口: {self.com} @ {self.baud} (等待首帧数据建立业务在线)")
                return True
            except Exception as e:
                # 若连接失败且当前指定端口已失效，尝试重新探测一次
                alt_port = find_cellular_vuart_port(None)
                if alt_port and alt_port.upper() != (self.com or "").upper():
                    try:
                        self.ser = serial.Serial(alt_port, self.baud, timeout=0.5)
                        self.ser.dtr = True
                        self.ser.rts = True
                        log(f"自愈重连成功: 切换至 {alt_port} @ {self.baud}")
                        self.com = alt_port
                        return True
                    except Exception:
                        pass
                self.ser = None
                self._mark_serial_disconnected(f"打开串口失败: {e}")
                return False

    def _mark_serial_disconnected(self, reason: str = ""):
        """物理串口断开或失效处理：清洗动态工况数据并向所有 IPC 客户端广播离线事件"""
        if self.serial_online:
            self.serial_online = False
            log(f"物理串口已离线: {reason}，清洗工况缓存并广播断开事件")
            with self.state_lock:
                self.latest_status = {
                    "online": False,
                    "csq": 0,
                    "rsrp": 0,
                    "temp": 0,
                    "vbat": 0,
                    "sms_count": 0,
                    "uptime": 0,
                    "model": "Air780EPV",
                    "rndis": False,
                    "cellular_data": False
                }
            dis_evt = json.dumps({
                "type": "event",
                "event": "device_disconnected",
                "data": {"online": False, "reason": reason}
            }) + "\n"
            self._broadcast(dis_evt)

    def _mark_serial_connected(self):
        """物理串口收到有效数据帧，正式确认业务在线并广播上线事件"""
        if not self.serial_online:
            self.serial_online = True
            log(f"物理串口已成功建立双向通信: {self.com}，广播上线事件")
            with self.state_lock:
                self.latest_status["online"] = True
            conn_evt = json.dumps({
                "type": "event",
                "event": "device_connected",
                "data": {"online": True, "port": self.com}
            }) + "\n"
            self._broadcast(conn_evt)

    def _serial_loop(self):
        """物理串口读取循环，断线自动重连"""
        log("串口守护线程已启动")
        while self.running:
            if not self._ensure_serial_connected():
                time.sleep(1.0)
                continue

            try:
                line_bytes = b""
                if self.ser and self.ser.is_open:
                    line_bytes = self.ser.readline()
                else:
                    time.sleep(0.5)
                    continue

                if not line_bytes:
                    continue

                line = line_bytes.decode('utf-8', errors='ignore').strip()
                if not line:
                    continue

                # 校验是否为合法 JSON
                if line.startswith("{") and line.endswith("}"):
                    try:
                        obj = json.loads(line)
                        self._mark_serial_connected()
                        self._process_incoming_frame(obj)
                    except Exception:
                        pass

                # 全网广播给所有已连接的 TCP 客户端
                self._broadcast(line + "\n")

            except (serial.SerialException, OSError) as e:
                log(f"物理串口发生瞬断异常: {e}，正在尝试自愈...")
                self._mark_serial_disconnected(f"串口瞬断: {e}")
                with self.serial_lock:
                    if self.ser:
                        try:
                            self.ser.close()
                        except Exception:
                            pass
                        self.ser = None
                time.sleep(1.0)
            except Exception as e:
                log(f"串口循环未捕获异常: {e}")
                time.sleep(0.5)

    def _send_serial_cmd(self, cmd_dict: Dict[str, Any]) -> bool:
        """向物理串口下发格式化 JSON 指令"""
        payload = json.dumps(cmd_dict, ensure_ascii=False) + "\r\n"
        with self.serial_lock:
            if not self.ser or not self.ser.is_open:
                return False
            try:
                self.ser.write(payload.encode("utf-8"))
                self.ser.flush()
                return True
            except Exception as e:
                log(f"向物理串口写入指令失败: {e}")
                return False

    def _dispatch_host_proxy_push(self, event_type: str, data: Dict[str, Any]):
        """借用宿主电脑本地宽带优先代推全渠道通知，并向模组回送 Push ACK 握手回执"""
        # 只要宿主在线且运行，无论板端是否开启蜂窝网络，一律借用电脑本地宽带代推（0 蜂窝流量消耗）
        msg_id = data.get("id")

        def _format_phone_number(raw_num: Any) -> str:
            if not raw_num:
                return "13800138000 +86"
            m = re.search(r"(?:\+86)?(\d{11})", str(raw_num))
            if m:
                return f"{m.group(1)} +86"
            return str(raw_num)

        title = ""
        plain_text = ""
        md_text = ""
        extra_otp = ""

        if event_type == "sms_rx":
            sender = data.get("from", "未知")
            content = data.get("content", "")
            code = data.get("code", "")
            extra_otp = code or ""
            title = "📩 收到新短信"
            otp_str = f"\r\n🔑 提取验证码: 【{code}】" if code else ""
            otp_md = f"\n> **提取验证码**: <font color=\"warning\">{code}</font>" if code else ""
            plain_text = f"发件人: {sender}\r\n内容: {content}{otp_str}"
            md_text = f"### {title}\n> **发件人**: {sender}\n> **短信正文**: {content}{otp_md}"

        elif event_type == "call_rx":
            sender = data.get("from", "未知号码")
            title = "📞 拦截到呼入电话"
            plain_text = f"呼入号码: {sender}\r\n动作: 已自动秒级拒接 (双方0元话费)"
            md_text = f"### {title}\n> **呼入号码**: {sender}\n> **拦截动作**: 已自动秒级拒接 (双方0元话费)"

        elif event_type in ("gateway_ready", "state_change"):
            title = "🚀 智能通信网关已上线" if event_type == "gateway_ready" else "⚙️ 网关配置状态变更"
            bsp = data.get("bsp", "Air780EPV")
            num = _format_phone_number(data.get("formatted_number") or data.get("number"))
            csq = data.get("csq", 0)
            rsrp = data.get("rsrp", 0)
            temp = data.get("temp", 0)
            vbat = data.get("vbat", 0)
            ver = data.get("version", "LuatOS-SoC_V2001_EC718PV")
            rndis_str = "🟢 已打开" if data.get("rndis") else "⚪ 已关闭"
            data_str = "🟢 已打开" if data.get("cellular_data") else "⚪ 已关闭"

            plain_text = (
                f"设备ID：{bsp}\r\n"
                f"号码：{num}\r\n"
                f"信号：CSQ {csq} (RSRP {rsrp} dBm)\r\n"
                f"版本：{ver}\r\n"
                f"温度：{temp} ℃\r\n"
                f"电压：{vbat} V\r\n"
                f"USB共享：{rndis_str}\r\n"
                f"蜂窝网络：{data_str}"
            )
            md_text = f"### {title}\n```\n{plain_text}\n```"

        else:
            return

        # 准备待代推的通道列表: List[Tuple[ch_name, url, payload_bytes]]
        channels_to_send: List[Tuple[str, str, bytes]] = []

        # 1. 飞书 (升级为 Schema 2.0 交互卡片，带原生代码块与纯数字气泡双发)
        feishu_cfg = self.notify_config.get("feishu", {})
        if feishu_cfg.get("enable") and feishu_cfg.get("url"):
            card_template = "blue"
            card_title = title
            body_elements = []

            if event_type == "sms_rx":
                card_template = "orange" if extra_otp else "blue"
                card_title = "🔑 收到短信验证码" if extra_otp else "📩 收到新短信"
                sender = data.get("from", "未知")
                content = data.get("content", "")
                now_str = time.strftime("%Y-%m-%d %H:%M:%S")

                body_elements.append({
                    "tag": "markdown",
                    "content": f"**发件人：** {sender}\n**到站时间：** {now_str}"
                })
                body_elements.append({"tag": "hr"})

                if extra_otp:
                    body_elements.append({
                        "tag": "markdown",
                        "content": f"**验证码：**\n```text\n{extra_otp}\n```"
                    })
                    body_elements.append({"tag": "hr"})

                body_elements.append({
                    "tag": "markdown",
                    "content": f"**短信原文：**\n{content}"
                })
                body_elements.append({"tag": "hr"})

            elif event_type == "call_rx":
                card_template = "carmine"
                card_title = "📞 拦截到呼入电话"
                sender = data.get("from", "未知号码")
                now_str = time.strftime("%Y-%m-%d %H:%M:%S")
                body_elements.append({
                    "tag": "markdown",
                    "content": f"**呼入号码：** {sender}\n**拦截时间：** {now_str}\n\n**拦截处理：** 已自动秒级拒接挂断（双方 0 元话费）"
                })
                body_elements.append({"tag": "hr"})

            else:  # gateway_ready / state_change
                card_template = "turquoise"
                card_title = "🚀 智能网关已上线" if event_type == "gateway_ready" else "⚙️ 网关配置状态变更"
                body_elements.append({
                    "tag": "markdown",
                    "content": f"```text\n{plain_text}\n```"
                })
                body_elements.append({"tag": "hr"})

            body_elements.append({
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "<font color='grey'>设备ID: Air780EPV （上位机推送）</font>"
                }
            })

            card_payload = {
                "schema": "2.0",
                "config": {
                    "update_multi": True,
                    "style": {
                        "text_size": {
                            "normal_v2": {
                                "default": "normal",
                                "pc": "normal",
                                "mobile": "heading"
                            }
                        }
                    }
                },
                "header": {
                    "title": {"tag": "plain_text", "content": card_title},
                    "template": card_template
                },
                "body": {
                    "direction": "vertical",
                    "padding": "12px 12px 12px 12px",
                    "elements": body_elements
                }
            }

            feishu_body_dict: Dict[str, Any] = {"msg_type": "interactive", "card": card_payload}
            secret = (feishu_cfg.get("secret") or "").strip()
            if secret:
                ts = str(int(time.time()))
                sign_str = f"{ts}\n{secret}"
                hmac_code = hmac.new(sign_str.encode("utf-8"), digestmod=hashlib.sha256).digest()
                feishu_body_dict["timestamp"] = ts
                feishu_body_dict["sign"] = base64.b64encode(hmac_code).decode("utf-8")

            feishu_body = json.dumps(feishu_body_dict, ensure_ascii=False).encode("utf-8")
            channels_to_send.append(("飞书", feishu_cfg["url"], feishu_body))

        # 2. 企业微信
        wecom_cfg = self.notify_config.get("wecom", {})
        if wecom_cfg.get("enable") and wecom_cfg.get("url"):
            wecom_body = json.dumps({
                "msgtype": "markdown",
                "markdown": {"content": md_text}
            }).encode("utf-8")
            channels_to_send.append(("企业微信", wecom_cfg["url"], wecom_body))

        # 3. 钉钉
        dingtalk_cfg = self.notify_config.get("dingtalk", {})
        if dingtalk_cfg.get("enable") and dingtalk_cfg.get("url"):
            target_ding_url = dingtalk_cfg["url"]
            ding_secret = (dingtalk_cfg.get("secret") or "").strip()
            if ding_secret:
                ts = str(round(time.time() * 1000))
                sign_str = f"{ts}\n{ding_secret}".encode("utf-8")
                hmac_code = hmac.new(ding_secret.encode("utf-8"), sign_str, digestmod=hashlib.sha256).digest()
                sign = urllib.parse.quote_plus(base64.b64encode(hmac_code).decode("utf-8"))
                sep = "&" if "?" in target_ding_url else "?"
                target_ding_url = f"{target_ding_url}{sep}timestamp={ts}&sign={sign}"

            dingtalk_body = json.dumps({
                "msgtype": "markdown",
                "markdown": {"title": title, "text": md_text}
            }).encode("utf-8")
            channels_to_send.append(("钉钉", target_ding_url, dingtalk_body))

        # 4. iOS Bark
        bark_cfg = self.notify_config.get("bark", {})
        if bark_cfg.get("enable") and bark_cfg.get("url"):
            bark_payload = {
                "title": title,
                "body": plain_text,
                "group": bark_cfg.get("group", "Air780EPV"),
                "sound": bark_cfg.get("sound", "minuet"),
                "badge": 1
            }
            if extra_otp:
                bark_payload["copy"] = extra_otp
                bark_payload["automaticallyCopy"] = 1
            bark_body = json.dumps(bark_payload).encode("utf-8")
            channels_to_send.append(("Bark", bark_cfg["url"], bark_body))

        # 5. 自定义 Webhook
        webhook_cfg = self.notify_config.get("webhook", {})
        if webhook_cfg.get("enable") and webhook_cfg.get("url"):
            webhook_body = json.dumps({
                "device": "Air780EPV",
                "type": event_type,
                "from": data.get("from", ""),
                "content": plain_text,
                "otp": extra_otp,
                "timestamp": int(time.time())
            }).encode("utf-8")
            channels_to_send.append(("Webhook", webhook_cfg["url"], webhook_body))

        # 执行并发宽带代推并安全回送 Push ACK 握手回执给板端
        def _proxy_worker():
            success_count = 0
            fail_count = 0

            def _send_single(ch_name: str, url: str, payload_bytes: bytes) -> bool:
                try:
                    req = urllib.request.Request(
                        url,
                        data=payload_bytes,
                        headers={"Content-Type": "application/json; charset=utf-8"}
                    )
                    with urllib.request.urlopen(req, timeout=4) as resp:
                        if resp.status == 200:
                            log(f"宿主宽带代推 [{event_type}] 到 {ch_name} 成功 (HTTP {resp.status})")
                            return True
                        else:
                            log(f"宿主宽带代推 [{event_type}] 到 {ch_name} 状态异常 (HTTP {resp.status})")
                            return False
                except Exception as e:
                    log(f"宿主宽带代推 [{event_type}] 到 {ch_name} 失败: {e}")
                    return False

            if channels_to_send:
                threads = []
                results = [False] * len(channels_to_send)

                def _runner(idx: int, ch: str, u: str, b: bytes):
                    results[idx] = _send_single(ch, u, b)

                for i, (ch, u, b) in enumerate(channels_to_send):
                    t = threading.Thread(target=_runner, args=(i, ch, u, b))
                    threads.append(t)
                    t.start()

                for t in threads:
                    t.join(timeout=4.0)

                success_count = sum(1 for r in results if r)
                fail_count = len(results) - success_count

            # 飞书纯数字气泡（仅在有验证码且飞书开启时触发）
            if extra_otp and feishu_cfg.get("enable") and feishu_cfg.get("url"):
                def _send_pure_otp_bubble():
                    time.sleep(0.3)
                    pure_dict = {
                        "msg_type": "text",
                        "content": {"text": str(extra_otp)}
                    }
                    secret_val = (feishu_cfg.get("secret") or "").strip()
                    if secret_val:
                        ts2 = str(int(time.time()))
                        sign_str2 = f"{ts2}\n{secret_val}"
                        hmac2 = hmac.new(sign_str2.encode("utf-8"), digestmod=hashlib.sha256).digest()
                        pure_dict["timestamp"] = ts2
                        pure_dict["sign"] = base64.b64encode(hmac2).decode("utf-8")
                    pure_bytes = json.dumps(pure_dict, ensure_ascii=False).encode("utf-8")
                    _send_single("飞书纯数字气泡", feishu_cfg["url"], pure_bytes)

                threading.Thread(target=_send_pure_otp_bubble, daemon=True).start()

            # 核心回执闭环：针对短信与来电回发 notify_ack 帧 (AIR-17)
            if msg_id and event_type in ("sms_rx", "call_rx"):
                # 如果用户未开启任何外推渠道（宿主接管静默），或至少有一个渠道发送成功：
                if not channels_to_send or success_count > 0:
                    ack_packet = {
                        "type": "cmd",
                        "id": f"ack_{int(time.time()*1000)}",
                        "cmd": "notify_ack",
                        "data": {
                            "id": msg_id,
                            "status": "ok"
                        }
                    }
                    if self._send_serial_cmd(ack_packet):
                        log(f"⚡ [PUSH_ACK] 已向模组串口回送 notify_ack [ok]，注销板端自推 (msg_id: {msg_id})")
                    else:
                        log(f"⚠️ [PUSH_ACK] 向模组回送 notify_ack [ok] 失败 (串口不可用)")
                else:
                    # 启用了渠道但全部发送失败（如电脑断网）-> 回送 fallback 触发板端 4G 应急自推
                    ack_packet = {
                        "type": "cmd",
                        "id": f"ack_{int(time.time()*1000)}",
                        "cmd": "notify_ack",
                        "data": {
                            "id": msg_id,
                            "status": "fallback"
                        }
                    }
                    if self._send_serial_cmd(ack_packet):
                        log(f"⚠️ [PUSH_ACK] 宿主代推全失败，已回送 notify_ack [fallback]，触发板端应急自推 (msg_id: {msg_id})")

        threading.Thread(target=_proxy_worker, daemon=True).start()

    def _process_incoming_frame(self, obj: Dict[str, Any]):
        """更新内部内存缓存与代推决策"""
        frame_type = obj.get("type")
        if frame_type == "event":
            evt = obj.get("event")
            data = obj.get("data", {})
            with self.state_lock:
                if evt in ("status", "gateway_ready", "state_change"):
                    self.latest_status = data
                    if "cellular_data" in data:
                        self.board_cellular_data = (data["cellular_data"] == True)
                elif evt == "sms_rx":
                    self.recent_sms.insert(0, data)
                    if len(self.recent_sms) > 50:
                        self.recent_sms.pop()
                    code = data.get("code")
                    if not code:
                        content = data.get("content", "")
                        sender = data.get("from", "")
                        kw = r"(?:验证码|校验码|动态码|动态密码|口令|动态口令|授权码|确认码|激活码|安全码|检验码|取件码|code|otp|pin)"
                        m = re.search(kw + r"[^\d]{0,15}(\d{4,8})", content, re.IGNORECASE) or re.search(r"(\d{4,8})[^\d]{0,15}" + kw, content, re.IGNORECASE)
                        if m and m.group(1) != sender:
                            code = m.group(1)
                            data["code"] = code

                    if code:
                        self.latest_otp = {
                            "code": code,
                            "from": data.get("from"),
                            "content": data.get("content"),
                            "time": obj.get("ts", int(time.time()))
                        }
                        # 方案 1：毫秒级直写 Windows 宿主机系统剪贴板并弹窗提示
                        if set_windows_clipboard(code):
                            log(f"⚡ [CLIPBOARD] 验证码 [{code}] 已自动存入 Windows 剪贴板，用户可直接 Ctrl+V 粘贴")
                            show_windows_toast("Air780EPV 智能网关", f"⚡ 捕获验证码：{code} (已自动存入剪贴板，直接按 Ctrl+V 粘贴)")

            # 触发宿主宽带代推
            if evt in ("sms_rx", "call_rx", "gateway_ready", "state_change"):
                self._dispatch_host_proxy_push(evt, data)

        elif frame_type == "response":
            data = obj.get("data", {})
            if isinstance(data, dict) and "cellular_data" in data:
                with self.state_lock:
                    self.board_cellular_data = (data["cellular_data"] == True)
                    log(f"已同步板端蜂窝数据状态: {self.board_cellular_data}")

    def _broadcast(self, text: str):
        """向所有连接的客户端推送报文"""
        data = text.encode("utf-8")
        with self.clients_lock:
            dead_clients = []
            for c in self.clients:
                try:
                    c.sendall(data)
                except Exception:
                    dead_clients.append(c)
            for dc in dead_clients:
                self.clients.remove(dc)
                try:
                    dc.close()
                except Exception:
                    pass

    def _handle_client_send(self, line: str):
        """接收客户端命令：拦截内部控制指令，其余透传写入物理串口"""
        line_clean = line.strip()
        if not line_clean:
            return

        # 检查是否为内部命令
        if line_clean.startswith("{") and line_clean.endswith("}"):
            try:
                cmd_obj = json.loads(line_clean)
                cmd_name = cmd_obj.get("cmd")
                if cmd_name == "reload_notify_config":
                    new_cfg = self.reload_notify_config()
                    resp = json.dumps({"type": "response", "cmd": "reload_notify_config", "status": "ok", "config": new_cfg}) + "\n"
                    self._broadcast(resp)
                    return
                elif cmd_name == "set_notify_config":
                    data = cmd_obj.get("data")
                    if isinstance(data, dict):
                        # 同步更新 Hub 本地内存与持久化
                        cur = self.load_notify_config()
                        for k, v in data.items():
                            if k in cur and isinstance(v, dict):
                                cur[k].update(v)
                            elif isinstance(v, dict):
                                cur[k] = v
                        with open(GATEWAY_CONFIG_PATH, "w", encoding="utf-8") as f:
                            json.dump(cur, f, ensure_ascii=False, indent=2)
                        self.notify_config = cur
                        log("Hub 已接收并更新本地通知配置，正透传下发板端持久化...")
            except Exception:
                pass

        with self.serial_lock:
            if not self.ser or not self.ser.is_open or not self.serial_online:
                log("串口未连接或处于离线态，执行 Fast-Fail 立即回送失败响应")
                if line_clean.startswith("{") and line_clean.endswith("}"):
                    try:
                        cmd_obj = json.loads(line_clean)
                        req_id = cmd_obj.get("id")
                        if req_id:
                            fail_resp = json.dumps({
                                "type": "response",
                                "id": req_id,
                                "ok": False,
                                "online": False,
                                "error": "4G 短信棒未插入或物理串口已断开"
                            }) + "\n"
                            self._broadcast(fail_resp)
                    except Exception:
                        pass
                return
            try:
                self.ser.write((line_clean + "\r\n").encode("utf-8"))
                self.ser.flush()
            except Exception as e:
                log(f"写入物理串口失败: {e}，重置串口等待重连...")
                self._mark_serial_disconnected(f"写入异常: {e}")
                try:
                    if self.ser:
                        self.ser.close()
                except Exception:
                    pass
                self.ser = None

    def _tcp_server_loop(self):
        """TCP 连接分发线程"""
        while self.running:
            try:
                client_sock, addr = self.server_sock.accept()
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                log(f"客户端已连接: {addr}")

                with self.clients_lock:
                    self.clients.append(client_sock)

                # 初始握手：向新客户端主动推送一份最新状态（若有）
                with self.state_lock:
                    if self.latest_status:
                        cached_evt = json.dumps({"type": "event", "event": "status", "data": self.latest_status}) + "\n"
                        try:
                            client_sock.sendall(cached_evt.encode("utf-8"))
                        except Exception:
                            pass

                # 为该客户端开辟独立接收线程
                t = threading.Thread(target=self._client_worker, args=(client_sock, addr), daemon=True)
                t.start()
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
                        self._handle_client_send(line)
            except Exception:
                break

        log(f"客户端已断开: {addr}")
        with self.clients_lock:
            if sock in self.clients:
                self.clients.remove(sock)
        try:
            sock.close()
        except Exception:
            pass

_single_instance_mutex = None

def _acquire_single_instance_mutex():
    global _single_instance_mutex
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        _single_instance_mutex = kernel32.CreateMutexW(None, True, "Air780EPV_Gateway_Hub_Global_Mutex")
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
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
        if hub.server_sock:
            try:
                hub.server_sock.close()
            except Exception:
                pass
        with hub.serial_lock:
            if hub.ser:
                try:
                    hub.ser.close()
                except Exception:
                    pass
        log("Hub 已安全退出")

if __name__ == "__main__":
    main()
