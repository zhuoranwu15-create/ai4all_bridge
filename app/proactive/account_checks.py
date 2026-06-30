"""账号级主动消息的候选生成 + account_check 派发（门面）。

实现已按职责拆分到子包，本模块仅作门面再导出，保持对外 import 路径不变：
- 生成：`app.proactive.generation.{account_check, topic_followup, content_invitation}`
- 派发：`app.proactive.dispatch.account_check`
- 共享：`app.proactive.generation._shared`（候选常量 + 元数据读取 + 时间/会话小工具）

注意（account_check 休眠路径，有意设计）：account_check candidate（`decide_account_check_action`
读取的 `account_check_candidate`）**只由 admin 端点**经 `generate_account_check_candidate_draft`
+ `promote_account_check_candidate_draft` 人工产生；自动调度链路（`scan_due_proactive_account_checks`）
**不生成**它。因此 scheduler 的 account_checks 步在无人工候选时恒 `no_op`，这是预期行为，不是漏接。
自动主动消息当前真正在跑的是 commitment 与 reactivation（topic_followup / content_invitation）两条。
"""
# 保留 settings 绑定：历史测试以 patch("app.proactive.account_checks.settings", ...) 注入测试配置，
# 各子模块自身也 from app.config import settings（conftest 已按 per-module 约定逐个 patch）。
from app.config import settings  # noqa: F401  (门面保留，供既有 patch 目标解析)

from app.proactive.generation._shared import (  # noqa: F401  (re-export)
    ACCOUNT_CHECK_CANDIDATE_DRAFT_KEY,
    ACCOUNT_CHECK_CANDIDATE_KEY,
    ACCOUNT_CHECK_SOURCE,
    LEGACY_HEARTBEAT_CANDIDATE_DRAFT_KEY,
    LEGACY_HEARTBEAT_CANDIDATE_KEY,
)
from app.proactive.generation.account_check import (  # noqa: F401  (re-export)
    clear_account_check_candidate_draft,
    generate_account_check_candidate_draft,
    promote_account_check_candidate_draft,
)
from app.proactive.generation.content_invitation import (  # noqa: F401  (re-export)
    generate_content_invitation_candidate,
)
from app.proactive.generation.topic_followup import (  # noqa: F401  (re-export)
    generate_topic_followup_candidate,
)
from app.proactive.dispatch.account_check import (  # noqa: F401  (re-export)
    decide_account_check_action,
    execute_account_check_decision,
)

__all__ = [
    "ACCOUNT_CHECK_SOURCE",
    "ACCOUNT_CHECK_CANDIDATE_KEY",
    "ACCOUNT_CHECK_CANDIDATE_DRAFT_KEY",
    "LEGACY_HEARTBEAT_CANDIDATE_KEY",
    "LEGACY_HEARTBEAT_CANDIDATE_DRAFT_KEY",
    "decide_account_check_action",
    "execute_account_check_decision",
    "generate_account_check_candidate_draft",
    "generate_topic_followup_candidate",
    "generate_content_invitation_candidate",
    "promote_account_check_candidate_draft",
    "clear_account_check_candidate_draft",
]
