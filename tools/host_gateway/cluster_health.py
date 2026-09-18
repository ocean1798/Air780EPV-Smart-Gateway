# -*- coding: utf-8 -*-
"""
Air780 系列智能通信网关 - 集群卡槽健康监控与断路器引擎 (ClusterHealthMonitor)
功能：
1. 建立基于三态断路器 (Healthy / Degraded / Isolated / Half-Open) 的物理卡槽运行健康模型；
2. 3GPP AT 规范 CSQ 99 瞬态去抖动防惊群 (Debounce/Smoothing: 持续>10s或连续3次才判定脱网)；
3. 连续发信失败断路熔断保护与 60 秒冷却半开 (Half-Open) 试探自愈；
4. 维持卡槽健康评分，为 ClusterRouter 智能出站分流提供实时健康度裁决。
"""

import time
from enum import Enum
from typing import Dict, Any, Optional, List


class HealthState(str, Enum):
    HEALTHY = "healthy"      # 健康就绪 (CSQ >= 10, 连续失败 == 0)
    DEGRADED = "degraded"    # 降级运行 (5 <= CSQ < 10 或 连续失败 == 2，仅允许定向直发)
    ISOLATED = "isolated"    # 断路熔断 (连续失败 >= 3 或 持续脱网 CSQ < 5，移出路由池)
    HALF_OPEN = "half_open"  # 半开试探 (熔断满 60s 冷却期，允许单次试探复活)


class SlotHealthRecord:
    """单个物理卡槽的动态健康指标记录"""
    def __init__(self, slot_id: str):
        self.slot_id = slot_id
        self.state: HealthState = HealthState.HEALTHY
        self.csq: int = 15                 # 最近有效 CSQ (0~31)
        self.raw_csq: int = 15             # 原始上报 CSQ (含 99)
        self.csq_99_count: int = 0         # 连续 99 次数
        self.csq_99_first_ts: float = 0.0  # 首次出现 99 的时间戳
        self.consecutive_failures: int = 0 # 连续发信失败计数
        self.last_failure_time: float = 0.0
        self.last_success_time: float = 0.0
        self.isolated_time: float = 0.0    # 进入 ISOLATED 的时间戳
        self.half_open_in_flight: bool = False # 半开态下是否已有在飞试探任务
        self.total_sent: int = 0
        self.total_success: int = 0
        self.total_failed: int = 0
        self.last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "slot": self.slot_id,
            "state": self.state.value,
            "csq": self.csq,
            "raw_csq": self.raw_csq,
            "consecutive_failures": self.consecutive_failures,
            "total_sent": self.total_sent,
            "total_success": self.total_success,
            "total_failed": self.total_failed,
            "last_error": self.last_error,
            "isolated_time": self.isolated_time,
            "routable": self.is_routable()
        }

    def is_routable(self, allow_degraded: bool = False, panic_mode: bool = False) -> bool:
        """
        判断当前卡槽是否具备自动出站路由资格：
        - 正常模式下：仅 HEALTHY 与 (非在飞试探的) HALF_OPEN 具备路由资格；
        - allow_degraded：允许 DEGRADED 参与 (如降级路由)；
        - panic_mode：全集群全熔断紧急兜底时，任何物理在线卡槽均尽力而为。
        """
        if panic_mode:
            return True
        if self.state == HealthState.HEALTHY:
            return True
        if self.state == HealthState.HALF_OPEN and not self.half_open_in_flight:
            return True
        if allow_degraded and self.state == HealthState.DEGRADED:
            return True
        return False


class ClusterHealthMonitor:
    """全集群卡槽健康与断路器监控中枢"""
    def __init__(self, cooldown_seconds: float = 60.0, debounce_99_seconds: float = 10.0):
        self.records: Dict[str, SlotHealthRecord] = {}
        self.cooldown_seconds = cooldown_seconds
        self.debounce_99_seconds = debounce_99_seconds

    def get_or_create(self, slot_id: str) -> SlotHealthRecord:
        if slot_id not in self.records:
            self.records[slot_id] = SlotHealthRecord(slot_id)
        return self.records[slot_id]

    def update_csq(self, slot_id: str, csq: int) -> HealthState:
        """
        更新卡槽信号并应用 CSQ 99 去抖动防惊群平滑逻辑：
        - 3GPP 规范中 99 代表 Not Detectable / 暂不可测（多为开机驻留或基站切换瞬态）；
        - 必须连续 3 次且持续超过 debounce_99_seconds 时才确认脱网熔断。
        """
        rec = self.get_or_create(slot_id)
        now = time.time()
        rec.raw_csq = csq

        if csq == 99 or csq < 0:
            rec.csq_99_count += 1
            if rec.csq_99_first_ts <= 0:
                rec.csq_99_first_ts = now

            # 判断是否超过防抖门槛
            duration = now - rec.csq_99_first_ts
            if rec.csq_99_count >= 3 or duration >= self.debounce_99_seconds:
                # 确认持久脱网 -> 熔断
                if rec.state != HealthState.ISOLATED:
                    rec.state = HealthState.ISOLATED
                    rec.isolated_time = now
                    rec.last_error = f"持久脱网信号不可测 (CSQ=99, 持续{duration:.1f}s)"
        else:
            # 收到真实有效信号 -> 复位 99 防抖计时
            rec.csq = csq
            rec.csq_99_count = 0
            rec.csq_99_first_ts = 0.0

            # 根据信号水线驱动状态流转
            if csq < 5:
                # 信号极其微弱，难以维持射频连接 -> 熔断
                if rec.state != HealthState.ISOLATED:
                    rec.state = HealthState.ISOLATED
                    rec.isolated_time = now
                    rec.last_error = f"信号微弱 (CSQ={csq} < 5)"
            elif 5 <= csq < 10:
                # 信号弱 -> 降级 (移出自动路由，仅定向直发)
                if rec.state not in (HealthState.ISOLATED, HealthState.HALF_OPEN):
                    rec.state = HealthState.DEGRADED
            else:
                # 信号恢复健康 (CSQ >= 10)
                if rec.consecutive_failures == 0 and rec.state in (HealthState.DEGRADED, HealthState.HALF_OPEN):
                    rec.state = HealthState.HEALTHY
                    rec.last_error = None

        self._check_half_open_transition(rec)
        return rec.state

    def record_send_success(self, slot_id: str):
        """记录发信成功，重置连续失败计数并促进自愈恢复"""
        rec = self.get_or_create(slot_id)
        now = time.time()
        rec.total_sent += 1
        rec.total_success += 1
        rec.consecutive_failures = 0
        rec.last_success_time = now
        rec.half_open_in_flight = False

        # 若处于半开或降级态，且信号良好，成功发信直接恢复为 HEALTHY
        if rec.csq >= 10:
            rec.state = HealthState.HEALTHY
            rec.last_error = None
        else:
            rec.state = HealthState.DEGRADED

    def record_send_failure(self, slot_id: str, error_msg: str, is_fatal: bool = False):
        """
        记录发信失败：
        - 失败 1 次：保持 HEALTHY，记录日志；
        - 连续失败 2 次：进入 DEGRADED；
        - 连续失败 >= 3 次或遭遇 fatal (如 SIM 无效/不可恢复)：进入 ISOLATED。
        """
        rec = self.get_or_create(slot_id)
        now = time.time()
        rec.total_sent += 1
        rec.total_failed += 1
        rec.consecutive_failures += 1
        rec.last_failure_time = now
        rec.last_error = error_msg
        rec.half_open_in_flight = False

        if is_fatal or rec.consecutive_failures >= 3:
            rec.state = HealthState.ISOLATED
            rec.isolated_time = now
        elif rec.consecutive_failures == 2:
            if rec.state != HealthState.ISOLATED:
                rec.state = HealthState.DEGRADED

    def _check_half_open_transition(self, rec: SlotHealthRecord):
        """检查 ISOLATED 是否满冷却期，若是则转为 HALF_OPEN"""
        if rec.state == HealthState.ISOLATED:
            if time.time() - rec.isolated_time >= self.cooldown_seconds:
                rec.state = HealthState.HALF_OPEN
                rec.half_open_in_flight = False

    def acquire_half_open_slot(self, slot_id: str) -> bool:
        """半开态下申请一次试探配额，确保同一时刻仅 1 个在飞试探，防止惊群重试"""
        rec = self.get_or_create(slot_id)
        self._check_half_open_transition(rec)
        if rec.state == HealthState.HALF_OPEN and not rec.half_open_in_flight:
            rec.half_open_in_flight = True
            return True
        return False

    def reset_slot(self, slot_id: str):
        """手动重置或强制复位卡槽健康状态"""
        if slot_id in self.records:
            rec = self.records[slot_id]
            rec.state = HealthState.HEALTHY
            rec.consecutive_failures = 0
            rec.csq_99_count = 0
            rec.csq_99_first_ts = 0.0
            rec.isolated_time = 0.0
            rec.half_open_in_flight = False
            rec.last_error = None

    def get_summary(self) -> Dict[str, Any]:
        """获取集群所有卡槽的健康看板数据"""
        now = time.time()
        slots_data = {}
        for s_id, rec in self.records.items():
            self._check_half_open_transition(rec)
            slots_data[s_id] = rec.to_dict()
        return {
            "timestamp": now,
            "slots": slots_data
        }
