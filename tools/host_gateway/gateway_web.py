#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Air780EPV 智能随身通信网关 - 局域网 Web 控制台与管理服务
基于 Python 原生轻量标准库 (http.server.ThreadingHTTPServer + socket + SSE) 实现
对外提供 RESTful API 与实时事件流，无缝对接 127.0.0.1:17800 串口中枢。
"""

import sys
import os
import time
import json
import socket
import threading
import queue
import subprocess
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

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

# 端口与地址常量
DEFAULT_WEB_HOST = "0.0.0.0"
DEFAULT_WEB_PORT = 17801
DEFAULT_HUB_HOST = "127.0.0.1"
DEFAULT_HUB_PORT = 17800

# 路径常量
def get_bundle_dir() -> str:
    """获取静态资源解压/打包根目录：PyInstaller 模式下读取 _MEIPASS"""
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

def get_config_dir() -> str:
    """获取配置持久化目录：PyInstaller 模式下写入 exe 所在目录"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

BUNDLE_DIR = get_bundle_dir()
DATA_DIR = get_config_dir()

WEB_DIR = os.path.join(BUNDLE_DIR, "web")
INDEX_HTML_PATH = os.path.join(WEB_DIR, "index.html")
GATEWAY_CONFIG_PATH = os.path.join(DATA_DIR, "gateway_config.json")

# 引入 Hub 的测试推送方法
from gateway_hub import test_channel_push

def _log(msg: str):
    if sys.stdout is not None:
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{ts}] [Web] {msg}", flush=True)
        except Exception:
            pass


class HubBackendClient:
    """与本地 127.0.0.1:17800 物理中枢通信的双向 TCP 客户端"""

    def __init__(self, host=DEFAULT_HUB_HOST, port=DEFAULT_HUB_PORT):
        self.host = host
        self.port = port
        self.sock = None
        self.sock_lock = threading.Lock()
        self.running = False
        self.rx_thread = None

        # RPC 等待字典：{ req_id: {"event": threading.Event(), "response": None} }
        self.pending_requests = {}
        self.pending_lock = threading.Lock()

        # 本地状态与历史缓存
        self.is_hardware_connected: bool = False
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
        self.recent_sms_events = []
        self.recent_calls = []
        self.cache_lock = threading.Lock()

        # SSE 广播客户端队列列表
        self.sse_listeners = []
        self.sse_lock = threading.Lock()

    def start(self):
        self.running = True
        self._ensure_connected()
        if not self.rx_thread or not self.rx_thread.is_alive():
            self.rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self.rx_thread.start()

    def stop(self):
        self.running = False
        with self.sock_lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

    def _auto_spawn_hub(self):
        hub_path = os.path.join(BASE_DIR, "gateway_hub.py")
        if not os.path.exists(hub_path):
            return
        _log(f"正在后台自拉起 gateway_hub.py 中枢: {hub_path}")
        try:
            creation_flags = 0
            if os.name == "nt":
                creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            subprocess.Popen(
                [sys.executable, hub_path],
                creationflags=creation_flags,
                close_fds=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            _log(f"自拉起 Hub 失败: {e}")

    def _ensure_connected(self) -> bool:
        with self.sock_lock:
            if self.sock:
                return True
            for attempt in range(1, 4):
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    s.settimeout(2.0)
                    s.connect((self.host, self.port))
                    s.settimeout(None)
                    self.sock = s
                    _log(f"已成功建立与物理中枢的连接: {self.host}:{self.port}")
                    return True
                except (ConnectionRefusedError, OSError):
                    if attempt == 1:
                        self._auto_spawn_hub()
                    time.sleep(0.5)
            return False

    def execute_cmd(self, cmd_name: str, params: dict = None, timeout: float = 8.0) -> dict:
        """向 Hub 发送命令并同步等待返回"""
        if not self._ensure_connected():
            return {"ok": False, "error": "无法连接至物理通信中枢"}

        req_id = f"web_{int(time.time()*1000)}_{os.getpid()}"
        evt = threading.Event()
        req_entry = {"event": evt, "response": None}

        with self.pending_lock:
            self.pending_requests[req_id] = req_entry

        packet = {
            "type": "cmd",
            "id": req_id,
            "cmd": cmd_name,
            "params": params or {}
        }

        try:
            line = json.dumps(packet, ensure_ascii=False) + "\n"
            with self.sock_lock:
                if not self.sock:
                    return {"ok": False, "error": "底层通信已断开"}
                self.sock.sendall(line.encode("utf-8"))
        except Exception as e:
            with self.pending_lock:
                self.pending_requests.pop(req_id, None)
            return {"ok": False, "error": f"指令写入套接字失败: {e}"}

        # 挂起等待响应
        if evt.wait(timeout=timeout):
            resp = req_entry.get("response", {})
            return resp
        else:
            with self.pending_lock:
                self.pending_requests.pop(req_id, None)
            return {"ok": False, "error": f"等待中枢响应超时 ({timeout}s)"}

    def send_raw_command(self, raw_line: str):
        """向中枢发送一条原始字符串命令（非阻塞）"""
        try:
            line = raw_line.strip() + "\n"
            with self.sock_lock:
                if self.sock:
                    self.sock.sendall(line.encode("utf-8"))
        except Exception as e:
            _log(f"发送原始命令失败: {e}")

    def _rx_loop(self):
        buffer = ""
        while self.running:
            if not self._ensure_connected():
                time.sleep(1.0)
                continue

            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    _log("与中枢套接字断开，准备重连...")
                    with self.sock_lock:
                        if self.sock:
                            try:
                                self.sock.close()
                            except Exception:
                                pass
                            self.sock = None
                    time.sleep(1.0)
                    continue

                buffer += chunk.decode("utf-8", errors="ignore")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if line:
                        self._dispatch_frame(line)

            except Exception as e:
                if not self.running:
                    break
                _log(f"接收线程异常: {e}")
                with self.sock_lock:
                    if self.sock:
                        try:
                            self.sock.close()
                        except Exception:
                            pass
                        self.sock = None
                time.sleep(1.0)

    def _update_status_cache(self, event_data: dict):
        """归一化更新最新网关状态缓存，确保 rndis 与 cellular_data 开关状态绝对双向同步"""
        if not isinstance(event_data, dict):
            return

        with self.cache_lock:
            # 同步 rndis 开关
            if "rndis" in event_data:
                val = bool(event_data["rndis"])
                self.latest_status["rndis"] = val
                self.latest_status["rndis_enable"] = val
            elif "rndis_enable" in event_data:
                val = bool(event_data["rndis_enable"])
                self.latest_status["rndis"] = val
                self.latest_status["rndis_enable"] = val

            # 同步 cellular_data 开关
            if "cellular_data" in event_data:
                val = bool(event_data["cellular_data"])
                self.latest_status["cellular_data"] = val
                self.latest_status["cellular_data_enable"] = val
            elif "cellular_data_enable" in event_data:
                val = bool(event_data["cellular_data_enable"])
                self.latest_status["cellular_data"] = val
                self.latest_status["cellular_data_enable"] = val

            # 同步其它各项硬件和网络指标
            for k in ("model", "bsp", "csq", "rsrp", "temp", "vbat", "sms_count", "uptime", "lua_mem_kb", "version"):
                if k in event_data:
                    self.latest_status[k] = event_data[k]
            if "blackbox_count" in event_data:
                self.latest_status["sms_count"] = event_data["blackbox_count"]
            if "uptime_seconds" in event_data:
                self.latest_status["uptime"] = event_data["uptime_seconds"]

            if "bsp" in event_data and "model" not in self.latest_status:
                self.latest_status["model"] = event_data["bsp"]

    def _dispatch_frame(self, raw_line: str):
        try:
            data = json.loads(raw_line)
        except Exception:
            return

        frame_type = data.get("type")

        # 1. 响应帧 (兼容 LuatOS 标准 "res" 与上位机规范 "response")
        if frame_type in ("res", "response"):
            req_id = data.get("id")
            if req_id:
                with self.pending_lock:
                    entry = self.pending_requests.pop(req_id, None)
                    if entry:
                        # 归一化 ok 状态 (code == 0 或 ok is True)
                        if "ok" not in data:
                            data["ok"] = (data.get("code", 0) == 0)
                        entry["response"] = data
                        entry["event"].set()

        # 2. 事件广播帧
        elif frame_type == "event":
            event_name = data.get("event")
            event_data = data.get("data", {})

            if event_name in ("status", "gateway_ready", "state_change"):
                self._update_status_cache(event_data)
                with self.cache_lock:
                    status_snapshot = dict(self.latest_status)
                self.broadcast_sse("status_update", status_snapshot)

            elif event_name in ("sms_rx", "sms_received"):
                # 归一化字段
                item = {
                    "phone": event_data.get("from") or event_data.get("phone") or "未知号码",
                    "content": event_data.get("content") or "",
                    "otp": event_data.get("code") or event_data.get("otp"),
                    "time": event_data.get("time") or time.strftime("%Y-%m-%d %H:%M:%S")
                }
                with self.cache_lock:
                    self.recent_sms_events.insert(0, item)
                    if len(self.recent_sms_events) > 100:
                        self.recent_sms_events.pop()
                self.broadcast_sse("sms_received", item)

            elif event_name in ("call_rx", "call_incoming"):
                item = {
                    "phone": event_data.get("from") or event_data.get("phone") or "未知号码",
                    "time": event_data.get("time") or time.strftime("%Y-%m-%d %H:%M:%S"),
                    "action": event_data.get("action") or "rejected"
                }
                with self.cache_lock:
                    self.recent_calls.insert(0, item)
                    if len(self.recent_calls) > 50:
                        self.recent_calls.pop()
                self.broadcast_sse("call_incoming", item)

    def register_sse_listener(self) -> queue.Queue:
        q = queue.Queue(maxsize=128)
        with self.sse_lock:
            self.sse_listeners.append(q)
        return q

    def unregister_sse_listener(self, q: queue.Queue):
        with self.sse_lock:
            if q in self.sse_listeners:
                self.sse_listeners.remove(q)

    def broadcast_sse(self, event_name: str, payload: dict):
        with self.sse_lock:
            listeners = list(self.sse_listeners)
        for q in listeners:
            try:
                q.put_nowait({"event": event_name, "data": payload})
            except queue.Full:
                pass


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """支持每个连接多线程独立运行的 HTTP 服务器（关键：支持长期存活的 SSE 连接）"""
    daemon_threads = True
    allow_reuse_address = True


class GatewayWebHandler(BaseHTTPRequestHandler):
    """处理静态资产与 RESTful API 的 HTTP 请求处理器"""

    backend: HubBackendClient = None  # 类级注入

    def log_message(self, format, *args):
        # 屏蔽原生琐碎的 access log，保持输出清晰
        pass

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def _send_json_resp(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # 1. 主页静态资产
        if path in ("/", "/index.html"):
            if os.path.exists(INDEX_HTML_PATH):
                try:
                    with open(INDEX_HTML_PATH, "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    self._send_cors_headers()
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                except Exception as e:
                    self._send_json_resp(500, {"ok": False, "error": f"加载 index.html 失败: {e}"})
                    return
            else:
                fallback_html = "<html><body><h1>Air780EPV 智能通信网关</h1><p>Web 资源未找到</p></body></html>".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(fallback_html)))
                self.end_headers()
                self.wfile.write(fallback_html)
                return

        # 2. SSE 实时推流订阅接口
        if path == "/api/events":
            self.handle_sse_stream()
            return

        # 3. 获取实时全景状态看板
        if path == "/api/status":
            if not self.backend.is_hardware_connected:
                # 物理串口未连接，直接 0ms 快速响应离线态，杜绝发送 RPC 导致超时挂起
                self._send_json_resp(200, {
                    "ok": False,
                    "online": False,
                    "error": "4G 短信棒未插入或物理串口已断开",
                    "data": {
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
                })
                return

            resp = self.backend.execute_cmd("get_status", timeout=2.0)
            if resp.get("ok") and resp.get("online") is not False:
                raw_data = resp.get("data", {})
                rndis_val = raw_data.get("rndis") if "rndis" in raw_data else raw_data.get("rndis_enable", False)
                data_val = raw_data.get("cellular_data") if "cellular_data" in raw_data else raw_data.get("cellular_data_enable", False)
                norm_status = {
                    "online": True,
                    "model": raw_data.get("bsp") or raw_data.get("model") or "Air780EPV",
                    "csq": raw_data.get("csq", 0),
                    "rsrp": raw_data.get("rsrp", 0),
                    "temp": raw_data.get("temp", 0),
                    "vbat": raw_data.get("vbat", 0),
                    "rndis": bool(rndis_val),
                    "rndis_enable": bool(rndis_val),
                    "cellular_data": bool(data_val),
                    "cellular_data_enable": bool(data_val),
                    "sms_count": raw_data.get("blackbox_count") if "blackbox_count" in raw_data else raw_data.get("sms_count", 0),
                    "uptime": raw_data.get("uptime_seconds") if "uptime_seconds" in raw_data else raw_data.get("uptime", 0),
                    "lua_mem_kb": raw_data.get("lua_mem_kb", 0),
                    "raw": raw_data
                }
                self.backend.is_hardware_connected = True
                self.backend._update_status_cache(norm_status)
                self._send_json_resp(200, {"ok": True, "online": True, "data": norm_status})
            else:
                self.backend.is_hardware_connected = False
                with self.backend.cache_lock:
                    self.backend.latest_status = {
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
                self._send_json_resp(200, {
                    "ok": False,
                    "online": False,
                    "error": resp.get("error", "4G 短信棒未插入或物理串口已断开"),
                    "data": {
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
                })
            return

        # 4. 获取短信历史记录
        if path == "/api/history":
            limit_val = 15
            if "limit" in query:
                try:
                    limit_val = max(1, min(50, int(query["limit"][0])))
                except Exception:
                    pass
            cursor_val = query.get("cursor", [None])[0]
            kw = query.get("keyword", [None])[0]
            order = query.get("order", ["desc"])[0].lower() # 默认时间降序 (最新在最前)

            board_limit = min(15, limit_val)
            cmd_payload = {"limit": board_limit, "keyword": kw}
            if cursor_val and str(cursor_val).strip() not in ("", "0", "null", "undefined"):
                cmd_payload["cursor"] = str(cursor_val).strip()

            resp = self.backend.execute_cmd("get_history", cmd_payload, timeout=6.0)
            if resp.get("ok"):
                raw_data = resp.get("data", {})
                raw_items = raw_data.get("items") or raw_data.get("list") or []
                total_cnt = raw_data.get("total", len(raw_items))
                next_cur = raw_data.get("next_cursor")
                has_more = bool(next_cur and next_cur != "0")
                norm_items = []
                for it in raw_items:
                    sender = it.get("from") or it.get("sender") or it.get("phone") or "未知"
                    time_raw = it.get("time") or it.get("ts") or ""
                    if isinstance(time_raw, (int, float)) and time_raw > 1000000000:
                        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time_raw))
                    else:
                        time_str = str(time_raw)
                    norm_items.append({
                        "phone": sender,
                        "sender": sender,
                        "content": it.get("content") or "",
                        "otp": it.get("code") or it.get("otp"),
                        "time": time_str
                    })
                # 板载 LittleFS 为正序追加存储，默认降序将最新记录排在最前
                if order == "desc":
                    norm_items.reverse()
                
                resp_payload = {
                    "ok": True,
                    "data": {
                        "list": norm_items,
                        "items": norm_items,
                        "count": len(norm_items),
                        "total": total_cnt,
                        "next_cursor": next_cur,
                        "has_more": has_more,
                        "order": order
                    },
                    "list": norm_items,
                    "items": norm_items,
                    "count": len(norm_items),
                    "total": total_cnt,
                    "next_cursor": next_cur,
                    "has_more": has_more
                }
                self._send_json_resp(200, resp_payload)
            else:
                # 回退提供内存接收事件
                with self.backend.cache_lock:
                    mem_list = list(self.backend.recent_sms_events)
                if order == "asc":
                    mem_list.reverse()
                self._send_json_resp(200, {
                    "ok": True,
                    "data": {
                        "list": mem_list,
                        "items": mem_list,
                        "count": len(mem_list),
                        "total": len(mem_list),
                        "next_cursor": None,
                        "has_more": False,
                        "order": order
                    },
                    "list": mem_list,
                    "items": mem_list,
                    "count": len(mem_list),
                    "total": len(mem_list),
                    "next_cursor": None,
                    "has_more": False,
                    "cached": True
                })
            return

        # 5. 读取 Webhook 通知配置
        if path == "/api/config/notify":
            cfg = self._read_notify_config()
            self._send_json_resp(200, {"ok": True, "data": cfg})
            return

        # 404
        self._send_json_resp(404, {"ok": False, "error": "Not Found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # 读取请求体 JSON
        content_len = int(self.headers.get("Content-Length", 0))
        post_data = {}
        if content_len > 0:
            try:
                raw_bytes = self.rfile.read(content_len)
                post_data = json.loads(raw_bytes.decode("utf-8"))
            except Exception as e:
                self._send_json_resp(400, {"ok": False, "error": f"JSON 解析失败: {e}"})
                return

        # 1. 主动代发 4G 短信
        if path == "/api/sms/send":
            phone = str(post_data.get("phone", "")).strip()
            content = str(post_data.get("content", "")).strip()
            if not phone or not content:
                self._send_json_resp(400, {"ok": False, "error": "phone 和 content 不能为空"})
                return
            resp = self.backend.execute_cmd("send_sms", {"to": phone, "text": content, "phone": phone, "content": content}, timeout=12.0)
            if resp.get("ok"):
                self._send_json_resp(200, {"ok": True, "data": resp.get("data", {})})
            else:
                self._send_json_resp(500, {"ok": False, "error": resp.get("error", "发送短信超时")})
            return

        # 2. 开关 USB 随身上网 RNDIS
        if path == "/api/control/rndis":
            enable = bool(post_data.get("enable", False))
            resp = self.backend.execute_cmd("set_rndis", {"enable": enable}, timeout=5.0)
            if resp.get("ok"):
                self.backend._update_status_cache({"rndis": enable, "rndis_enable": enable})
                with self.backend.cache_lock:
                    status_snapshot = dict(self.backend.latest_status)
                self.backend.broadcast_sse("status_update", status_snapshot)
                self._send_json_resp(200, {"ok": True, "data": resp.get("data", {})})
            else:
                self._send_json_resp(500, {"ok": False, "error": resp.get("error", "切换随身上网失败")})
            return

        # 3. 开关板载 4G 蜂窝数据
        if path == "/api/control/data":
            enable = bool(post_data.get("enable", False))
            resp = self.backend.execute_cmd("set_cellular_data", {"enable": enable}, timeout=5.0)
            if resp.get("ok"):
                self.backend._update_status_cache({"cellular_data": enable, "cellular_data_enable": enable})
                with self.backend.cache_lock:
                    status_snapshot = dict(self.backend.latest_status)
                self.backend.broadcast_sse("status_update", status_snapshot)
                self._send_json_resp(200, {"ok": True, "data": resp.get("data", {})})
            else:
                self._send_json_resp(500, {"ok": False, "error": resp.get("error", "切换蜂窝数据失败")})
            return

        # 4. 安全软复位重启网关
        if path == "/api/control/reboot":
            reason = str(post_data.get("reason", "web_action"))
            resp = self.backend.execute_cmd("reboot", {"reason": reason}, timeout=4.0)
            self._send_json_resp(200, {"ok": True, "data": resp.get("data", {})})
            return

        # 5. 清空脱机黑匣子短信
        if path == "/api/control/clear_history":
            resp = self.backend.execute_cmd("clear_history", {}, timeout=5.0)
            if resp.get("ok"):
                self._send_json_resp(200, {"ok": True, "data": resp.get("data", {})})
            else:
                self._send_json_resp(500, {"ok": False, "error": resp.get("error", "清空记录失败")})
            return

        # 6. 保存 Webhook 通知配置并同步下发板端持久化
        if path == "/api/config/notify":
            if not isinstance(post_data, dict):
                self._send_json_resp(400, {"ok": False, "error": "参数必须为 JSON 对象"})
                return
            saved_cfg = self._save_notify_config(post_data)
            # 1. 触发上位机 Hub 重新加载内存中的通知配置
            self.backend.send_raw_command(json.dumps({"cmd": "reload_notify_config"}))
            
            # 2. 串口下发给板端，持久化写入板载 LittleFS fskv
            board_synced = False
            try:
                board_resp = self.backend.execute_cmd("set_notify_config", saved_cfg, timeout=3.5)
                if board_resp and board_resp.get("ok"):
                    board_synced = True
                    _log("Webhook 配置已成功下发至板端 LittleFS fskv 持久化")
                else:
                    _log(f"下发板端配置超时或未响应: {board_resp}")
            except Exception as e:
                _log(f"下发板端指令异常: {e}")

            self._send_json_resp(200, {
                "ok": True,
                "data": saved_cfg,
                "board_synced": board_synced,
                "message": "配置已保存并同步写入板载硬件存储 (fskv)" if board_synced else "配置已保存至上位机（板端未响应）"
            })
            return

        # 7. 连通性测试 (Test Ping)
        if path == "/api/config/notify/test":
            channel = post_data.get("channel", "")
            cfg = post_data.get("config", {})
            if not channel or not isinstance(cfg, dict):
                self._send_json_resp(400, {"ok": False, "error": "缺少 channel 或 config 参数"})
                return
            res = test_channel_push(channel, cfg)
            if res.get("ok"):
                self._send_json_resp(200, res)
            else:
                self._send_json_resp(400, res)
            return

        self._send_json_resp(404, {"ok": False, "error": "Not Found"})

    def _read_notify_config(self) -> dict:
        """读取通知配置，优先读本地 json，次选从板端 fskv 读取，最终回退至 config.lua"""
        default_cfg = {
            "feishu": {"enable": 0, "url": "", "secret": ""},
            "wecom": {"enable": 0, "url": ""},
            "dingtalk": {"enable": 0, "url": "", "secret": ""},
            "bark": {"enable": 0, "url": "", "group": "Air780EPV", "sound": "minuet"},
            "webhook": {"enable": 0, "url": "", "method": "POST"}
        }
        if os.path.exists(GATEWAY_CONFIG_PATH):
            try:
                with open(GATEWAY_CONFIG_PATH, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                for k, v in loaded.items():
                    if k in default_cfg and isinstance(v, dict):
                        default_cfg[k].update(v)
                return default_cfg
            except Exception as e:
                _log(f"读取 gateway_config.json 失败: {e}")

        # 次选：若本地没有配置，尝试向板端请求 get_notify_config (换机即插即读)
        try:
            board_resp = self.backend.execute_cmd("get_notify_config", {}, timeout=2.0)
            if board_resp and board_resp.get("ok"):
                board_data = board_resp.get("data", {})
                if isinstance(board_data, dict) and any(k in board_data for k in default_cfg):
                    _log("成功从板载 fskv 恢复通知配置")
                    for k, v in board_data.items():
                        if k in default_cfg and isinstance(v, dict):
                            default_cfg[k].update(v)
                    try:
                        with open(GATEWAY_CONFIG_PATH, "w", encoding="utf-8") as f:
                            json.dump(default_cfg, f, ensure_ascii=False, indent=2)
                    except Exception:
                        pass
                    return default_cfg
        except Exception as e:
            _log(f"尝试从板端读取配置异常: {e}")

        # 回退读取 config.lua
        lua_path = os.path.normpath(os.path.join(BASE_DIR, "..", "..", "deploy", "smart-gateway-780epv", "config.lua"))
        if os.path.exists(lua_path):
            try:
                with open(lua_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                for ch in ["feishu", "wecom", "dingtalk", "bark", "webhook"]:
                    m = re.search(rf"{ch}\s*=\s*\{{([^}}]+)\}}", content)
                    if m:
                        blk = m.group(1)
                        en = re.search(r"enable\s*=\s*(\d+)", blk)
                        url = re.search(r"url\s*=\s*['\"]([^'\"]*)['\"]", blk)
                        if en: default_cfg[ch]["enable"] = int(en.group(1))
                        if url: default_cfg[ch]["url"] = url.group(1)
                        if ch == "bark":
                            grp = re.search(r"group\s*=\s*['\"]([^'\"]*)['\"]", blk)
                            snd = re.search(r"sound\s*=\s*['\"]([^'\"]*)['\"]", blk)
                            if grp: default_cfg[ch]["group"] = grp.group(1)
                            if snd: default_cfg[ch]["sound"] = snd.group(1)
                try:
                    with open(GATEWAY_CONFIG_PATH, "w", encoding="utf-8") as f:
                        json.dump(default_cfg, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
            except Exception as e:
                _log(f"回退读取 config.lua 失败: {e}")
        return default_cfg

    def _save_notify_config(self, patch_cfg: dict) -> dict:
        """保存上位机通知配置到 gateway_config.json"""
        cur = self._read_notify_config()
        for k, v in patch_cfg.items():
            if k in cur and isinstance(v, dict):
                cur[k].update(v)
            elif isinstance(v, dict):
                cur[k] = v
        with open(GATEWAY_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        _log(f"已持久化更新 gateway_config.json")
        return cur

    def handle_sse_stream(self):
        """处理 Server-Sent Events 持久推流连接"""
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        # 注册监听队列
        event_queue = self.backend.register_sse_listener()
        _log(f"客户端已建立 SSE 事件订阅通道 ({self.client_address})")

        # 立即推送初始当前看板状态
        with self.backend.cache_lock:
            if self.backend.latest_status:
                initial_status = json.dumps(self.backend.latest_status, ensure_ascii=False)
                try:
                    self.wfile.write(f"event: status_update\ndata: {initial_status}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    pass

        try:
            while True:
                try:
                    item = event_queue.get(timeout=15.0)
                    evt_name = item["event"]
                    data_str = json.dumps(item["data"], ensure_ascii=False)
                    msg = f"event: {evt_name}\ndata: {data_str}\n\n"
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # 15s 发送心跳包保活，防止某些网络或反向代理超时关闭连接
                    self.wfile.write(b":keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.backend.unregister_sse_listener(event_queue)
            _log(f"客户端断开 SSE 事件通道 ({self.client_address})")


class WebServer:
    """可编程启停的 Web 控制台服务器封装"""

    def __init__(self, host=DEFAULT_WEB_HOST, port=DEFAULT_WEB_PORT, hub_host=DEFAULT_HUB_HOST, hub_port=DEFAULT_HUB_PORT):
        self.host = host
        self.port = port
        self.hub_host = hub_host
        self.hub_port = hub_port
        self.backend = None
        self.server = None
        self.running = False

    def start(self):
        self.backend = HubBackendClient(host=self.hub_host, port=self.hub_port)
        self.backend.start()

        GatewayWebHandler.backend = self.backend
        self.server = ThreadedHTTPServer((self.host, self.port), GatewayWebHandler)
        self.running = True

        _log(f"============================================================")
        _log(f"  Air780EPV 智能蜂窝通信网关 Web 控制台已启动")
        _log(f"  本地访问入口: http://127.0.0.1:{self.port}")
        _log(f"  局域网入口:   http://<宿主机/NAS局域网IP>:{self.port}")
        _log(f"  物理中枢地址: {self.hub_host}:{self.hub_port}")
        _log(f"============================================================")

        try:
            self.server.serve_forever()
        except Exception:
            pass

    def stop(self):
        self.running = False
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        if self.backend:
            try:
                self.backend.stop()
            except Exception:
                pass
        _log("Web 控制台已安全退出")


def run_web_server(host=DEFAULT_WEB_HOST, port=DEFAULT_WEB_PORT, hub_host=DEFAULT_HUB_HOST, hub_port=DEFAULT_HUB_PORT):
    ws = WebServer(host=host, port=port, hub_host=hub_host, hub_port=hub_port)
    try:
        ws.start()
    except KeyboardInterrupt:
        _log("正在停止 Web 服务器...")
    finally:
        ws.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Air780EPV 智能随身通信网关 Web 控制台")
    parser.add_argument("--host", default=DEFAULT_WEB_HOST, help=f"Web 监听地址 (默认 {DEFAULT_WEB_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help=f"Web 监听端口 (默认 {DEFAULT_WEB_PORT})")
    parser.add_argument("--hub-host", default=DEFAULT_HUB_HOST, help=f"Hub 中枢地址 (默认 {DEFAULT_HUB_HOST})")
    parser.add_argument("--hub-port", type=int, default=DEFAULT_HUB_PORT, help=f"Hub 中枢端口 (默认 {DEFAULT_HUB_PORT})")
    args = parser.parse_args()

    run_web_server(host=args.host, port=args.port, hub_host=args.hub_host, hub_port=args.hub_port)
