# -*- coding: utf-8 -*-
"""
Air780 系列智能通信网关 - 集群智能出站路由与分流引擎 (ClusterRouter)
功能：
1. 规范化清洗手机号码与覆盖四大运营商（移动/联通/电信/广电及虚商）的前缀识别器；
2. 四大出站路由策略：
   - direct: 定向直发（死守防串台铁律：指定 slot 不在线或不可用时坚决快速失败，绝不降级）；
   - operator_affinity: 同网亲和优先（根据目标手机号匹配同网 SIM 卡，降低资费与跨网时延）；
   - round_robin: 平滑轮询负载均衡（多卡轮换出站，均匀分摊发信频次）；
   - signal_best: 最佳网络信号优先（选取当前 CSQ 最高健康卡槽）。
3. In-flight 安全保护与 Failover 准入红线（在飞未决超时禁止跨卡重发，杜绝重复扣费）；
4. Panic Threshold 全熔断紧急兜底通道（集群全灭时尽力而为，杜绝系统性瘫痪）。
"""

import re
import threading
from enum import Enum
from typing import Dict, Any, Optional, List, Tuple

from cluster_health import ClusterHealthMonitor, HealthState

class RouteStrategy(str, Enum):
    DIRECT = "direct"
    OPERATOR_AFFINITY = "operator_affinity"
    ROUND_ROBIN = "round_robin"
    SIGNAL_BEST = "signal_best"


# 运营商前缀规则库
CARRIER_PREFIXES = {
    "cmcc": [ # 中国移动
        "134", "135", "136", "137", "138", "139", "147", "150", "151", "152",
        "157", "158", "159", "172", "178", "182", "183", "184", "187", "188",
        "195", "197", "198"
    ],
    "cucc": [ # 中国联通
        "130", "131", "132", "145", "155", "156", "166", "175", "176", "185",
        "186", "196"
    ],
    "ctcc": [ # 中国电信
        "133", "149", "153", "173", "177", "180", "181", "189", "190", "191",
        "193", "199"
    ],
    "cbn": [  # 中国广电
        "192"
    ]
}

# 虚拟运营商前缀
MVNO_PREFIXES = {
    "1700": "ctcc", "1701": "ctcc", "1702": "ctcc", "162": "ctcc",
    "1703": "cmcc", "1705": "cmcc", "1706": "cmcc", "165": "cmcc",
    "1704": "cucc", "1707": "cucc", "1708": "cucc", "1709": "cucc",
    "171": "cucc", "167": "cucc"
}


def clean_phone_number(phone: str) -> str:
    """标准化清洗手机号码：剥离 +86, 0086, 空格, 破折号, 括号等"""
    if not phone:
        return ""
    p = str(phone).strip()
    p = re.sub(r"[\s\-\(\)]+", "", p)
    if p.startswith("+86"):
        p = p[3:]
    elif p.startswith("0086"):
        p = p[4:]
    return p


def detect_phone_carrier(phone: str) -> str:
    """
    根据清洗后的手机号识别目标运营商：
    返回: 'cmcc' | 'cucc' | 'ctcc' | 'cbn' | 'unknown'
    """
    p = clean_phone_number(phone)
    if len(p) < 3:
        return "unknown"

    # 0. 常用运营商特服客服号码优先识别
    if p in ("10010", "10011", "10015"):
        return "cucc"
    if p in ("10086", "10085", "10088"):
        return "cmcc"
    if p in ("10000", "10001"):
        return "ctcc"
    if p in ("10099",):
        return "cbn"

    # 先匹配 4 位与 3 位虚商
    if len(p) >= 4 and p[:4] in MVNO_PREFIXES:
        return MVNO_PREFIXES[p[:4]]
    if len(p) >= 3 and p[:3] in MVNO_PREFIXES:
        return MVNO_PREFIXES[p[:3]]

    p3 = p[:3]
    for carrier, prefixes in CARRIER_PREFIXES.items():
        if p3 in prefixes:
            return carrier
    return "unknown"


def detect_sim_carrier(iccid: str, plmn: str = "") -> str:
    """
    根据模组 SIM 卡的 ICCID 或 PLMN (运营商代码) 推导本卡运营商归属：
    中国移动: 898600, 898602, 898604, 898607, 898608, 46000, 46002, 46004, 46007, 46008
    中国联通: 898601, 898606, 898609, 46001, 46006, 46009
    中国电信: 898603, 898611, 46003, 46005, 46011
    中国广电: 898615, 46015
    """
    iccid = str(iccid or "").strip()
    plmn = str(plmn or "").strip()

    if iccid.startswith(("898600", "898602", "898604", "898607", "898608")) or plmn in ("46000", "46002", "46004", "46007", "46008"):
        return "cmcc"
    if iccid.startswith(("898601", "898606", "898609")) or plmn in ("46001", "46006", "46009"):
        return "cucc"
    if iccid.startswith(("898603", "898611")) or plmn in ("46003", "46005", "46011"):
        return "ctcc"
    if iccid.startswith("898615") or plmn == "46015":
        return "cbn"
    return "unknown"


class RouteDecision:
    """路由裁决结果包装对象"""
    def __init__(self, ok: bool, slot_id: Optional[str] = None, session: Any = None,
                 strategy_used: str = "", error: Optional[str] = None,
                 fallback_used: bool = False, panic_mode: bool = False):
        self.ok = ok
        self.slot_id = slot_id
        self.session = session
        self.strategy_used = strategy_used
        self.error = error
        self.fallback_used = fallback_used
        self.panic_mode = panic_mode

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "slot": self.slot_id,
            "strategy": self.strategy_used,
            "fallback": self.fallback_used,
            "panic_mode": self.panic_mode,
            "error": self.error
        }


class ClusterRouter:
    """集群智能出站路由器"""
    def __init__(self, session_pool: Any, health_monitor: ClusterHealthMonitor):
        self.session_pool = session_pool
        self.health_monitor = health_monitor
        self.rr_index = 0
        self.lock = threading.Lock()

    def route_outbound(self, target_phone: str, strategy: str = "operator_affinity",
                       direct_slot: Optional[str] = None) -> RouteDecision:
        """
        核心分流方法：
        1. 若显式指定 direct_slot -> 执行 Direct 定向直发（死守隔离铁律，绝不降级）；
        2. 若未指定 direct_slot -> 按照策略在可用健康卡槽池中智能优选。
        """
        target_phone = clean_phone_number(target_phone)

        # -------------------------------------------------------------
        # 1. Direct 模式：死守隔离铁律
        # -------------------------------------------------------------
        if direct_slot and direct_slot.strip() and direct_slot.strip().lower() != "auto":
            target_slot = direct_slot.strip()
            session = self.session_pool.get_session(target_slot)
            if not session or not session.is_connected:
                return RouteDecision(
                    ok=False,
                    slot_id=target_slot,
                    strategy_used=RouteStrategy.DIRECT.value,
                    error=f"TARGET_SLOT_UNAVAILABLE: 指定卡槽 [{target_slot}] 处于离线状态，严格隔离拒绝发送"
                )
            # 校验是否插卡并驻网就绪
            if session.meta.get("net_ready") is False and not session.meta.get("iccid"):
                return RouteDecision(
                    ok=False,
                    slot_id=target_slot,
                    strategy_used=RouteStrategy.DIRECT.value,
                    error=f"TARGET_SLOT_NO_SIM: 指定卡槽 [{target_slot}] 未插 SIM 卡或网络未就绪，无法发送短信"
                )
            # 检查是否处于完全熔断态
            health = self.health_monitor.get_or_create(target_slot)
            if health.state == HealthState.ISOLATED:
                return RouteDecision(
                    ok=False,
                    slot_id=target_slot,
                    strategy_used=RouteStrategy.DIRECT.value,
                    error=f"TARGET_SLOT_UNAVAILABLE: 指定卡槽 [{target_slot}] 已触发故障熔断隔离，严格隔离拒绝发送"
                )

            return RouteDecision(
                ok=True,
                slot_id=target_slot,
                session=session,
                strategy_used=RouteStrategy.DIRECT.value
            )

        # -------------------------------------------------------------
        # 2. Auto 模式：多级健康度候选池构建
        # -------------------------------------------------------------
        all_sessions = []
        with self.session_pool.pool_lock:
            for s in self.session_pool.sessions.values():
                if s.is_connected:
                    all_sessions.append(s)

        if not all_sessions:
            return RouteDecision(
                ok=False,
                strategy_used=strategy,
                error="CLUSTER_NO_ONLINE_SLOT: 通信集群中暂无任何物理在线卡槽"
            )

        # 分级候选池
        healthy_candidates = []
        degraded_candidates = []

        for s in all_sessions:
            if getattr(s, "is_flashing", False):
                continue
            rec = self.health_monitor.get_or_create(s.slot_id)
            if rec.is_routable():
                healthy_candidates.append(s)
            elif rec.state == HealthState.DEGRADED:
                degraded_candidates.append(s)

        chosen_session: Optional[Any] = None
        fallback_used = False
        panic_mode = False

        active_pool = healthy_candidates
        if not active_pool:
            if degraded_candidates:
                # 降级池可用
                active_pool = degraded_candidates
                fallback_used = True
            else:
                # Panic 紧急兜底保护：全集群所有卡槽全灭 (全降级/全熔断)
                # 选出在线且 CSQ 相对最高的卡槽尽力而为
                active_pool = sorted(all_sessions, key=lambda x: self.health_monitor.get_or_create(x.slot_id).csq, reverse=True)
                fallback_used = True
                panic_mode = True

        # -------------------------------------------------------------
        # 3. 策略调度
        # -------------------------------------------------------------
        strategy = (strategy or RouteStrategy.OPERATOR_AFFINITY.value).lower()

        if strategy == RouteStrategy.OPERATOR_AFFINITY.value:
            carrier = detect_phone_carrier(target_phone)
            matched_pool = []
            if carrier != "unknown":
                for s in active_pool:
                    sim_carrier = detect_sim_carrier(s.meta.get("iccid", ""))
                    if sim_carrier == carrier:
                        matched_pool.append(s)

            if matched_pool:
                # 同网卡中取信号最佳
                chosen_session = max(matched_pool, key=lambda x: self.health_monitor.get_or_create(x.slot_id).csq)
            else:
                # 无同网卡 -> 平滑降级为 signal_best
                chosen_session = max(active_pool, key=lambda x: self.health_monitor.get_or_create(x.slot_id).csq)
                fallback_used = True

        elif strategy == RouteStrategy.ROUND_ROBIN.value:
            with self.lock:
                self.rr_index = (self.rr_index + 1) % len(active_pool)
                chosen_session = active_pool[self.rr_index]

        elif strategy == RouteStrategy.SIGNAL_BEST.value:
            chosen_session = max(active_pool, key=lambda x: self.health_monitor.get_or_create(x.slot_id).csq)

        else:
            # 默认回退为首选活跃卡槽或最佳信号
            chosen_session = active_pool[0]

        # 若命中的是半开态卡槽，登记在飞试探配额
        if chosen_session:
            self.health_monitor.acquire_half_open_slot(chosen_session.slot_id)

        return RouteDecision(
            ok=True,
            slot_id=chosen_session.slot_id,
            session=chosen_session,
            strategy_used=strategy,
            fallback_used=fallback_used,
            panic_mode=panic_mode
        )

    def can_failover(self, error_msg: str, in_flight: bool) -> bool:
        """
        Failover 准入准则：
        【防重发与多重扣费铁律】：若指令已下发至硬件且正在等待基带/网络终态 (in_flight=True)，
        严禁触发 Failover 重试，防止基站延迟入网导致的双发双扣！
        仅当调度前端口无法开启、握手离线或底层明确报不可恢复致命错误时才允许转移。
        """
        if in_flight:
            return False
        err = str(error_msg).upper()
        if "UNAVAILABLE" in err or "DISCONNECTED" in err or "OFFLINE" in err:
            return True
        return False
