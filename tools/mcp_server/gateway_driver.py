# -*- coding: utf-8 -*-
"""
Air780EPV 智能通信网关 - 透明自托管 IPC 驱动客户端
特性：
1. 客户端透明自拉起（Auto-spawn）：未检测到 Hub 服务时静默在后台拉起 gateway_hub.py 并连接；
2. 绝对纯净 Stdio：严禁向 sys.stdout 输出任何内容（杜绝 MCP stdio JSON-RPC 管道污染），日志全走 sys.stderr；
3. 多路事件分发与条件变量同步：支持带有时间窗口和新鲜度的 wait_for_otp() 毫秒级异步唤醒；
4. 线程安全双向交互：支持多线程并发发起命令请求与 Promise-like 等待。
"""

import sys
import os
import re
import time
import uuid
import json
import socket
import threading
import subprocess
from typing import Optional, Dict, Any, List

HUB_HOST = "127.0.0.1"
HUB_PORT = 17800

def _dbg(msg: str):
    """仅向 stderr 输出调试日志，严禁污染 stdout"""
    now = time.strftime("%H:%M:%S")
    sys.stderr.write(f"[{now}] [GatewayDriver] {msg}\n")
    sys.stderr.flush()

class GatewayDriver:
    _instance = None
    _singleton_lock = threading.Lock()

    def __new__(cls, host: str = HUB_HOST, port: int = HUB_PORT):
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = super(GatewayDriver, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, host: str = HUB_HOST, port: int = HUB_PORT):
        if self._initialized:
            return
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.sock_lock = threading.Lock()
        self.running = False
        self.rx_thread: Optional[threading.Thread] = None

        # 请求池：{ req_id: {"event": threading.Event(), "response": None} }
        self.pending_requests: Dict[str, Dict[str, Any]] = {}
        self.pending_lock = threading.Lock()

        # 事件实时缓存
        self.latest_status: Dict[str, Any] = {}
        self.recent_sms_events: List[Dict[str, Any]] = []
        self.latest_otp: Optional[Dict[str, Any]] = None
        self.state_lock = threading.Lock()

        # 验证码事件同步通知条件变量
        self.otp_cond = threading.Condition(self.state_lock)

        self._initialized = True

    def start(self):
        """确保连接已建立并在后台监听"""
        if self.running and self.sock:
            return
        self.running = True
        self._ensure_connected()
        if not self.rx_thread or not self.rx_thread.is_alive():
            self.rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
            self.rx_thread.start()

    def stop(self):
        """停止客户端驱动"""
        self.running = False
        with self.sock_lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

    def _auto_spawn_hub(self):
        """静默自动拉起后台 Gateway Hub"""
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        hub_path = os.path.join(base_dir, "host_gateway", "gateway_hub.py")
        if not os.path.exists(hub_path):
            _dbg(f"未找到 Hub 脚本: {hub_path}")
            return

        _dbg(f"正在后台静默自拉起网关共享中枢: {hub_path}...")
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
            _dbg(f"自拉起 Hub 失败: {e}")

    def _ensure_connected(self) -> bool:
        """检查并确保本地 IPC Socket 已连接；若未启动则自拉起并重试"""
        with self.sock_lock:
            if self.sock:
                return True

            for attempt in range(1, 5):
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    s.settimeout(2.0)
                    s.connect((self.host, self.port))
                    s.settimeout(None)
                    self.sock = s
                    _dbg(f"成功连接至本地共享中枢 ({self.host}:{self.port})")
                    return True
                except (ConnectionRefusedError, OSError):
                    if attempt == 1:
                        self._auto_spawn_hub()
                    time.sleep(0.5)

            _dbg("连接本地网关中枢超时")
            return False

    def _rx_loop(self):
        buffer = ""
        while self.running:
            if not self._ensure_connected():
                time.sleep(1.0)
                continue

            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    _dbg("与 Hub 的 IPC 连接断开，准备重连...")
                    with self.sock_lock:
                        if self.sock:
                            try:
                                self.sock.close()
                            except Exception:
                                pass
                            self.sock = None
                    time.sleep(0.5)
                    continue

                buffer += chunk.decode("utf-8", errors="ignore")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line or not (line.startswith("{") and line.endswith("}")):
                        continue

                    try:
                        obj = json.loads(line)
                        self._handle_frame(obj)
                    except Exception:
                        pass
            except Exception as e:
                _dbg(f"IPC 读取异常: {e}")
                with self.sock_lock:
                    if self.sock:
                        try:
                            self.sock.close()
                        except Exception:
                            pass
                        self.sock = None
                time.sleep(0.5)

    @staticmethod
    def _extract_otp_fallback(content: str, sender: str = "") -> str:
        """宿主机端工业级兜底提码引擎（支持验证码/口令/PIN/OTP及倒装句）"""
        if not content or len(content) < 4:
            return ""
        kw = r"(?:验证码|校验码|动态码|动态密码|口令|动态口令|授权码|确认码|激活码|安全码|检验码|取件码|code|otp|pin)"
        m1 = re.search(kw + r"[^\d]{0,15}(\d{4,8})", content, re.IGNORECASE)
        if m1:
            cand = m1.group(1)
            if cand != sender:
                return cand
        m2 = re.search(r"(\d{4,8})[^\d]{0,15}" + kw, content, re.IGNORECASE)
        if m2:
            cand = m2.group(1)
            if cand != sender:
                return cand
        return ""

    def _handle_frame(self, obj: Dict[str, Any]):
        frame_type = obj.get("type")

        # 1. 响应帧 -> 唤醒等待该 ID 的请求者
        if frame_type in ("res", "response"):
            req_id = obj.get("id")
            with self.pending_lock:
                if req_id in self.pending_requests:
                    self.pending_requests[req_id]["response"] = obj
                    self.pending_requests[req_id]["event"].set()

        # 2. 异步事件广播
        elif frame_type == "event":
            evt = obj.get("event")
            data = obj.get("data", {})
            slot = obj.get("slot") or (data.get("slot") if isinstance(data, dict) else None) or "slot_1"
            with self.state_lock:
                if evt in ("status", "gateway_ready"):
                    self.latest_status = data
                elif evt == "sms_rx":
                    if isinstance(data, dict):
                        data["slot"] = slot
                    self.recent_sms_events.insert(0, data)
                    if len(self.recent_sms_events) > 50:
                        self.recent_sms_events.pop()

                    # 若检测到验证码，立即唤醒所有等待 OTP 的线程
                    code = data.get("code")
                    if not code:
                        code = self._extract_otp_fallback(data.get("content", ""), data.get("from", ""))
                    if code:
                        self.latest_otp = {
                            "code": code,
                            "slot": slot,
                            "from": data.get("from"),
                            "content": data.get("content"),
                            "ts": int(time.time())
                        }
                        self.otp_cond.notify_all()

    def execute_cmd(self, cmd: str, params: Optional[Dict[str, Any]] = None, slot: Optional[str] = None, timeout: float = 6.0) -> Dict[str, Any]:
        """向网关共享中枢下发指令并同步等待回执"""
        if not self._ensure_connected():
            raise RuntimeError(f"无法连接到网关共享中枢 ({self.host}:{self.port})，请确认后台服务是否正常")

        req_id = "ipc_" + uuid.uuid4().hex[:8]
        req_pkt: Dict[str, Any] = {
            "type": "cmd",
            "id": req_id,
            "cmd": cmd,
            "params": params or {}
        }
        if slot:
            req_pkt["slot"] = slot
            req_pkt["params"]["slot"] = slot

        wait_entry = {
            "event": threading.Event(),
            "response": None
        }

        with self.pending_lock:
            self.pending_requests[req_id] = wait_entry

        try:
            line = json.dumps(req_pkt, ensure_ascii=False) + "\n"
            with self.sock_lock:
                if not self.sock:
                    raise RuntimeError("IPC 管道未连接")
                self.sock.sendall(line.encode("utf-8"))
        except Exception as e:
            with self.pending_lock:
                self.pending_requests.pop(req_id, None)
            raise RuntimeError(f"向网关 IPC 发送指令失败: {e}")

        # 阻塞等待响应
        signaled = wait_entry["event"].wait(timeout=timeout)
        with self.pending_lock:
            self.pending_requests.pop(req_id, None)

        if not signaled or not wait_entry["response"]:
            raise TimeoutError(f"指令 '{cmd}' 等待网关响应超时 ({timeout}s)")

        return wait_entry["response"]

    # ================= 业务方法接口 =================

    def list_dongles(self) -> List[Dict[str, Any]]:
        """获取当前集群所有在线卡槽信息"""
        res = self.execute_cmd("get_slots", timeout=4.0)
        data = res.get("data", {})
        return data.get("slots", [])

    def get_status(self, slot: Optional[str] = None) -> Dict[str, Any]:
        """获取网关看板数据（支持指定卡槽）"""
        res = self.execute_cmd("get_status", slot=slot, timeout=4.0)
        return res.get("data", {})

    def send_sms(self, phone: str, text: str, slot: Optional[str] = None) -> Dict[str, Any]:
        """主动发送短信（支持指定卡槽出站）"""
        if not phone or not text:
            raise ValueError("手机号和短信正文不能为空")
        res = self.execute_cmd("send_sms", {"phone": phone, "content": text}, slot=slot, timeout=8.0)
        return res

    def get_history(self, limit: int = 20, slot: Optional[str] = None) -> Dict[str, Any]:
        """获取脱机黑匣子短信（支持指定卡槽）"""
        res = self.execute_cmd("get_history", {"limit": limit}, slot=slot, timeout=5.0)
        return res.get("data", {})

    def clear_history(self, slot: Optional[str] = None) -> Dict[str, Any]:
        """清空脱机黑匣子（支持指定卡槽）"""
        return self.execute_cmd("clear_history", slot=slot, timeout=4.0)

    def set_rndis(self, enable: bool, slot: Optional[str] = None) -> Dict[str, Any]:
        """启闭 4G 随身上网 (USB 虚拟网卡)"""
        return self.execute_cmd("set_rndis", {"enable": enable}, slot=slot, timeout=5.0)

    def set_cellular_data(self, enable: bool, slot: Optional[str] = None) -> Dict[str, Any]:
        """启闭模组自身 4G 蜂窝数据 (0流量纯信令保号开关)"""
        return self.execute_cmd("set_cellular_data", {"enable": enable}, slot=slot, timeout=5.0)

    def reboot(self, reason: str = "mcp_trigger", slot: Optional[str] = None) -> Dict[str, Any]:
        """重启模组（支持指定卡槽）"""
        return self.execute_cmd("reboot", {"reason": reason}, slot=slot, timeout=4.0)

    def wait_for_otp(self, timeout_seconds: int = 20, freshness_seconds: int = 180, slot: Optional[str] = None) -> Dict[str, Any]:
        """
        核心时序方法：带有新鲜度检查与条件变量挂起守候的验证码获取引擎
        1. 检查最近 freshness_seconds 内是否已有未消费的验证码（可按 slot 过滤）；
        2. 若无，在条件变量上挂起等待，直到收到新短信事件或超时；
        3. 若仍未收到，返回超时异常。
        """
        self.start()
        now = time.time()

        with self.state_lock:
            # 1. 检查当前缓存中是否存在新鲜的验证码
            if self.latest_otp and (now - self.latest_otp.get("ts", 0) <= freshness_seconds):
                if not slot or self.latest_otp.get("slot") == slot:
                    return self.latest_otp

            # 2. 若无新鲜验证码，在条件变量上挂起守候
            deadline = now + timeout_seconds
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self.otp_cond.wait(timeout=min(remaining, 1.0))
                # 检查是否刚被唤醒并收到了新的合法 OTP
                if self.latest_otp and (time.time() - self.latest_otp.get("ts", 0) <= freshness_seconds):
                    if not slot or self.latest_otp.get("slot") == slot:
                        return self.latest_otp

        # 3. 兜底检查脱机黑匣子（防止 Hub 重启间隙到站漏记）
        try:
            hist = self.get_history(limit=5, slot=slot)
            for it in hist.get("items", []):
                otp = it.get("otp")
                if not otp:
                    otp = self._extract_otp_fallback(it.get("content", ""), it.get("sender", ""))
                item_ts = it.get("time", 0)
                if otp and (time.time() - item_ts <= freshness_seconds):
                    return {
                        "code": otp,
                        "slot": slot or "slot_1",
                        "from": it.get("sender"),
                        "content": it.get("content"),
                        "ts": item_ts
                    }
        except Exception:
            pass

        slot_desc = f" (指定卡槽: {slot})" if slot else ""
        raise TimeoutError(f"在 {timeout_seconds} 秒守候窗口内未收到新的验证码短信{slot_desc} (新鲜度设定: {freshness_seconds}秒)")

    def get_latest_otp(self, freshness_seconds: int = 180, slot: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """瞬时获取验证码（不阻塞等待，支持按 slot 过滤）"""
        self.start()
        with self.state_lock:
            if self.latest_otp and (time.time() - self.latest_otp.get("ts", 0) <= freshness_seconds):
                if not slot or self.latest_otp.get("slot") == slot:
                    return self.latest_otp

        hist = self.get_history(limit=10, slot=slot)
        for it in hist.get("items", []):
            otp = it.get("otp")
            if not otp:
                otp = self._extract_otp_fallback(it.get("content", ""), it.get("sender", ""))
            item_ts = it.get("time", 0)
            if otp and (time.time() - item_ts <= freshness_seconds):
                return {
                    "code": otp,
                    "slot": slot or "slot_1",
                    "from": it.get("sender"),
                    "content": it.get("content"),
                    "ts": item_ts
                }
        return None
