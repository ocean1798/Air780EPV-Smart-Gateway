import os
import sys
import time
import json
import socket
import threading
import queue
import argparse
import base64
import re
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

# 引入 luadb_packer 打包引擎
HOST_GATEWAY_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "host_gateway"))
if HOST_GATEWAY_DIR not in sys.path:
    sys.path.insert(0, HOST_GATEWAY_DIR)

try:
    import luadb_packer
except ImportError:
    luadb_packer = None

try:
    import firmware_flasher
except ImportError:
    firmware_flasher = None

from urllib.parse import urlparse, parse_qs

# 端口与地址配置
DEFAULT_WEB_HOST = "0.0.0.0"
DEFAULT_WEB_PORT = 17801
DEFAULT_HUB_HOST = "127.0.0.1"
DEFAULT_HUB_PORT = 17800

class _SafeStream:
    def write(self, msg): pass
    def flush(self): pass

if sys.stdout is None:
    sys.stdout = _SafeStream()
if sys.stderr is None:
    sys.stderr = _SafeStream()


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

def parse_semver(ver_str: Any) -> tuple:
    """提取 SemVer 版本号数字元组 (major, minor, patch)，彻底杜绝字符串字典序倒挂与前缀漏洞"""
    if not ver_str or not isinstance(ver_str, str):
        return (0, 0, 0)
    nums = re.findall(r'\d+', ver_str)
    return tuple(map(int, nums[:3])) if nums else (0, 0, 0)

# 引入 Hub 的渠道测试函数与分舱存储引擎
from gateway_hub import test_channel_push
from storage_manager import StorageManager


flashing_lock = threading.Lock()
flashing_state = {
    "is_flashing": False,
    "slot": None,
    "percent": 0,
    "status": "idle",
    "stage": "idle",
    "error": None,
    "start_time": 0
}

def update_flashing_progress(percent: int, status: str, stage: str = "flashing", error: str = None, slot: str = None):
    with flashing_lock:
        flashing_state["percent"] = percent
        flashing_state["status"] = status
        flashing_state["stage"] = stage
        flashing_state["error"] = error
        if slot:
            flashing_state["slot"] = slot
        if percent >= 100 or error:
            flashing_state["is_flashing"] = False

def _log(msg: str):
    if sys.stdout is not None:
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{ts}] [Web] {msg}", flush=True)
        except Exception:
            pass


# =========================================================================
# 核心类：HubBackendClient (与 17800 中枢通信的多卡槽客户端)
# =========================================================================

class HubBackendClient:
    """与本地 127.0.0.1:17800 多模组中枢通信的双向 TCP 客户端"""

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

        # 多卡槽集群状态
        self.slots: List[Dict[str, Any]] = []
        self.active_slot: str = "slot_1"
        self.storage_mgr = StorageManager(DATA_DIR)
        self.synced_slots: Set[str] = set() # 记录已完成脱机同步收割的卡槽
        self.latest_status: Dict[str, Any] = {}
        self.latest_status_by_slot: Dict[str, Dict[str, Any]] = {}
        self.recent_sms_events: List[Dict[str, Any]] = []
        self.recent_sms_by_slot: Dict[str, List[Dict[str, Any]]] = {}
        self.recent_calls: List[Dict[str, Any]] = []
        self.recent_calls_by_slot: Dict[str, List[Dict[str, Any]]] = {}
        self.call_status_by_slot: Dict[str, Dict[str, Any]] = {}
        self.is_hardware_connected = False
        self.cache_lock = threading.Lock()

        # SSE 广播客户端队列列表
        self.sse_listeners = []
        self.sse_lock = threading.Lock()

        # 方案 D: 读取本地内部免检 Session Token
        self.internal_session_token = ""
        self._load_session_token()

    def _load_session_token(self):
        """读取 Hub 生成在本地数据目录的内部免检令牌"""
        try:
            token_path = os.path.join(DATA_DIR, ".hub_session_token")
            if os.path.exists(token_path):
                with open(token_path, "r", encoding="utf-8") as f:
                    self.internal_session_token = f.read().strip()
        except Exception:
            pass

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
        base_dir = os.path.dirname(os.path.abspath(__file__))
        hub_path = os.path.join(base_dir, "gateway_hub.py")
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
                    _log(f"已成功连接底层多设备通信中枢: {self.host}:{self.port}")
                    # 建连成功后立即刷新读取一次 Session Token（防止 Hub 独立重启后换了新令牌）
                    self._load_session_token()
                    # 请求获取卡槽全量列表
                    time.sleep(0.1)
                    req_line = json.dumps({"type": "cmd", "cmd": "get_slots", "id": "init_slots", "token": self.internal_session_token, "source": "web"}) + "\n"
                    self.sock.sendall(req_line.encode("utf-8"))
                    return True
                except (ConnectionRefusedError, OSError):
                    if attempt == 1:
                        self._auto_spawn_hub()
                    time.sleep(0.5)
            return False

    def _trigger_offline_sync(self, slot_id: str, iccid: str):
        """触发模组脱机黑匣子短信异步拉取与 2PC 清理闭环 (审查 P1 修正)"""
        clean_iccid = str(iccid or "").strip()
        if not slot_id or not clean_iccid or clean_iccid == "sim_unknown":
            return
        with self.cache_lock:
            if slot_id in self.synced_slots:
                return
            self.synced_slots.add(slot_id)

        threading.Thread(target=self._run_offline_sync, args=(slot_id, clean_iccid), daemon=True).start()

    def _run_offline_sync(self, slot_id: str, iccid: str):
        _log(f"[{slot_id}] 检测到模组上线就绪，启动脱机黑匣子自动同步 (ICCID: {iccid})...")
        time.sleep(1.0)  # 避开开机/插卡初始通信高频期
        cursor = None
        all_fetched = []
        max_batches = 10  # 板端最多 100 条，每批 15 条，最多 7~8 批即可收割完毕
        batch_count = 0

        while batch_count < max_batches:
            batch_count += 1
            cmd_payload = {"limit": 15}
            if cursor:
                cmd_payload["cursor"] = cursor
            resp = self.execute_cmd("get_history", params=cmd_payload, slot=slot_id, timeout=4.0)
            if not resp.get("ok"):
                break
            raw_data = resp.get("data", {})
            raw_items = raw_data.get("items") or raw_data.get("list") or []
            if not raw_items:
                break
            all_fetched.extend(raw_items)
            has_more = raw_data.get("has_more")
            next_cur = raw_data.get("next_cursor")
            has_more = bool(next_cur and str(next_cur) != "0")
            if not has_more:
                break
            cursor = next_cur

        if all_fetched:
            _log(f"[{slot_id}] 成功拉取脱机短信 {len(all_fetched)} 条，增量写入本地 ICCID 分舱权威存储...")
            comp = self.storage_mgr.get_compartment(iccid)
            for it in all_fetched:
                sender = it.get("from") or it.get("sender") or it.get("phone") or "未知号码"
                content = it.get("content", "")
                raw_time = it.get("time") or it.get("ts") or ""
                ts = it.get("timestamp")
                if ts is None:
                    if isinstance(raw_time, (int, float)) and raw_time > 1000000000:
                        ts = float(raw_time)
                        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
                    elif isinstance(raw_time, str) and len(raw_time) >= 19:
                        try:
                            ts = time.mktime(time.strptime(raw_time[:19], "%Y-%m-%d %H:%M:%S"))
                            time_str = raw_time[:19]
                        except Exception:
                            ts = time.time()
                            time_str = raw_time
                    else:
                        ts = time.time()
                        time_str = time.strftime("%Y-%m-%d %H:%M:%S")
                else:
                    ts = float(ts)
                    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts > 1000000000 else str(raw_time)

                msg_id = it.get("id") or f"{slot_id}_{int(float(ts)*1000)}"
                if comp.is_deleted(msg_id, sender, content, raw_time):
                    continue

                msg_obj = {
                    "id": msg_id,
                    "slot": slot_id,
                    "iccid": iccid,
                    "phone": sender,
                    "sender": sender,
                    "content": content,
                    "otp": it.get("code") or it.get("otp") or "",
                    "time": time_str,
                    "timestamp": float(ts)
                }
                comp.append_message(msg_obj)

            # 2PC 确认清理：上位机落盘成功后，下发 clear_history 让板端 LittleFS 恢复 0 占用
            _log(f"[{slot_id}] 本地权威落盘成功，下发 clear_history 清空板端脱机暂存...")
            self.execute_cmd("clear_history", slot=slot_id, timeout=3.0)
            self.broadcast_sse("sms_received", {"slot": slot_id, "sync": True})

    def execute_cmd(self, cmd_name: str, params: dict = None, slot: Optional[str] = None, timeout: float = 8.0, wait_terminal: bool = False) -> dict:
        """向 Hub 下发指令并同步等待响应，支持定向指定目标卡槽 slot"""
        if not self._ensure_connected():
            return {"ok": False, "error": "无法连接底层通信中枢"}

        req_id = f"web_{int(time.time()*1000)}_{os.getpid()}"
        evt = threading.Event()
        req_entry = {"event": evt, "response": None, "wait_terminal": wait_terminal, "cmd": cmd_name}

        with self.pending_lock:
            self.pending_requests[req_id] = req_entry

        # 若未指定 slot，仅针对单板控制命令回退到 active_slot；对于 send_sms 等支持集群智能调度的命令保持 None
        if slot:
            target_slot = slot
        elif cmd_name in ("send_sms", "get_cluster_overview", "get_cluster_health", "get_slots", "list_dongles"):
            target_slot = None
        else:
            target_slot = self.active_slot or "slot_1"

        packet = {
            "type": "cmd",
            "id": req_id,
            "cmd": cmd_name,
            "params": params or {},
            "source": "web",
            "token": getattr(self, "internal_session_token", "")
        }
        if not packet["token"]:
            self._load_session_token()
            packet["token"] = getattr(self, "internal_session_token", "")
        if target_slot:
            packet["slot"] = target_slot

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

        # 同步等待响应
        if evt.wait(timeout=timeout):
            resp = req_entry.get("response", {})
            return resp
        else:
            with self.pending_lock:
                entry = self.pending_requests.pop(req_id, None)
            if entry and entry.get("queued"):
                return {
                    "ok": False,
                    "code": -408,
                    "msg": "UNKNOWN",
                    "error": f"短信已进入发送队列，但在 {timeout}s 内未收到基站终态回执",
                    "data": {"reason": "modem_result_timeout"}
                }
            return {"ok": False, "error": f"等待设备响应超时 ({timeout}s)"}

    def _rx_loop(self):
        buffer = ""
        while self.running:
            if not self._ensure_connected():
                time.sleep(1.0)
                continue

            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    _log("中枢套接字断开，准备重连...")
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

    def _update_status_cache(self, event_data: dict, slot: str = ""):
        """更新对应卡槽的状态缓存"""
        if not isinstance(event_data, dict):
            return

        target_slot = slot or event_data.get("slot") or self.active_slot or "slot_1"

        with self.cache_lock:
            if target_slot not in self.latest_status_by_slot:
                self.latest_status_by_slot[target_slot] = {}
            target_cache = self.latest_status_by_slot[target_slot]
            slot_info = next((s for s in self.slots if s.get("slot") == target_slot), {})
            # 计数只属于当前设备/SIM。缺少身份字段不代表换卡。
            identity_changed = any(
                event_data.get(key) and (target_cache.get(key) or slot_info.get(key))
                and event_data[key] != (target_cache.get(key) or slot_info.get(key))
                for key in ("imei", "iccid")
            )
            offline = event_data.get("online") is False
            if identity_changed or offline:
                target_cache.pop("sms_count", None)

            if "rndis" in event_data:
                target_cache["rndis"] = bool(event_data["rndis"])
                target_cache["rndis_enable"] = bool(event_data["rndis"])
            elif "rndis_enable" in event_data:
                target_cache["rndis"] = bool(event_data["rndis_enable"])
                target_cache["rndis_enable"] = bool(event_data["rndis_enable"])

            if "cellular_data" in event_data:
                target_cache["cellular_data"] = bool(event_data["cellular_data"])
                target_cache["cellular_data_enable"] = bool(event_data["cellular_data"])
            elif "cellular_data_enable" in event_data:
                target_cache["cellular_data"] = bool(event_data["cellular_data_enable"])
                target_cache["cellular_data_enable"] = bool(event_data["cellular_data_enable"])

            for k in ("model", "bsp", "imei", "iccid", "csq", "rsrp", "temp", "vbat", "uptime", "lua_mem_kb", "version", "capabilities", "port", "online"):
                if k in event_data and (k not in ("imei", "iccid") or event_data[k]):
                    target_cache[k] = event_data[k]
            # 增量缺字段保留同设备最近值；显式 0 是有效设备值。
            if not offline:
                if "blackbox_count" in event_data:
                    target_cache["sms_count"] = event_data["blackbox_count"]
                elif "sms_count" in event_data:
                    target_cache["sms_count"] = event_data["sms_count"]
            if "current_version" in event_data:
                target_cache["version"] = event_data["current_version"]
            if "uptime_seconds" in event_data:
                target_cache["uptime"] = event_data["uptime_seconds"]

            if not target_cache.get("port"):
                slot_info = next((s for s in self.slots if s.get("slot") == target_slot), None)
                if slot_info and slot_info.get("port"):
                    target_cache["port"] = slot_info["port"]

            phone_val = event_data.get("phone") or event_data.get("number")
            if phone_val and str(phone_val).strip():
                target_cache["phone"] = str(phone_val).strip()
                target_cache["number"] = str(phone_val).strip()
            elif not target_cache.get("phone"):
                slot_info = next((s for s in self.slots if s.get("slot") == target_slot), None)
                if slot_info and slot_info.get("phone"):
                    target_cache["phone"] = slot_info["phone"]
                    target_cache["number"] = slot_info["phone"]

            target_cache["slot"] = target_slot

            # 如果当前活跃卡槽与 target_slot 一致，同步更新缺省缓存
            if target_slot == self.active_slot:
                self.latest_status.pop("sms_count", None)
                self.latest_status.update(target_cache)

    def _update_slots_cache(self, slots_data: list):
        """Hub 卡槽摘要中的设备数也同步到后续 SSE 使用的状态缓存。"""
        for slot_info in slots_data:
            if slot_info.get("slot"):
                self._update_status_cache(slot_info, slot=slot_info["slot"])
        with self.cache_lock:
            self.slots = slots_data

    def _dispatch_frame(self, raw_line: str):
        try:
            data = json.loads(raw_line)
        except Exception:
            return

        frame_type = data.get("type")
        frame_slot = data.get("slot") or "slot_1"

        # 1. 响应帧
        if frame_type in ("res", "response"):
            inner_data = data.get("data")
            req_id = data.get("id") or (inner_data.get("id") if isinstance(inner_data, dict) else None)
            if req_id:
                with self.pending_lock:
                    entry = self.pending_requests.get(req_id)
                    if entry:
                        msg = data.get("msg") or (inner_data.get("msg", "") if isinstance(inner_data, dict) else "")
                        code = data.get("code") if "code" in data else (inner_data.get("code", 0) if isinstance(inner_data, dict) else 0)
                        if entry.get("wait_terminal") and code == 0 and msg == "QUEUED":
                            entry["queued"] = True
                            entry["intermediate"] = inner_data if isinstance(inner_data, dict) else data
                            self.broadcast_sse("sms_status", {
                                "id": req_id,
                                "slot": frame_slot,
                                "state": "QUEUED",
                                "data": inner_data if isinstance(inner_data, dict) else {}
                            })
                            return

                        self.pending_requests.pop(req_id, None)
                        res_obj = dict(data)
                        if "ok" not in res_obj:
                            res_obj["ok"] = (code == 0)
                        if isinstance(inner_data, dict):
                            for k, v in inner_data.items():
                                if k not in res_obj:
                                    res_obj[k] = v
                        entry["response"] = res_obj
                        entry["event"].set()

            # 处理 get_slots 响应
            if req_id == "init_slots" and data.get("ok"):
                slots_data = data.get("data", {}).get("slots", [])
                self._update_slots_cache(slots_data)
                with self.cache_lock:
                    if self.slots and not any(s["slot"] == self.active_slot for s in self.slots):
                        self.active_slot = self.slots[0]["slot"]
                self.broadcast_sse("cluster_update", {"slots": self.slots, "active_slot": self.active_slot})

            if data.get("ok"):
                self.is_hardware_connected = True

        # 2. 事件广播帧
        elif frame_type == "event":
            event_name = data.get("event")
            event_data = data.get("data", {})
            evt_slot = data.get("slot") or event_data.get("slot") or frame_slot

            if event_name in ("cluster_status", "dongle_connected", "dongle_disconnected"):
                # 会话池集群状态变动
                if event_name == "cluster_status":
                    self._update_slots_cache(event_data.get("slots", []))
                    # 尝试触发所有在线卡槽的脱机同步
                    for s in self.slots:
                        if s.get("online") and s.get("iccid"):
                            self._trigger_offline_sync(s.get("slot"), s.get("iccid"))
                elif event_name == "dongle_connected":
                    # 增量添加或更新
                    slot_id = event_data.get("slot")
                    self._update_status_cache(event_data, slot=slot_id)
                    existing = [s for s in self.slots if s.get("slot") == slot_id]
                    if existing:
                        existing[0].update(event_data)
                    else:
                        self.slots.append(event_data)
                    if event_data.get("iccid"):
                        self._trigger_offline_sync(slot_id, event_data.get("iccid"))
                elif event_name == "dongle_disconnected":
                    slot_id = event_data.get("slot")
                    self._update_status_cache({"online": False}, slot=slot_id)
                    for s in self.slots:
                        if s.get("slot") == slot_id:
                            s["online"] = False
                    with self.cache_lock:
                        self.synced_slots.discard(slot_id)

                with self.cache_lock:
                    if self.slots and not any(s.get("slot") == self.active_slot and s.get("online") for s in self.slots):
                        online_slots = [s for s in self.slots if s.get("online")]
                        if online_slots:
                            self.active_slot = online_slots[0]["slot"]

                self.broadcast_sse("cluster_update", {"slots": self.slots, "active_slot": self.active_slot})

            elif event_name in ("device_connected", "dongle_connected"):
                self._update_status_cache(event_data, slot=evt_slot)
                self.is_hardware_connected = True
                if event_data.get("iccid"):
                    self._trigger_offline_sync(evt_slot, event_data.get("iccid"))
                self.broadcast_sse("device_connected", {"online": True, "slot": evt_slot})

            elif event_name in ("device_disconnected", "dongle_disconnected"):
                self._update_status_cache({"online": False}, slot=evt_slot)
                with self.cache_lock:
                    self.synced_slots.discard(evt_slot)
                self.broadcast_sse("device_disconnected", {"online": False, "slot": evt_slot})

            elif event_name in ("status", "gateway_ready", "state_change"):
                self.is_hardware_connected = True
                self._update_status_cache(event_data, slot=evt_slot)
                if event_data.get("iccid"):
                    self._trigger_offline_sync(evt_slot, event_data.get("iccid"))
                with self.cache_lock:
                    status_snapshot = dict(self.latest_status_by_slot.get(evt_slot, {}))
                    s_info = next((s for s in self.slots if s.get("slot") == evt_slot), None)
                    if s_info:
                        s_info = dict(s_info)
                status_snapshot["online"] = True
                status_snapshot["slot"] = evt_slot
                # 预先合并该卡槽的静态元数据 (imei, iccid, model, version, phone, port)，杜绝缺失字段推流导致前端闪烁
                if s_info:
                    for field in ("imei", "iccid", "model", "bsp", "version", "port"):
                        if not status_snapshot.get(field) and s_info.get(field):
                            status_snapshot[field] = s_info[field]
                    if not status_snapshot.get("phone") and s_info.get("phone"):
                        status_snapshot["phone"] = s_info["phone"]
                        status_snapshot["number"] = s_info["phone"]
                self.broadcast_sse("status_update", status_snapshot)

            elif event_name in ("sms_rx", "sms_received"):
                self.is_hardware_connected = True
                raw_time = event_data.get("time") or event_data.get("ts")
                if isinstance(raw_time, (int, float)) and raw_time > 1000000000:
                    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(raw_time))
                elif raw_time:
                    time_str = str(raw_time)
                else:
                    time_str = time.strftime("%Y-%m-%d %H:%M:%S")

                sender_phone = event_data.get("from") or event_data.get("phone") or "未知号码"
                with self.cache_lock:
                    s_meta = next((s for s in self.slots if s.get("slot") == evt_slot), {})
                    slot_phone = s_meta.get("phone") or s_meta.get("number")
                    slot_model = s_meta.get("model") or s_meta.get("bsp")
                    slot_iccid = s_meta.get("iccid") or self.latest_status_by_slot.get(evt_slot, {}).get("iccid") or "sim_unknown"

                from storage_manager import derive_operator_and_badge
                badge_info = derive_operator_and_badge(
                    iccid=slot_iccid,
                    my_phone=slot_phone,
                    sender=sender_phone,
                    content=event_data.get("content") or "",
                    model=slot_model,
                    slot=evt_slot
                )

                item = {
                    "slot": evt_slot,
                    "phone": sender_phone,
                    "content": event_data.get("content") or "",
                    "otp": event_data.get("code") or event_data.get("otp"),
                    "time": time_str,
                    "operator": badge_info["operator"],
                    "display_badge": badge_info["display_badge"],
                    "slot_display": badge_info["display_badge"],
                    "slot_label": badge_info["slot_label"]
                }
                # 审查建议 P1/P2: 实时短信立即落盘至本地 ICCID 权威存储分舱
                try:
                    active_iccid = slot_iccid
                    comp = self.storage_mgr.get_compartment(active_iccid)
                    storage_item = dict(item)
                    storage_item["id"] = event_data.get("id") or event_data.get("msg_id") or f"{evt_slot}_{int(time.time()*1000)}"
                    storage_item["timestamp"] = time.time()
                    storage_item["iccid"] = active_iccid
                    comp.append_message(storage_item)
                except Exception as e:
                    _log(f"实时短信落盘异常: {e}")

                with self.cache_lock:
                    if evt_slot not in self.recent_sms_by_slot:
                        self.recent_sms_by_slot[evt_slot] = []
                    self.recent_sms_by_slot[evt_slot].insert(0, item)
                    if len(self.recent_sms_by_slot[evt_slot]) > 100:
                        self.recent_sms_by_slot[evt_slot].pop()

                    self.recent_sms_events.insert(0, item)
                    if len(self.recent_sms_events) > 150:
                        self.recent_sms_events.pop()

                self.broadcast_sse("sms_received", item)

            elif event_name in ("call_rx", "call_incoming"):
                raw_time = event_data.get("time") or event_data.get("ts")
                if isinstance(raw_time, (int, float)) and raw_time > 1000000000:
                    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(raw_time))
                elif raw_time:
                    time_str = str(raw_time)
                else:
                    time_str = time.strftime("%Y-%m-%d %H:%M:%S")

                item = {
                    "slot": evt_slot,
                    "phone": event_data.get("from") or event_data.get("phone") or "未知号码",
                    "time": time_str,
                    "action": event_data.get("action") or "rejected"
                }
                with self.cache_lock:
                    if evt_slot not in self.recent_calls_by_slot:
                        self.recent_calls_by_slot[evt_slot] = []
                    self.recent_calls_by_slot[evt_slot].insert(0, item)
                    if len(self.recent_calls_by_slot[evt_slot]) > 50:
                        self.recent_calls_by_slot[evt_slot].pop()

                    self.recent_calls.insert(0, item)
                    if len(self.recent_calls) > 100:
                        self.recent_calls.pop()

                self.broadcast_sse("call_incoming", item)

            elif event_name == "call_status":
                evt_slot = event_data.get("slot") or "slot_2"
                with self.cache_lock:
                    self.call_status_by_slot[evt_slot] = {
                        "status": event_data.get("status", "IDLE"),
                        "phone": event_data.get("phone", ""),
                        "message": event_data.get("message", ""),
                        "time": time.time()
                    }
                self.broadcast_sse("call_status", {"slot": evt_slot, "data": event_data})

            elif event_name == "fota_status":
                event_data["slot"] = evt_slot
                self.broadcast_sse("fota_status", event_data)

    def register_sse_listener(self) -> queue.Queue:
        q = queue.Queue(maxsize=128)
        with self.sse_lock:
            self.sse_listeners.append(q)
        return q

    def unregister_sse_listener(self, q: queue.Queue):
        with self.sse_lock:
            if q in self.sse_listeners:
                self.sse_listeners.remove(q)

    def perform_serial_ota(self, slot: str = "slot_1", progress_cb=None) -> dict:
        """执行多模组集群定向卡槽串口分块流式热更新 (Serial SOTA)"""
        if luadb_packer is None:
            return {"ok": False, "error": "luadb_packer 模块未加载，无法执行打包"}

        try:
            if progress_cb:
                progress_cb(5, "正在执行 Lua 静态语法预检与标准打包...", "packing")

            manifest = luadb_packer.get_version_manifest()
            target_ver = manifest.get("version", "1.2.7")

            # 自动探测芯片架构 (支持 EC718PV 与 EC618 平台)
            curr_slot_info = {}
            with self.cache_lock:
                for s in self.slots:
                    if s.get("slot") == slot:
                        curr_slot_info = s
                        break
            mod = (curr_slot_info.get("model") or curr_slot_info.get("bsp") or "").upper()
            chip_type = "ec618" if ("780E" in mod and "EPV" not in mod) or "618" in mod or "700E" in mod else "ec718"

            raw_luadb = luadb_packer.pack_luadb(target_version=target_ver)
            sota_bytes, meta = luadb_packer.pack_sota_package(raw_luadb, target_version=target_ver, chip_type=chip_type)

            total_len = len(sota_bytes)
            chunk_size = 2048
            chunks = [sota_bytes[i:i + chunk_size] for i in range(0, total_len, chunk_size)]
            total_chunks = len(chunks)
            sota_md5 = meta["package_md5"]

            if progress_cb:
                progress_cb(15, f"开始向卡槽 [{slot}] 启动 OTA 协商 (共 {total_chunks} 块, {total_len} 字节)...", "starting")

            start_resp = self.execute_cmd("ota_start", {
                "size": total_len,
                "md5": sota_md5,
                "total_chunks": total_chunks,
                "chunk_size": chunk_size
            }, slot=slot, timeout=8.0)

            if not start_resp.get("ok"):
                err = start_resp.get("msg") or start_resp.get("error") or "模组响应超时"
                return {"ok": False, "error": f"模组拒绝启动 OTA: {err}"}

            for idx, chunk in enumerate(chunks):
                b64_str = base64.b64encode(chunk).decode("ascii")
                pct = 15 + int((idx + 1) / total_chunks * 70)
                if progress_cb:
                    progress_cb(pct, f"正在灌流传输分块 [{idx + 1}/{total_chunks}]...", "flashing")

                chunk_resp = self.execute_cmd("ota_chunk", {
                    "index": idx,
                    "data": b64_str
                }, slot=slot, timeout=6.0)

                if not chunk_resp.get("ok"):
                    self.execute_cmd("ota_abort", {}, slot=slot, timeout=2.0)
                    return {"ok": False, "error": f"分块 [{idx + 1}/{total_chunks}] 传输失败: {chunk_resp.get('msg')}"}

            if progress_cb:
                progress_cb(88, "分块传输完成，模组正在烧录 Flash 并校验 MD5...", "burning")

            finish_resp = self.execute_cmd("ota_finish", {
                "md5": sota_md5
            }, slot=slot, timeout=20.0)

            if not finish_resp.get("ok"):
                return {"ok": False, "error": f"模组固件烧录失败: {finish_resp.get('msg') or finish_resp.get('error')}"}

            if progress_cb:
                progress_cb(92, "固件烧录完成！模组正在软重启与置换固件...", "rebooting")

            # 缓冲等待模组重启（1.5s 后触发 rtos.reboot，USB 重举约 2~4 秒）
            t_start = time.time()
            reboot_detected = False
            while time.time() - t_start < 15.0:
                time.sleep(1.0)
                status_resp = self.execute_cmd("get_status", {}, slot=slot, timeout=2.0)
                if status_resp.get("ok") and status_resp.get("data"):
                    d = status_resp["data"]
                    curr_v = d.get("version") or d.get("firmware_version")
                    if curr_v == target_ver:
                        reboot_detected = True
                        break

            if not reboot_detected:
                err_msg = f"模组升级重启超时 (15s)，未检测到新固件版本生效"
                if progress_cb:
                    progress_cb(95, err_msg, "failed")
                return {"ok": False, "error": err_msg}

            if progress_cb:
                progress_cb(100, f"模组已成功平滑升级至 v{target_ver}！", "success")
            return {"ok": True, "target_version": target_ver, "msg": f"热更新完成，模组已重启并上线 v{target_ver}"}
        except Exception as e:
            return {"ok": False, "error": f"热更新异常: {str(e)}"}

    def broadcast_sse(self, event_name: str, payload: dict):
        with self.sse_lock:
            listeners = list(self.sse_listeners)
        for q in listeners:
            try:
                q.put_nowait({"event": event_name, "data": payload})
            except queue.Full:
                pass


# =========================================================================
# Web 服务器：HTTP Handler 与线程池
# =========================================================================

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class GatewayWebHandler(BaseHTTPRequestHandler):
    server_version = "Air780ClusterWeb/2.0"

    def log_message(self, format, *args):
        """覆盖 BaseHTTPRequestHandler 的默认 stderr 输出，防止 windowed 模式下无控制台报错"""
        if sys.stderr is not None and not isinstance(sys.stderr, _SafeStream):
            try:
                sys.stderr.write("%s - - [%s] %s\n" %
                                 (self.address_string(),
                                  self.log_date_time_string(),
                                  format % args))
                sys.stderr.flush()
            except Exception:
                pass

    @property
    def backend(self) -> HubBackendClient:
        return self.server.backend

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send_json_resp(self, status_code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        try:
            self._handle_get()
        except Exception as e:
            import traceback
            trace_str = traceback.format_exc()
            _log(f"HTTP GET Error: {e}\n{trace_str}")
            try:
                self._send_json_resp(500, {"ok": False, "error": str(e), "trace": trace_str})
            except Exception:
                pass

    def _handle_get(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        target_slot = query.get("slot", [None])[0] or self.backend.active_slot or "slot_1"

        # 1. 网页静态页面
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
                fallback_html = "<html><body><h1>Air780 智能通信网关</h1><p>Web 资源未找到</p></body></html>".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(fallback_html)))
                self.end_headers()
                self.wfile.write(fallback_html)
                return

        # 2. SSE 实时事件推送流接口
        if path == "/api/events":
            self.handle_sse_stream()
            return

        # 2.9 未分配/全新模组嗅探接口 (AIR-35)
        if path == "/api/flasher/unassigned":
            resp = self.backend.execute_cmd("get_unassigned_dongles", timeout=2.0)
            if resp.get("ok"):
                unassigned = resp.get("data", {}).get("unassigned", [])
                self._send_json_resp(200, {"ok": True, "unassigned": unassigned, "count": len(unassigned)})
            else:
                self._send_json_resp(200, {"ok": True, "unassigned": [], "count": 0})
            return

        # 3. 集群卡槽列表接口
        if path == "/api/slots":
            # 向中枢同步刷新一次最新卡槽
            resp = self.backend.execute_cmd("get_slots", timeout=2.0)
            if resp.get("ok"):
                slots_data = resp.get("data", {}).get("slots", [])
                self.backend._update_slots_cache(slots_data)
            with self.backend.cache_lock:
                slots_list = list(self.backend.slots)
                act_slot = self.backend.active_slot
            self._send_json_resp(200, {
                "ok": True,
                "slots": slots_list,
                "active_slot": act_slot,
                "count": len(slots_list)
            })
            return

        # 3.1 全集群全景驾驶舱接口 (AIR-22)
        if path == "/api/cluster/overview":
            resp = self.backend.execute_cmd("get_cluster_overview", timeout=3.0)
            if resp.get("ok"):
                self._send_json_resp(200, {"ok": True, "data": resp.get("data")})
            else:
                self._send_json_resp(500, {"ok": False, "error": resp.get("error") or "获取集群概览失败"})
            return

        # 3.2 全集群健康监控状态机接口 (AIR-22)
        if path == "/api/cluster/health":
            resp = self.backend.execute_cmd("get_cluster_health", timeout=3.0)
            if resp.get("ok"):
                self._send_json_resp(200, {"ok": True, "data": resp.get("data")})
            else:
                self._send_json_resp(500, {"ok": False, "error": resp.get("error") or "获取集群健康数据失败"})
            return

        # 3.3 全集群防 OOM 复合游标聚合收件箱 (AIR-22)
        if path == "/api/cluster/messages":
            limit_val = 15
            if "limit" in query:
                try:
                    limit_val = max(1, min(50, int(query["limit"][0])))
                except Exception:
                    pass
            cursor_val = query.get("cursor", [None])[0]
            slot_filter = query.get("slot", ["all"])[0]

            slots_meta_map = {}
            with self.backend.cache_lock:
                for s in self.backend.slots:
                    s_id = s.get("slot")
                    if s_id:
                        slots_meta_map[s_id] = s

            res = self.backend.storage_mgr.get_aggregated_messages(
                limit=limit_val,
                cursor=cursor_val,
                slot_filter=slot_filter,
                slots_meta=slots_meta_map
            )
            self._send_json_resp(200, {"ok": True, "data": res})
            return
            return

        # 4. 获取实时全局状态看板 (支持按 slot 路由)
        if path == "/api/status":
            resp = self.backend.execute_cmd("get_status", slot=target_slot, timeout=2.5)
            if resp.get("ok"):
                self.backend.is_hardware_connected = True
                raw_data = resp.get("data", {})
                rndis_val = raw_data.get("rndis") if "rndis" in raw_data else raw_data.get("rndis_enable", False)
                data_val = raw_data.get("cellular_data") if "cellular_data" in raw_data else raw_data.get("cellular_data_enable", False)
                sms_count_val = raw_data.get("blackbox_count") if "blackbox_count" in raw_data else raw_data.get("sms_count", 0)
                iccid_val = raw_data.get("iccid", "")
                if not iccid_val:
                    with self.backend.cache_lock:
                        for s in self.backend.slots:
                            if s.get("slot") == target_slot and s.get("iccid"):
                                iccid_val = s["iccid"]
                                break
                norm_status = {
                    "online": True,
                    "slot": target_slot,
                    "model": raw_data.get("bsp") or raw_data.get("model") or "Air780 Series",
                    "imei": raw_data.get("imei", ""),
                    "iccid": iccid_val,
                    "version": raw_data.get("version") or raw_data.get("current_version") or "1.2.0",
                    "csq": raw_data.get("csq", 0),
                    "rsrp": raw_data.get("rsrp", 0),
                    "temp": raw_data.get("temp", 0),
                    "vbat": raw_data.get("vbat", 0),
                    "rndis": bool(rndis_val),
                    "rndis_enable": bool(rndis_val),
                    "cellular_data": bool(data_val),
                    "cellular_data_enable": bool(data_val),
                    "sms_count": sms_count_val,
                    "uptime": raw_data.get("uptime_seconds") if "uptime_seconds" in raw_data else raw_data.get("uptime", 0),
                    "lua_mem_kb": raw_data.get("lua_mem_kb", 0),
                    "capabilities": raw_data.get("capabilities", {}),
                    "raw": raw_data
                }
                self.backend._update_status_cache(norm_status, slot=target_slot)
                self._send_json_resp(200, {"ok": True, "online": True, "slot": target_slot, "data": norm_status})
            else:
                err_msg = resp.get("error") or resp.get("msg") or "模组未响应，物理设备已拔出"
                self.backend._update_status_cache({"online": False}, slot=target_slot)
                with self.backend.cache_lock:
                    cached = dict(self.backend.latest_status_by_slot.get(target_slot, {}))
                cached["online"] = False
                cached["slot"] = target_slot
                self._send_json_resp(200, {"ok": False, "online": False, "slot": target_slot, "error": err_msg, "data": cached})
            return

        # 5. 获取短信历史记录 (本地权威存储 + 服务端全文检索 + 复合游标懒加载)
        if path == "/api/history":
            limit_val = 40
            if "limit" in query:
                try:
                    limit_val = max(1, min(100, int(query["limit"][0])))
                except Exception:
                    pass
            cursor_val = query.get("cursor", [None])[0]
            kw = query.get("keyword", [None])[0]
            order = query.get("order", ["desc"])[0].lower()
            requested_slot = query.get("slot", [None])[0]

            slots_meta_map = {}
            with self.backend.cache_lock:
                for s in self.backend.slots:
                    s_id = s.get("slot")
                    if s_id:
                        slots_meta_map[s_id] = s

            res = self.backend.storage_mgr.get_aggregated_messages(
                limit=limit_val,
                cursor=cursor_val,
                slot_filter=requested_slot,
                keyword=kw,
                order=order,
                slots_meta=slots_meta_map
            )

            self._send_json_resp(200, {
                "ok": True,
                "slot": requested_slot or "all",
                "items": res["items"],
                "list": res["items"],
                "total": res["total"],
                "count": res["count"],
                "next_cursor": res["next_cursor"],
                "has_more": res["has_more"],
                "data": res
            })
            return

        # 6. 获取来电拦截记录 (支持按 slot 路由)
        if path == "/api/calls":
            with self.backend.cache_lock:
                calls = list(self.backend.recent_calls_by_slot.get(target_slot, []))
            self._send_json_resp(200, {"ok": True, "slot": target_slot, "items": calls, "count": len(calls)})
            return

        # 6.1 获取通话状态 (AIR-30)
        if path == "/api/call/status":
            with self.backend.cache_lock:
                status_info = getattr(self.backend, "call_status_by_slot", {}).get(target_slot, {"status": "IDLE"})
            self._send_json_resp(200, {"ok": True, "slot": target_slot, "data": status_info})
            return

        # 7. 获取网关通用配置 (含通知与 MCP 开关)
        if path in ("/api/config", "/api/config/notify"):
            if os.path.exists(GATEWAY_CONFIG_PATH):
                try:
                    with open(GATEWAY_CONFIG_PATH, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    self._send_json_resp(200, {"ok": True, "config": cfg, "data": cfg})
                    return
                except Exception as e:
                    self._send_json_resp(500, {"ok": False, "error": f"读取配置失败: {e}"})
                    return
            else:
                self._send_json_resp(200, {"ok": True, "config": {}, "data": {}})
            return

        # 8. 获取固件版本与待更元数据信息 (AIR-29 / AIR-31 门禁加固)
        if path == "/api/control/upgrade_info":
            manifest = luadb_packer.get_version_manifest() if luadb_packer else {"version": "1.2.9", "changelog": "优化弱信号重连稳定性与长短信防重发", "size": 22400}
            curr_slot_info = {}
            with self.backend.cache_lock:
                for s in self.backend.slots:
                    if s.get("slot") == target_slot:
                        curr_slot_info = s
                        break
            if not curr_slot_info:
                resp = self.backend.execute_cmd("get_slots", timeout=1.5)
                if resp.get("ok"):
                    slots_data = resp.get("data", {}).get("slots", [])
                    with self.backend.cache_lock:
                        self.backend.slots = slots_data
                    for s in slots_data:
                        if s.get("slot") == target_slot:
                            curr_slot_info = s
                            break
            current_ver = curr_slot_info.get("version") or ""
            target_ver = manifest.get("version", "1.2.9")
            model = curr_slot_info.get("model") or "Air780"
            bsp = curr_slot_info.get("bsp") or model

            # SemVer 严密数值判定：固件必须 >= 1.2.6 且支持串口 SOTA 协议栈
            cur_tuple = parse_semver(current_ver)
            target_tuple = parse_semver(target_ver)
            sota_min_tuple = (1, 2, 6)

            # 芯片架构匹配：当前 SOTA 包由 deploy/smart-gateway-780epv 生成，针对 EC718PV 架构
            is_epv = "EPV" in model.upper() or "EC718" in bsp.upper() or "EPV" in bsp.upper()

            if cur_tuple == (0, 0, 0) or not current_ver:
                sota_supported = False
                has_update = False
                upgrade_method = "无法热更 (未检测到固件版本)"
                tip = "未读取到模组固件版本，请确认设备是否正常在线"
            elif cur_tuple >= target_tuple:
                # 已是最新固件（或更高版本），无论什么芯片架构，绝不谎报 has_update
                has_update = False
                if is_epv:
                    sota_supported = True
                    upgrade_method = "本地串口极速热更 (已是最新固件)"
                    tip = ""
                else:
                    sota_supported = False
                    upgrade_method = f"已是最新版本 ({model} 专属固件)"
                    tip = f"当前模组为 {model} (EC618 纯数传平台)，已运行最新专属固件 v{current_ver}。如需重装请使用【重新刷机控制台】。"
            elif cur_tuple < sota_min_tuple:
                sota_supported = False
                has_update = True
                upgrade_method = "物理线刷 (旧版本固件需首次线刷)"
                tip = f"当前固件 (v{current_ver}) 较早，尚未内置串口极速热更桩。请使用【重新刷机控制台】升级至最新版。"
            elif not is_epv and "780E" in model.upper():
                sota_supported = False
                has_update = True
                upgrade_method = "物理线刷 (芯片平台专属镜像)"
                tip = f"检测到新版本 v{target_ver}。当前模组为 {model} (EC618 纯数传平台)，与 EPV 在线热更镜像互斥，请使用【重新刷机控制台】线刷更新。"
            else:
                sota_supported = True
                has_update = True
                upgrade_method = "本地串口极速热更 (0流量·保留所有短信)"
                tip = ""

            self._send_json_resp(200, {
                "ok": True,
                "slot": target_slot,
                "model": model,
                "current_version": current_ver or "未知",
                "target_version": target_ver,
                "has_update": has_update,
                "sota_supported": sota_supported,
                "tip": tip,
                "changelog": manifest.get("changelog", "常规稳定性优化"),
                "size_kb": round(manifest.get("size", 22400) / 1024, 1),
                "upgrade_method": upgrade_method
            })
            return

        self._send_json_resp(404, {"ok": False, "error": "接口不存在"})

    @staticmethod
    def _format_error_message(resp: dict) -> str:
        if not isinstance(resp, dict):
            return "未知错误"
        code = resp.get("code")
        msg = str(resp.get("msg", "")).strip()
        data_sec = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        reason = data_sec.get("reason", "")

        if code == -409 or msg == "SMS_RESULT_UNKNOWN" or reason == "previous_modem_result_pending":
            return "上一条短信发送结果未决，设备已处于保护状态，请稍后重试或重置状态"
        if code == -429 or msg == "QUEUE_FULL":
            return "短信发送队列已满，请等待前序短信处理完成"
        if code == -101 or msg == "PARAM_ERR":
            return "短信参数错误：手机号码或短信内容不能为空"
        if code == -102 or msg == "SEND_FAILED":
            return "模组底层射频发射失败"
        if code == -408 or msg == "UNKNOWN" or reason == "modem_result_timeout":
            return "等待模组发送结果超时，结果未知"
        if code == -1 or msg == "SENT_FAILED":
            return "基站发送失败"

        return resp.get("error") or msg or reason or f"请求失败 (错误码: {code})"

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        content_len = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_len > 0:
            raw_body = self.rfile.read(content_len)
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except Exception:
                pass

        qs_slot = query.get("slot", [None])[0]
        target_slot = qs_slot or body.get("slot") or self.backend.active_slot or "slot_1"

        # 1. 切换前端当前活跃卡槽
        if path == "/api/slots/switch":
            new_slot = body.get("slot")
            if not new_slot:
                self._send_json_resp(400, {"ok": False, "error": "必须提供目标 slot"})
                return
            with self.backend.cache_lock:
                self.backend.active_slot = new_slot
            _log(f"用户已切换当前活跃卡槽 -> 【{new_slot}】")
            self.backend.broadcast_sse("cluster_update", {"slots": self.backend.slots, "active_slot": new_slot})
            self._send_json_resp(200, {"ok": True, "active_slot": new_slot})
            return

        # 2. 发送短信 (支持定向卡槽与集群智能路由分流 AIR-22)
        if path == "/api/sms/send":
            phone = (body.get("phone") or "").strip()
            content = (body.get("content") or "").strip()
            strategy = (body.get("strategy") or "operator_affinity").strip()
            dry_run = bool(body.get("dry_run", False))
            if not phone or not content:
                self._send_json_resp(400, {"ok": False, "error": "手机号与正文不能为空"})
                return

            # 区分 direct 与 auto 契约：显式传 slot 则为 direct；未传或为 auto 则为 None
            specified_slot = body.get("slot")
            if specified_slot in ("", "auto", None):
                target_slot = None
            else:
                target_slot = str(specified_slot).strip()

            wait_term = body.get("wait_terminal", False)
            resp = self.backend.execute_cmd(
                "send_sms",
                params={"phone": phone, "content": content, "strategy": strategy, "slot": target_slot, "dry_run": dry_run},
                slot=target_slot,
                timeout=12.0,
                wait_terminal=wait_term and not dry_run
            )
            if resp.get("ok"):
                used_slot = resp.get("slot") or target_slot or "slot_1"
                self._send_json_resp(200, {
                    "ok": True,
                    "slot": used_slot,
                    "status": "routed" if dry_run else ("sent" if resp.get("msg") in ("SENT_OK", "SENT") else "queued"),
                    "strategy": resp.get("routed_strategy") or strategy,
                    "fallback": resp.get("fallback_used", False),
                    "panic_mode": resp.get("panic_mode", False),
                    "msg": resp.get("msg", "SENT_OK"),
                    "data": resp.get("data")
                })
            else:
                err_msg = self._format_error_message(resp)
                self._send_json_resp(500, {"ok": False, "slot": target_slot, "error": err_msg, "code": resp.get("code")})
            return

        # 2.1 重置短信队列与发送保护状态
        if path == "/api/sms/reset":
            resp = self.backend.execute_cmd("reset_sms", params={}, slot=target_slot, timeout=5.0)
            if resp.get("ok"):
                self._send_json_resp(200, {"ok": True, "slot": target_slot, "msg": "短信发送状态已重置", "data": resp.get("data")})
            else:
                err_msg = self._format_error_message(resp)
                self._send_json_resp(500, {"ok": False, "slot": target_slot, "error": err_msg, "code": resp.get("code")})
            return

        # 3. 控制 RNDIS 开关 (支持定向卡槽)
        if path == "/api/control/rndis":
            enable = bool(body.get("enable", False))
            resp = self.backend.execute_cmd("set_rndis", params={"enable": enable}, slot=target_slot, timeout=6.0)
            if resp.get("ok"):
                with self.backend.cache_lock:
                    if target_slot not in self.backend.latest_status_by_slot:
                        self.backend.latest_status_by_slot[target_slot] = {}
                    self.backend.latest_status_by_slot[target_slot]["rndis"] = enable
                    self.backend.latest_status_by_slot[target_slot]["rndis_enable"] = enable
                self._send_json_resp(200, {"ok": True, "slot": target_slot, "rndis": enable, "msg": "RNDIS 配置已更新"})
            else:
                self._send_json_resp(200, {"ok": False, "slot": target_slot, "error": resp.get("error") or "RNDIS 切换失败"})
            return

        # 4. 控制板载蜂窝数据开关 (支持定向卡槽)
        if path == "/api/control/data":
            enable = bool(body.get("enable", False))
            resp = self.backend.execute_cmd("set_cellular_data", params={"enable": enable}, slot=target_slot, timeout=6.0)
            if resp.get("ok"):
                with self.backend.cache_lock:
                    if target_slot not in self.backend.latest_status_by_slot:
                        self.backend.latest_status_by_slot[target_slot] = {}
                    self.backend.latest_status_by_slot[target_slot]["cellular_data"] = enable
                    self.backend.latest_status_by_slot[target_slot]["cellular_data_enable"] = enable
                self._send_json_resp(200, {"ok": True, "slot": target_slot, "cellular_data": enable, "msg": "蜂窝数据配置已更新"})
            else:
                self._send_json_resp(200, {"ok": False, "slot": target_slot, "error": resp.get("error") or "蜂窝数据切换失败"})
            return

        # 5. 软重启模组 (支持定向卡槽)
        if path == "/api/control/reboot":
            reason = body.get("reason", "web_console_action")
            resp = self.backend.execute_cmd("reboot", params={"reason": reason}, slot=target_slot, timeout=3.0)
            self._send_json_resp(200, {"ok": True, "slot": target_slot, "msg": "重启指令已成功下发至网关"})
            return

        # 5.1 发起 VoLTE 电话拨号呼叫 (AIR-30)
        if path == "/api/call/dial":
            phone = (body.get("phone") or body.get("number") or "").strip()
            timeout_sec = int(body.get("timeout") or body.get("timeout_seconds") or 15)
            hangup_on_ans = bool(body.get("hangup_on_answer", True))

            if not phone:
                self._send_json_resp(400, {"ok": False, "error": "目标手机号不能为空"})
                return

            resp = self.backend.execute_cmd(
                "call_dial",
                params={"phone": phone, "timeout": timeout_sec, "hangup_on_answer": hangup_on_ans, "slot": target_slot},
                slot=target_slot,
                timeout=8.0
            )
            if resp.get("ok"):
                used_slot = resp.get("slot") or target_slot or "slot_2"
                self._send_json_resp(200, {
                    "ok": True,
                    "slot": used_slot,
                    "status": "DIALING",
                    "timeout": timeout_sec,
                    "msg": f"正在向 {phone} 发起 VoLTE 呼叫，{timeout_sec}秒后自动挂断（防扣费）",
                    "data": resp.get("data")
                })
            else:
                err_msg = resp.get("error") or self._format_error_message(resp)
                self._send_json_resp(400 if ("HARDWARE_UNSUPPORTED" in str(resp) or "NO_VOLTE_SLOT" in str(resp)) else 500, {
                    "ok": False,
                    "slot": target_slot,
                    "error": err_msg,
                    "code": resp.get("code")
                })
            return

        # 5.2 手动挂断当前呼叫 (AIR-30)
        if path == "/api/call/hangup":
            resp = self.backend.execute_cmd("call_hangup", slot=target_slot, timeout=4.0)
            self._send_json_resp(200, {
                "ok": resp.get("ok", False),
                "slot": target_slot,
                "msg": "已执行挂断指令" if resp.get("ok") else (resp.get("error") or "挂断失败")
            })
            return

        # 6. 触发空中 FOTA 更新 (支持定向卡槽)
        if path == "/api/control/fota":
            resp = self.backend.execute_cmd("trigger_fota", slot=target_slot, timeout=6.0)
            if resp.get("ok"):
                self._send_json_resp(200, {"ok": True, "slot": target_slot, "msg": "已触发板卡 FOTA 固件检测"})
            else:
                self._send_json_resp(200, {"ok": False, "slot": target_slot, "error": resp.get("error") or "FOTA 触发失败"})
            return

        # 6.1 删除单条短信 (支持墓碑持久化)
        if path == "/api/control/delete_sms":
            msg_id = body.get("id")
            sender = body.get("sender") or body.get("phone") or ""
            content = body.get("content") or ""
            sms_time = body.get("time") or ""
            active_iccid = body.get("iccid")
            if not active_iccid:
                with self.backend.cache_lock:
                    s_meta = next((s for s in self.backend.slots if s.get("slot") == target_slot), {})
                    active_iccid = s_meta.get("iccid")
            comp = self.backend.storage_mgr.get_compartment(active_iccid)
            succ = comp.add_tombstone(msg_id, sender, content, sms_time)
            self._send_json_resp(200, {
                "ok": True,
                "slot": target_slot,
                "msg": "已从本地归档移除并生成墓碑记录" if succ else "已记录删除墓碑"
            })
            return

        # 10. 串口分块平滑热更 (AIR-29 Serial SOTA / AIR-31 门禁防呆)
        if path == "/api/control/upgrade_script":
            action_slot = target_slot

            curr_slot_info = {}
            with self.backend.cache_lock:
                for s in self.backend.slots:
                    if s.get("slot") == action_slot:
                        curr_slot_info = s
                        break
            current_ver = curr_slot_info.get("version") or ""
            model = curr_slot_info.get("model") or "Air780"
            bsp = curr_slot_info.get("bsp") or model
            cur_tuple = parse_semver(current_ver)

            # 校验版本是否支持串口热更
            if cur_tuple < (1, 2, 6):
                self._send_json_resp(400, {
                    "ok": False,
                    "error": f"模组固件版本 (v{current_ver or '未知'}) 较早，尚未内置串口极速热更协议桩，请使用【重新刷机控制台】升级底座固件",
                    "slot": action_slot
                })
                return

            with flashing_lock:
                if flashing_state["is_flashing"]:
                    self._send_json_resp(423, {
                        "ok": False,
                        "error": f"已有卡槽 [{flashing_state['slot']}] 正在烧录升级中，请稍候...",
                        "state": flashing_state
                    })
                    return
                flashing_state["is_flashing"] = True
                flashing_state["slot"] = action_slot
                flashing_state["percent"] = 5
                flashing_state["status"] = "starting_ota"
                flashing_state["stage"] = "starting"
                flashing_state["error"] = None
                flashing_state["start_time"] = time.time()

            def _ota_worker(slot_to_upgrade):
                def _cb(pct, status_text, stage="flashing"):
                    update_flashing_progress(pct, status_text, stage=stage, slot=slot_to_upgrade)
                    self.backend.broadcast_sse("flash_progress", {
                        "slot": slot_to_upgrade,
                        "percent": pct,
                        "status": status_text,
                        "stage": stage,
                        "mode": "serial_sota"
                    })

                res = self.backend.perform_serial_ota(slot=slot_to_upgrade, progress_cb=_cb)
                if not res.get("ok"):
                    update_flashing_progress(0, "failed", stage="failed", error=res.get("error"), slot=slot_to_upgrade)
                    self.backend.broadcast_sse("flash_progress", {
                        "slot": slot_to_upgrade,
                        "percent": 0,
                        "status": "failed",
                        "stage": "failed",
                        "error": res.get("error")
                    })
                else:
                    update_flashing_progress(100, "success", stage="success", slot=slot_to_upgrade)
                    self.backend.broadcast_sse("flash_progress", {
                        "slot": slot_to_upgrade,
                        "percent": 100,
                        "status": "success",
                        "stage": "success",
                        "target_version": res.get("target_version")
                    })

            threading.Thread(target=_ota_worker, args=(action_slot,), daemon=True).start()
            self._send_json_resp(200, {
                "ok": True,
                "slot": action_slot,
                "msg": f"已成功启动卡槽 [{action_slot}] 串口平滑热更任务",
                "status": "started"
            })
            return

        # 10.1 硬件底层线刷与全新模块烧录 (FlashToolCLI · AIR-35 通用多芯片引擎)
        if path == "/api/control/flash":
            data = body or {}
            action_slot = data.get("slot") or target_slot
            req_port = data.get("port")
            req_chip = data.get("chip_type") or data.get("chip")
            req_mode = data.get("mode") or "script"  # 'script' 或 'full'
            req_model = data.get("hardware_model")

            curr_slot_info = {}
            if action_slot:
                with self.backend.cache_lock:
                    for s in self.backend.slots:
                        if s.get("slot") == action_slot:
                            curr_slot_info = s
                            break

            target_port = req_port or curr_slot_info.get("port")
            model_bsp = req_model or curr_slot_info.get("model") or curr_slot_info.get("bsp") or ""

            # 归一化芯片类型 (AIR-35: 彻底支持 EC718PV 与 EC618 双芯片架构)
            import firmware_flasher
            chip_type = firmware_flasher.normalize_chip_type(model_bsp, req_chip)

            def _cli_flash_worker(slot_id, port, chip, mode, model):
                self.backend.broadcast_sse("cli_flash_progress", {
                    "slot": slot_id or "new_device",
                    "percent": 5,
                    "message": f"正在准备向目标设备 ({port or 'Bootloader自动探测'}) 下发烧录任务 ({chip.upper()} · {'全量' if mode=='full' else '脚本'})...",
                    "stage": "starting"
                })
                # 1. 若为已知卡槽，暂停轮询以防冲突
                if slot_id:
                    self.backend.execute_cmd("pause_for_flash", {}, slot=slot_id, timeout=3.0)
                time.sleep(0.5)

                def _cb(pct, msg):
                    self.backend.broadcast_sse("cli_flash_progress", {
                        "slot": slot_id or "new_device",
                        "percent": pct,
                        "message": msg,
                        "stage": "flashing"
                    })

                try:
                    res = firmware_flasher.flash_hardware_cli(
                        current_vuart_port=port,
                        hardware_model=model,
                        chip_type=chip,
                        mode=mode,
                        progress_cb=_cb
                    )
                except Exception as e:
                    res = {"ok": False, "msg": f"调用烧录引擎异常: {e}"}

                # 2. 恢复串口轮询
                if slot_id:
                    self.backend.execute_cmd("resume_after_flash", {}, slot=slot_id, timeout=3.0)

                if res.get("ok"):
                    self.backend.broadcast_sse("cli_flash_progress", {
                        "slot": slot_id or "new_device",
                        "percent": 100,
                        "message": f"线刷完成！{chip.upper()} 模组已平滑重启生效",
                        "stage": "success",
                        "port": res.get("port"),
                        "chip": chip,
                        "mode": mode
                    })
                else:
                    self.backend.broadcast_sse("cli_flash_progress", {
                        "slot": slot_id or "new_device",
                        "percent": 0,
                        "message": f"硬件线刷中断: {res.get('msg')}",
                        "stage": "error"
                    })

            threading.Thread(
                target=_cli_flash_worker,
                args=(action_slot, target_port, chip_type, req_mode, model_bsp),
                daemon=True
            ).start()

            self._send_json_resp(200, {
                "ok": True,
                "slot": action_slot or "new_device",
                "chip": chip_type,
                "mode": req_mode,
                "port": target_port,
                "msg": f"已启动 {chip_type.upper()} ({'全量系统刷入' if req_mode=='full' else '极速应用脚本更新'}) 任务",
                "status": "started"
            })
            return

        # 7. 清空板载历史记录 (支持定向卡槽)
        if path == "/api/control/clear_history":
            resp = self.backend.execute_cmd("clear_history", slot=target_slot, timeout=6.0)
            if resp.get("ok"):
                with self.backend.cache_lock:
                    self.backend.recent_sms_by_slot[target_slot] = []
                self._send_json_resp(200, {"ok": True, "slot": target_slot, "msg": "板载短信黑匣子已清空"})
            else:
                self._send_json_resp(200, {"ok": False, "slot": target_slot, "error": resp.get("error") or "清空黑匣子失败"})
            return

        # 8. 保存网关通用配置 (含通知设置与 MCP 开关)
        if path in ("/api/config", "/api/config/notify"):
            try:
                cur_cfg = {}
                if os.path.exists(GATEWAY_CONFIG_PATH):
                    with open(GATEWAY_CONFIG_PATH, "r", encoding="utf-8") as f:
                        cur_cfg = json.load(f)
                for k, v in body.items():
                    if isinstance(v, dict):
                        if k not in cur_cfg: cur_cfg[k] = {}
                        cur_cfg[k].update(v)
                    else:
                        cur_cfg[k] = v

                with open(GATEWAY_CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cur_cfg, f, ensure_ascii=False, indent=2)

                # 向底层 Hub 发送配置重载指令（免检内部指令携带内部令牌）
                hub_cmd = {
                    "type": "cmd",
                    "cmd": "reload_notify_config",
                    "source": "web",
                    "token": getattr(self.backend, "internal_session_token", "")
                }
                try:
                    line = json.dumps(hub_cmd, ensure_ascii=False) + "\n"
                    with self.backend.sock_lock:
                        if self.backend.sock:
                            self.backend.sock.sendall(line.encode("utf-8"))
                except Exception:
                    pass

                self._send_json_resp(200, {"ok": True, "msg": "系统配置已成功保存并立即生效"})
            except Exception as e:
                self._send_json_resp(500, {"ok": False, "error": f"保存配置失败: {e}"})
            return

        # 9. 测试单渠道推送
        if path == "/api/config/notify/test":
            channel = body.get("channel")
            channel_cfg = body.get("config") or {}
            if not channel or not isinstance(channel_cfg, dict):
                self._send_json_resp(400, {"ok": False, "error": "缺少测试渠道或参数"})
                return

            target_slot = body.get("slot")
            dev_desc = None
            if target_slot and hasattr(self.backend, "hub") and self.backend.hub:
                session = self.backend.hub.session_pool.get_session(target_slot)
                if session:
                    m = session.meta.get("model") or "Air780"
                    im = session.meta.get("imei") or ""
                    dev_desc = f"[{session.slot_id.upper()}] {m} · IMEI: {im}" if im else f"[{session.slot_id.upper()}] {m}"
            res = test_channel_push(channel, channel_cfg, device_desc=dev_desc)
            self._send_json_resp(200, res)
            return

        self._send_json_resp(404, {"ok": False, "error": "接口不存在"})

    def handle_sse_stream(self):
        """处理 SSE 持续事件推送流"""
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q = self.backend.register_sse_listener()
        _log(f"前端已建立 SSE 事件流连接 (当前队列数: {len(self.backend.sse_listeners)})")

        # 初始向刚连接的前端发送当前卡槽列表
        init_slots_event = {
            "event": "cluster_update",
            "data": {
                "slots": self.backend.slots,
                "active_slot": self.backend.active_slot
            }
        }
        init_payload = f"event: cluster_update\ndata: {json.dumps(init_slots_event['data'], ensure_ascii=False)}\n\n".encode("utf-8")
        try:
            self.wfile.write(init_payload)
            self.wfile.flush()
        except Exception:
            self.backend.unregister_sse_listener(q)
            return

        try:
            while True:
                try:
                    item = q.get(timeout=15.0)
                    evt_name = item.get("event", "message")
                    evt_data = json.dumps(item.get("data", {}), ensure_ascii=False)
                    payload = f"event: {evt_name}\ndata: {evt_data}\n\n".encode("utf-8")
                    self.wfile.write(payload)
                    self.wfile.flush()
                except queue.Empty:
                    # 发送保活心跳包
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            _log(f"SSE 推送异常: {e}")
        finally:
            self.backend.unregister_sse_listener(q)
            _log("前端已断开 SSE 事件流连接")


# =========================================================================
# WebServer 容器封装
# =========================================================================

class WebServer:
    def __init__(self, host: str = DEFAULT_WEB_HOST, port: int = DEFAULT_WEB_PORT, hub_host: str = DEFAULT_HUB_HOST, hub_port: int = DEFAULT_HUB_PORT):
        self.host = host
        self.port = port
        self.backend = HubBackendClient(host=hub_host, port=hub_port)
        self.httpd = None

    def start(self):
        self.backend.start()
        self.httpd = ThreadedHTTPServer((self.host, self.port), GatewayWebHandler)
        self.httpd.backend = self.backend
        _log(f"Web 控制台已启动，访问地址: http://127.0.0.1:{self.port}")
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            _log("正在关闭 Web 控制台...")
        finally:
            if self.httpd:
                self.httpd.shutdown()
            self.backend.stop()
            _log("Web 控制台已安全关闭")

    def stop(self):
        """外部受控停止 Web 监听与后端 TCP 客户端"""
        try:
            if self.httpd:
                self.httpd.shutdown()
        except Exception:
            pass
        try:
            if self.backend:
                self.backend.stop()
        except Exception:
            pass
        _log("Web 控制台已安全关闭")

def main():
    parser = argparse.ArgumentParser(description="Air780 Series Smart Cellular Gateway Web Console")
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help=f"HTTP 监听端口 (默认 {DEFAULT_WEB_PORT})")
    parser.add_argument("--host", type=str, default=DEFAULT_WEB_HOST, help=f"HTTP 监听地址 (默认 {DEFAULT_WEB_HOST})")
    parser.add_argument("--hub-host", type=str, default=DEFAULT_HUB_HOST, help="底层 Hub 主机地址")
    parser.add_argument("--hub-port", type=int, default=DEFAULT_HUB_PORT, help="底层 Hub 监听端口")
    args = parser.parse_args()

    server = WebServer(host=args.host, port=args.port, hub_host=args.hub_host, hub_port=args.hub_port)
    server.start()

if __name__ == "__main__":
    main()
