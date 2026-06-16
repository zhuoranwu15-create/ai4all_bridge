from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


RISK_LEVEL_ORDER = {
    "pass": 0,
    "review": 1,
    "block": 2,
    "escalate": 3,
    "error": 4,
}

VALID_RISK_LEVELS = {"pass", "review", "block", "escalate", "error"}


@dataclass(frozen=True)
class RuleDecision:
    """Represents the deterministic sensitive-word/rule result for one item."""

    level: str = "pass"
    categories: List[str] = field(default_factory=list)
    matched_terms: List[Dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    confidence: Optional[float] = None
    policy_version: str = "moderation_rules_v1"


@dataclass(frozen=True)
class SyncModerationDecision:
    """Represents the low-latency outbound pre-send moderation decision."""

    allowed: bool
    level: str = "pass"
    categories: List[str] = field(default_factory=list)
    matched_terms: List[Dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    policy_version: str = "moderation_rules_v1"


@dataclass(frozen=True)
class InboundScreenDecision:
    """Synchronous inbound screening result that gates the current turn's reply."""

    allowed: bool
    level: str = "pass"
    categories: List[str] = field(default_factory=list)
    task_id: Optional[str] = None
    reason: str = ""
    degraded: bool = False  # True 表示阿里云调用失败、已降级为本地规则判定


@dataclass(frozen=True)
class SamplingDecision:
    """Records whether a task is selected for asynchronous LLM review."""

    run_llm_review: bool
    sample_rate_percent: int
    sampling_reason: str
    policy_version: str


@dataclass(frozen=True)
class MachineReviewResult:
    """Normalized result from one machine moderation reviewer."""

    reviewer_type: str
    engine: str
    engine_version: str = ""
    level: str = "pass"
    categories: List[str] = field(default_factory=list)
    confidence: Optional[float] = None
    matched_terms: List[Dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    raw_result: Dict[str, Any] = field(default_factory=dict)
    latency_ms: Optional[int] = None
    error: Optional[str] = None


def normalize_risk_level(value: Optional[str], *, default: str = "pass") -> str:
    """Return a supported risk level, falling back to *default* for bad input."""

    level = str(value or "").strip().lower()
    if level in VALID_RISK_LEVELS:
        return level
    return default


def max_risk_level(levels: List[str]) -> str:
    """Return the highest-severity moderation level from *levels*."""

    best = "pass"
    for level in levels:
        normalized = normalize_risk_level(level)
        if RISK_LEVEL_ORDER[normalized] > RISK_LEVEL_ORDER[best]:
            best = normalized
    return best
