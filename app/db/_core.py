import hashlib
import json
import logging
import math
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.config import settings
from app.db._backend import Connection, close_pg_pool, is_postgres
from app.time_utils import beijing_now_str

# 迁移函数体已按产品拆分到 app/db/migrations/*，此处整体回导出：
# _MIGRATIONS 有序链、以及 tests / app 内按名字 import 的既有契约都保持不变。
from app.db._schema_utils import (
    _ensure_column,
    _table_exists,
    product_quota_subject,
)
from app.db.migrations.mingchan import (
    _migration_0028_companion_world_core,
    _migration_0029_universe_memory_l3,
    _migration_0030_companion_world_candidates,
    _migration_0033_companion_world_m3_content,
    _migration_0034_companion_world_lifecycle_mailbox,
    _migration_0035_companion_world_visit_human_chat,
    _migration_0047_legacy_template_display_name,
    _migration_0048_companion_world_resident_drafts,
    _migration_0049_companion_world_naming,
    _migration_0050_ai_conversation_read_cursor,
    _migration_0052_human_conversation_read_cursor,
    _migration_0053_resident_intro_post,
    _migration_0055_universe_post_media,
    _migration_0057_resident_wish_drafts,
    _migration_0058_async_resident_wishes,
    _migration_0059_creator_role_templates,
    _migration_0060_creator_role_template_opening_and_summary,
    _migration_0061_companion_world_product_scope,
    _migration_0062_mingchan_notification_product_scope,
    _migration_0063_companion_world_owner_product_unique,
)
from app.db.migrations.plum import (
    _migration_0064_fibre_mvp,
    _migration_0065_fibre_character_experience,
    _migration_0066_fibre_public_test_auth,
    _migration_0067_plum_product_rename,
)
from app.db.migrations.shared import (
    _billing_contract_violation_counts,
    _billing_idempotency_contract_violation_counts,
    _drop_pg_global_billing_idempotency_constraints,
    _identity_contract_violation_counts,
    _migration_0001_baseline,
    _migration_0002_llm_runtime_config,
    _migration_0003_user_meta,
    _migration_0004_account_profile_files,
    _migration_0005_rpm_hits,
    _migration_0006_merge_reactivation_categories,
    _migration_0007_merge_reactivation_settings_keys,
    _migration_0008_relationship_state,
    _migration_0009_messages_account_id_index,
    _migration_0010_sessions_rolling_summary,
    _migration_0011_proactive_global_candidates,
    _migration_0012_agent_mission,
    _migration_0013_campaign_codes,
    _migration_0019_campaign_ai_name_preset,
    _migration_0020_campaign_visits,
    _migration_0021_dynamic_reminders,
    _migration_0022_account_app_id,
    _migration_0023_owner_binding_active_unique,
    _migration_0024_rename_channel_app_to_native,
    _migration_0025_wallet_unique_platform_user,
    _migration_0026_daily_usage_platform_user,
    _migration_0027_daily_quota_reservations,
    _migration_0031_platform_user_quota_overrides,
    _migration_0032_rpm_hit_double_precision,
    _migration_0036_repair_account_app_id,
    _migration_0037_product_memberships,
    _migration_0038_session_app_id,
    _migration_0039_billing_app_id_expand,
    _migration_0040_billing_app_id_contract,
    _migration_0041_quota_app_id_expand,
    _migration_0042_quota_app_id_contract,
    _migration_0043_referral_app_id_expand,
    _migration_0044_referral_app_id_contract,
    _migration_0045_multi_product_phase1_contract,
    _migration_0046_billing_idempotency_contract,
    _migration_0051_app_me_tab,
    _migration_0054_media_assets,
    _migration_0056_media_moderation_scan_index,
    _migration_0068_runtime_turn_runs,
    _migration_0069_runtime_turn_cancellation,
    _phase1_contract_violation_counts,
    _quota_contract_violation_counts,
    _rebuild_billing_idempotency_sqlite,
    _rebuild_referral_relationships_sqlite,
    _referral_contract_violation_counts,
)

logger = logging.getLogger("ai4all.db")

_UNSET = object()
_NON_CONTEXT_ASSISTANT_REPLY = "我这边刚刚有点卡住了，你可以稍后再发我一次。"
# 入站内容被同步审核拦截后写入 messages.error 的标记；用于将命中原文从所有 LLM 上下文路径中剔除。
MODERATION_BLOCKED_ERROR = "moderation_blocked"
ACCOUNT_ACTIVE_SESSION_KEY = "__account_active__"
# Web 渠道短期会话隔离键（conversation_scope，§7.1）。与 app.platform.channels 的 web cap
# active_session_key 取值必须一致（两处各留一份字面量以避免 channels↔db 循环依赖）。
WEB_ACTIVE_SESSION_KEY = "__web_active__"
# App 原生渠道短期会话隔离键；与 app.platform.channels 的 app cap 保持字面量一致，避免反向依赖。
APP_ACTIVE_SESSION_KEY = "__app_active__"
# dreaming 每日轮转默认扫描的所有合法 active scope。新增 scope 时在此登记，否则该 scope
# 的 active session 永不轮转/dreaming（§7.1 / Codex ②）。
DEFAULT_ACTIVE_SESSION_KEYS = (
    ACCOUNT_ACTIVE_SESSION_KEY,
    WEB_ACTIVE_SESSION_KEY,
    APP_ACTIVE_SESSION_KEY,
)
_LEGACY_DEFAULT_ASSISTANT_NAMES = {"AI4ALL 助手"}
SHELL_MICROS_PER_SHELL = 1_000_000
SHELL_BILLABLE_TOKENS_PER_SHELL = 1000
NEW_USER_GRANT_SHELLS = 1000
NEW_USER_GRANT_SHELL_MICROS = NEW_USER_GRANT_SHELLS * SHELL_MICROS_PER_SHELL
REFERRAL_REWARD_SHELLS = 1000
REFERRAL_REWARD_SHELL_MICROS = REFERRAL_REWARD_SHELLS * SHELL_MICROS_PER_SHELL
REFERRAL_QUALIFYING_MESSAGE_COUNT = 3
REFERRAL_SOFT_REVIEW_WINDOW_DAYS = 7
REFERRAL_SOFT_REVIEW_REGISTRATION_LIMIT = 5
REFERRAL_SOFT_REVIEW_REWARD_DELAY_DAYS = 3
_ACCOUNT_ID_RANDOM_MIN = 100_000_000
_ACCOUNT_ID_RANDOM_SPACE = 900_000_000
_ACCOUNT_ID_GENERATION_RETRIES = 20
_REFERRAL_CODE_GENERATION_RETRIES = 20


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def advisory_lock_key(subject: str) -> int:
    """把任意字符串主体映射成稳定的 64 位有符号整数，作 pg_advisory_xact_lock 的键。

    用 blake2b 取 8 字节（跨进程稳定、碰撞极低）。与 rate_limiter._advisory_key 同一算法，
    抽到 _core 供配额预占（accounts.reserve_daily_quota）与 RPM 共用，避免 app.db → rate_limiter
    循环导入。偶发碰撞只让两个无关主体短暂互斥，仅轻微竞争、不影响正确性。
    """
    digest = hashlib.blake2b(subject.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)




def _new_account_id() -> str:
    return f"aid_{_ACCOUNT_ID_RANDOM_MIN + uuid.uuid4().int % _ACCOUNT_ID_RANDOM_SPACE}"


def _clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_referral_code(code: Optional[str]) -> Optional[str]:
    cleaned = _clean_text(code)
    if not cleaned:
        return None
    return re.sub(r"\s+", "", cleaned).upper()


def _new_referral_code() -> str:
    return uuid.uuid4().hex[:8].upper()


def _clean_default_account_display_name(value: Optional[str]) -> Optional[str]:
    cleaned = _clean_text(value)
    if cleaned in _LEGACY_DEFAULT_ASSISTANT_NAMES:
        return None
    return cleaned


def _format_shell_amount(amount_shell_micros: int) -> str:
    sign = "-" if amount_shell_micros < 0 else ""
    amount = abs(int(amount_shell_micros))
    whole = amount // SHELL_MICROS_PER_SHELL
    fraction = amount % SHELL_MICROS_PER_SHELL
    if fraction == 0:
        return f"{sign}{whole}"
    return f"{sign}{whole}.{fraction:06d}".rstrip("0")


def _channel_account_id_aliases(value: str) -> List[str]:
    """Return known OpenClaw/provider account id variants for lookup.

    openclaw-weixin's QR wait result returns the raw ilink bot id such as
    `abc@im.bot`, while OpenClaw runtime context commonly uses its normalized
    account id form `abc-im-bot`.
    """
    cleaned = _clean_text(value)
    if not cleaned:
        return []
    aliases = [cleaned]
    if cleaned.endswith("@im.bot"):
        aliases.append(f"{cleaned[:-7]}-im-bot")
    elif cleaned.endswith("-im-bot"):
        aliases.append(f"{cleaned[:-7]}@im.bot")
    if cleaned.endswith("@im.wechat"):
        aliases.append(f"{cleaned[:-10]}-im-wechat")
    elif cleaned.endswith("-im-wechat"):
        aliases.append(f"{cleaned[:-10]}@im.wechat")
    return list(dict.fromkeys(aliases))


_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


def _normalize_phone(phone: str) -> str:
    normalized = str(phone or "")
    for ch in (" ", "-", "(", ")", "."):
        normalized = normalized.replace(ch, "")
    normalized = normalized.strip()
    if not _PHONE_RE.match(normalized):
        raise ValueError("phone must be a valid Chinese mobile number (1[3-9]XXXXXXXXX)")
    return normalized


def _settings():
    """返回实时 settings 对象。

    通过包命名空间 `app.db.settings` 动态读取，使测试中 `patch("app.db.settings")`
    能路由整个 db 层到临时工作区（拆包后各子模块的模块级 settings 名字在 import 期
    已绑定，直接用会绕过 patch）。
    """
    import app.db as _pkg
    return getattr(_pkg, "settings", settings)


def _db_path() -> Path:
    path = Path(_settings().database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect() -> Iterator[Connection]:
    """打开 DB 连接（后端由 settings.database_url 决定），成功提交、异常回滚。

    连接的获取/方言差异收敛在 app.db._backend；本函数只负责事务边界，两套后端
    （SQLite 默认 / PostgreSQL）共用同一套 commit/rollback/close 语义。
    """
    from app.db import _backend

    conn = _backend.connect_raw()
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except Exception as rollback_err:
            logger.warning("db rollback failed after exception: %s", rollback_err)
        raise
    else:
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _tx(conn: Optional[Connection]) -> Iterator[Connection]:
    """复用调用方事务（传入 conn，由调用方负责提交/回滚）或自开一个独立事务。

    用于让 unbind/wipe 既能各自独立调用，又能被 unbind_and_wipe_account 串进
    同一个事务，保证「要么全成、要么整体回滚」。
    """
    if conn is not None:
        yield conn
        return
    with connect() as own:
        yield own


@contextmanager
def _savepoint(conn: Connection, name: str = "sp") -> Iterator[None]:
    """子事务保存点：把可能触发唯一冲突的语句隔离起来，冲突时只回滚到保存点而非整笔事务。

    PG 在任一语句报错后会中止整个事务，后续语句一律 InFailedSqlTransaction；因此
    「INSERT 失败 → 捕获 IntegrityError → 同一连接继续重试/查询」的模式在 PG 必须靠
    SAVEPOINT 才能继续（SQLite 同样支持 SAVEPOINT，两后端行为一致）。

    SQLite 上必须先显式开事务：Python sqlite3 只在 DML 前隐式 BEGIN，``SAVEPOINT`` 不算
    DML，于是它成为**最外层**保存点，对应的 ``RELEASE`` 会直接提交——外层 ``connect()``
    之后再 rollback 就什么也回滚不掉（表现为「认领失败了但消息还在」）。PG 侧连接恒为
    ``autocommit=False``，事务一直开着，不需要也不能再 BEGIN。
    """
    if not is_postgres() and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {name}")






# 应用级事务 advisory lock ID，用于串行化多节点并发启动时的 PG 迁移
_PG_MIGRATION_LOCK_ID = 7_483_920
_PHASE1_CONTRACT_MIGRATION_VERSIONS = (40, 42, 44, 45, 46)


def _guard_automatic_phase1_contract_migrations(conn: Connection) -> None:
    """拒绝既有 PG 库由常规应用启动跨越 Phase 1 contract 检查点。"""
    _ensure_schema_migrations_table(conn)
    current = _applied_schema_version(conn)
    # 只有版本 0 且核心业务表尚不存在才是真空库；版本表丢失不能绕过 interlock。
    if current == 0 and not _table_exists(conn, "accounts"):
        return
    pending_contracts = [
        version
        for version in _PHASE1_CONTRACT_MIGRATION_VERSIONS
        if current < version
    ]
    if pending_contracts:
        pending = ",".join(str(version) for version in pending_contracts)
        raise RuntimeError(
            "automatic Phase 1 contract migration blocked: "
            f"current={current}, pending={pending}; drain all writers and use "
            "scripts/migrate_multi_product_phase1.py --through <version>"
        )


# PG 上「首次应用迁移」必须是受控部署动作的 opt-in 环境变量。
# 背景：生产机同时是开发机，`.env` 指向生产 PG，而 pydantic 的 env_file 让**任何**在仓库
# 目录里手跑的 python 进程都读到它。于是自测时顺手跑一条会触发 init_db 的命令，就把尚未
# 评审的迁移写进了生产库（2026-07-16 / 07-29 / 07-30 三次，皆无害但均属意外）。
# 该变量只写进 systemd 单元的 ``Environment=``——**绝不能写进 `.env`**，否则同目录的手跑
# 进程会一起继承，闸门等于不存在。
AUTO_MIGRATE_ENV = "AI4ALL_ALLOW_AUTO_MIGRATE"
_TRUTHY = {"1", "true", "yes", "on"}


def auto_migrate_allowed() -> bool:
    """当前进程是否获准在 PG 上应用待执行迁移。"""
    return (os.getenv(AUTO_MIGRATE_ENV, "") or "").strip().lower() in _TRUTHY


def _guard_unattended_pg_migrations(conn: Connection) -> None:
    """未显式 opt-in 的进程不得在 PG 上应用待执行迁移（无待执行迁移时不拦）。"""
    if auto_migrate_allowed():
        return
    _ensure_schema_migrations_table(conn)
    current = _applied_schema_version(conn)
    pending = [version for version, _apply in _MIGRATIONS if version > current]
    if not pending:
        return
    raise RuntimeError(
        "unattended PostgreSQL migration blocked: "
        f"current={current}, pending={','.join(str(v) for v in pending)}; "
        f"迁移只应由受控部署应用（systemd 单元已带 {AUTO_MIGRATE_ENV}=1）。"
        f"本地自测请改用 SQLite（DATABASE_URL=\"\"）或临时库；确需手工迁移生产库时显式加 "
        f"{AUTO_MIGRATE_ENV}=1 前缀。"
    )


def init_db() -> None:
    """应用所有待执行的 schema 迁移（版本由 schema_migrations 表跟踪）。

    PG 后端：用事务级 advisory lock 防止多节点同时 DDL。持锁节点完成迁移后提交释放，
    后续节点持锁时迁移已全部应用，幂等跳过。既有 PG 库不得由应用启动跨越
    Phase 1 contract，必须使用受控逐闸 runner；空库与 SQLite 路径不受影响。
    另有 opt-in 闸：PG 上有待执行迁移时，只有受控部署进程可以应用（见 AUTO_MIGRATE_ENV）。
    """
    with connect() as conn:
        if is_postgres():
            conn.execute(f"SELECT pg_advisory_xact_lock({_PG_MIGRATION_LOCK_ID})")
            _guard_automatic_phase1_contract_migrations(conn)
            _guard_unattended_pg_migrations(conn)
        _run_migrations(conn)
        if is_postgres():
            _ensure_pg_functions(conn)


def migrate_db_through(
    *, target_version: int, expected_current_version: int
) -> Dict[str, int]:
    """在全局迁移锁内把数据库推进到指定版本，并拒绝意外起始版本。

    仅供需要 expand/reconcile/contract 检查点的受控发布脚本使用；正常应用启动仍调用
    ``init_db`` 追到最新版本。
    """
    known_versions = {version for version, _apply in _MIGRATIONS}
    if target_version not in known_versions:
        raise ValueError(f"unknown migration target: {target_version}")
    with connect() as conn:
        if is_postgres():
            conn.execute(f"SELECT pg_advisory_xact_lock({_PG_MIGRATION_LOCK_ID})")
        _ensure_schema_migrations_table(conn)
        current = _applied_schema_version(conn)
        if current == target_version:
            return {"before": current, "after": current}
        if current != expected_current_version:
            raise RuntimeError(
                "migration start version mismatch: "
                f"expected={expected_current_version}, actual={current}, "
                f"target={target_version}"
            )
        _run_migrations(conn, target_version=target_version)
        if is_postgres():
            _ensure_pg_functions(conn)
        after = _applied_schema_version(conn)
        if after != target_version:
            raise RuntimeError(
                f"migration target not reached: target={target_version}, actual={after}"
            )
        return {"before": current, "after": after}


# SQLite 内置 json_patch（RFC 7396 JSON Merge Patch）；PG 无内置实现。这里建一个同名
# 递归函数，严格保留「patch 值为 null 即删除该键、对象递归合并」语义——浅 || 合并做不到。
# 垫片层把 json_patch(A, ?) 翻成 json_patch(A::jsonb, (?)::jsonb)::text 调用本函数。
_PG_JSON_PATCH_FN = """
CREATE OR REPLACE FUNCTION json_patch(target jsonb, patch jsonb)
RETURNS jsonb AS $$
DECLARE
    result jsonb;
    k text;
    v jsonb;
BEGIN
    IF patch IS NULL OR jsonb_typeof(patch) <> 'object' THEN
        RETURN patch;
    END IF;
    IF target IS NULL OR jsonb_typeof(target) <> 'object' THEN
        result := '{}'::jsonb;
    ELSE
        result := target;
    END IF;
    FOR k, v IN SELECT * FROM jsonb_each(patch) LOOP
        IF v = 'null'::jsonb THEN
            result := result - k;
        ELSE
            result := result || jsonb_build_object(k, json_patch(result -> k, v));
        END IF;
    END LOOP;
    RETURN result;
END;
$$ LANGUAGE plpgsql IMMUTABLE;
"""


_PG_JSON_VALID_FN = """
CREATE OR REPLACE FUNCTION json_valid(val text)
RETURNS boolean AS $$
BEGIN
    IF val IS NULL THEN RETURN false; END IF;
    PERFORM val::jsonb;
    RETURN true;
EXCEPTION WHEN OTHERS THEN
    RETURN false;
END;
$$ LANGUAGE plpgsql IMMUTABLE;
"""

# safe_json_extract_text: SQLite json_extract(col, '$.a.b') 的 PG 安全替代。
# 脏 JSON 时返回 NULL 而非抛异常（::jsonb 强转遇非法 JSON 会直接报错）。
_PG_SAFE_JSON_EXTRACT_FN = """
CREATE OR REPLACE FUNCTION safe_json_extract_text(val text, path text[])
RETURNS text AS $$
BEGIN
    RETURN val::jsonb #>> path;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END;
$$ LANGUAGE plpgsql IMMUTABLE;
"""


def _ensure_pg_functions(conn: Connection) -> None:
    """在 PG 后端建立 SQLite 专有但本仓 SQL 依赖的函数（幂等 CREATE OR REPLACE）。"""
    conn.execute(_PG_JSON_PATCH_FN)
    conn.execute(_PG_JSON_VALID_FN)
    conn.execute(_PG_SAFE_JSON_EXTRACT_FN)


def _ensure_schema_migrations_table(conn: Connection) -> None:
    """建立跨后端通用的迁移版本表（取代 SQLite 专属的 PRAGMA user_version）。

    applied_at 由 Python 侧写入北京时间串，避免在引导表上引入方言默认值。
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )


def _applied_schema_version(conn: Connection) -> int:
    """返回已应用的最大迁移版本。

    历史 SQLite 库用 PRAGMA user_version 记录版本，schema_migrations 表为空；
    首次升级时把旧 user_version 回填进新表，避免幂等迁移被重复执行。
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations"
    ).fetchone()
    current = (row["v"] if row is not None else 0) or 0
    if current == 0 and not is_postgres():
        legacy = conn.execute("PRAGMA user_version").fetchone()[0]
        if legacy and int(legacy) > 0:
            now = beijing_now_str()
            for ver in range(1, int(legacy) + 1):
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
                    "VALUES (?, ?)",
                    (ver, now),
                )
            current = int(legacy)
    return int(current)


def _run_migrations(
    conn: Connection, *, target_version: Optional[int] = None
) -> None:
    """按版本顺序应用未执行迁移；在调用方事务内运行，整体提交/回滚。"""
    _ensure_schema_migrations_table(conn)
    current = _applied_schema_version(conn)
    for version, apply in _MIGRATIONS:
        if version > current and (target_version is None or version <= target_version):
            logger.info("db migration applying version=%s", version)
            apply(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, beijing_now_str()),
            )




















































































































































_MIGRATIONS = [
    (1, _migration_0001_baseline),
    (2, _migration_0002_llm_runtime_config),
    (3, _migration_0003_user_meta),
    (4, _migration_0004_account_profile_files),
    (5, _migration_0005_rpm_hits),
    (6, _migration_0006_merge_reactivation_categories),
    (7, _migration_0007_merge_reactivation_settings_keys),
    (8, _migration_0008_relationship_state),
    (9, _migration_0009_messages_account_id_index),
    (10, _migration_0010_sessions_rolling_summary),
    (11, _migration_0011_proactive_global_candidates),
    (12, _migration_0012_agent_mission),
    (13, _migration_0013_campaign_codes),
    (19, _migration_0019_campaign_ai_name_preset),
    (20, _migration_0020_campaign_visits),
    (21, _migration_0021_dynamic_reminders),
    (22, _migration_0022_account_app_id),
    (23, _migration_0023_owner_binding_active_unique),
    (24, _migration_0024_rename_channel_app_to_native),
    (25, _migration_0025_wallet_unique_platform_user),
    (26, _migration_0026_daily_usage_platform_user),
    (27, _migration_0027_daily_quota_reservations),
    (28, _migration_0028_companion_world_core),
    (29, _migration_0029_universe_memory_l3),
    (30, _migration_0030_companion_world_candidates),
    (31, _migration_0031_platform_user_quota_overrides),
    (32, _migration_0032_rpm_hit_double_precision),
    (33, _migration_0033_companion_world_m3_content),
    (34, _migration_0034_companion_world_lifecycle_mailbox),
    (35, _migration_0035_companion_world_visit_human_chat),
    (36, _migration_0036_repair_account_app_id),
    (37, _migration_0037_product_memberships),
    (38, _migration_0038_session_app_id),
    (39, _migration_0039_billing_app_id_expand),
    (40, _migration_0040_billing_app_id_contract),
    (41, _migration_0041_quota_app_id_expand),
    (42, _migration_0042_quota_app_id_contract),
    (43, _migration_0043_referral_app_id_expand),
    (44, _migration_0044_referral_app_id_contract),
    (45, _migration_0045_multi_product_phase1_contract),
    (46, _migration_0046_billing_idempotency_contract),
    (47, _migration_0047_legacy_template_display_name),
    (48, _migration_0048_companion_world_resident_drafts),
    (49, _migration_0049_companion_world_naming),
    (50, _migration_0050_ai_conversation_read_cursor),
    (51, _migration_0051_app_me_tab),
    (52, _migration_0052_human_conversation_read_cursor),
    (53, _migration_0053_resident_intro_post),
    (54, _migration_0054_media_assets),
    (55, _migration_0055_universe_post_media),
    (56, _migration_0056_media_moderation_scan_index),
    (57, _migration_0057_resident_wish_drafts),
    (58, _migration_0058_async_resident_wishes),
    (59, _migration_0059_creator_role_templates),
    (60, _migration_0060_creator_role_template_opening_and_summary),
    (61, _migration_0061_companion_world_product_scope),
    (62, _migration_0062_mingchan_notification_product_scope),
    (63, _migration_0063_companion_world_owner_product_unique),
    (64, _migration_0064_fibre_mvp),
    (65, _migration_0065_fibre_character_experience),
    (66, _migration_0066_fibre_public_test_auth),
    (67, _migration_0067_plum_product_rename),
    (68, _migration_0068_runtime_turn_runs),
    (69, _migration_0069_runtime_turn_cancellation),
]


# ---------------------------------------------------------------------------
# Dreaming runs / memory items / memory events
# ---------------------------------------------------------------------------

def _decode_json_field(
    item: Dict[str, Any],
    *,
    source_field: str,
    target_field: str,
    default: Any,
) -> Dict[str, Any]:
    raw_json = item.pop(source_field, None)
    try:
        item[target_field] = json.loads(raw_json or json.dumps(default))
    except json.JSONDecodeError:
        item[target_field] = default
        item[f"{target_field}_decode_error"] = True
    return item
