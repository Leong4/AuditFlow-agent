"""
AuditFlow - Decision Trace（可审计决策链）
==========================================
记录整个对账流程中每个 Agent 的每一步决策。

这是 AuditFlow 的核心差异化功能：
- 普通 AI：给你一个结论，不知道怎么来的
- AuditFlow：每一步推理都有记录，出错时精确定位是哪个 Agent 的哪一步出了问题

使用方式：
    from shared.trace import AuditTrace, TraceStep, new_trace, add_step, finish_trace

典型流程：
    trace = new_trace(entity="Acme Corp", raw_query="为什么合同和到账对不上？")
    add_step(trace, TraceStep(agent="router", ...))
    add_step(trace, TraceStep(agent="crm_agent", ...))
    ...
    finish_trace(trace)
    print(trace.to_dict())  # 输出完整 JSON
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import uuid


# ─────────────────────────────────────────────
# 单步 Trace
# ─────────────────────────────────────────────

@dataclass
class TraceStep:
    """
    单个 Agent 的单次决策记录。
    每个 Agent 执行完后往 trace 里加一条。
    """
    agent: str          # agent 名称，例如 "router", "crm_agent", "reconciliation", "root_cause"
    layer: str          # 所在层，例如 "routing", "data", "analysis", "diagnosis"
    decision: str       # 这一步做了什么决定，一句话描述
    reason: str = ""    # 为什么这么决定（推理依据）

    # 可选补充字段（不同 agent 填不同的）
    confidence: Optional[float] = None         # 置信度，0.0 ~ 1.0
    data_freshness: Optional[str] = None       # 数据截止日期，System Agent 填
    rules_reported: list[str] = field(default_factory=list)    # 报告的业务规则，System Agent 填
    discrepancies_found: int = 0               # 发现的差异数量，Reconciliation Agent 填
    anomaly_status: Optional[str] = None       # 判定结果，Root-Cause Agent 填
    error: Optional[str] = None                # 如果这步出错了，记录错误信息

    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None and v != [] and v != 0}


# ─────────────────────────────────────────────
# 完整 Trace
# ─────────────────────────────────────────────

@dataclass
class AuditTrace:
    """
    一次完整对账请求的决策链。
    包含从用户提问到最终结论的所有步骤。
    """
    trace_id: str                               # 唯一 ID，自动生成
    entity: str                                 # 被查询的客户/公司名称
    raw_query: str                              # 用户原始问题

    steps: list[TraceStep] = field(default_factory=list)

    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: Optional[str] = None
    total_duration_ms: Optional[int] = None     # 整体耗时（毫秒）
    status: str = "in_progress"                 # "in_progress" | "completed" | "failed"

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "entity": self.entity,
            "raw_query": self.raw_query,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_duration_ms": self.total_duration_ms,
            "steps": [step.to_dict() for step in self.steps],
        }


# ─────────────────────────────────────────────
# 工具函数（队友直接调用这些）
# ─────────────────────────────────────────────

def new_trace(entity: str, raw_query: str) -> AuditTrace:
    """
    开始一次新的对账追踪。
    在 Router Agent 收到用户问题时调用。

    示例：
        trace = new_trace(entity="Acme Corp", raw_query="为什么合同和到账对不上？")
    """
    return AuditTrace(
        trace_id=f"audit_{uuid.uuid4().hex[:8]}",
        entity=entity,
        raw_query=raw_query,
    )


def add_step(trace: AuditTrace, step: TraceStep) -> None:
    """
    往 trace 里加一步。
    每个 Agent 执行完后调用一次。

    示例：
        add_step(trace, TraceStep(
            agent="crm_agent",
            layer="data",
            decision="返回合同数据 + 付款条款",
            data_freshness="2026-03-31",
            rules_reported=["payment_terms: 3 installments 40%+40%+20%"],
        ))
    """
    trace.steps.append(step)


def finish_trace(trace: AuditTrace) -> None:
    """
    标记 trace 完成，计算总耗时。
    在 Root-Cause Agent 输出最终结论后调用。

    示例：
        finish_trace(trace)
        print(trace.to_dict())
    """
    finished = datetime.now(timezone.utc)
    trace.finished_at = finished.isoformat()
    trace.status = "completed"

    started = datetime.fromisoformat(trace.started_at)
    delta_ms = int((finished - started).total_seconds() * 1000)
    trace.total_duration_ms = delta_ms


def fail_trace(trace: AuditTrace, reason: str) -> None:
    """
    标记 trace 失败。
    任何 Agent 遇到无法恢复的错误时调用。

    示例：
        fail_trace(trace, reason="CRM Agent 查询超时")
    """
    trace.finished_at = datetime.now(timezone.utc).isoformat()
    trace.status = "failed"
    add_step(trace, TraceStep(
        agent="system",
        layer="error",
        decision="流程中止",
        reason=reason,
        error=reason,
    ))