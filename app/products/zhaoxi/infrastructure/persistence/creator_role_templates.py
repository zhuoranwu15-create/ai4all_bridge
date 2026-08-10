"""用户角色模板的 owner-scoped 持久化与并发槽位分配。"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Mapping, Optional, Tuple

from app.db._backend import Connection, IntegrityError
from app.db._core import _savepoint, _tx
from app.products.zhaoxi.domain.creator_role_templates import (
    CREATOR_ROLE_TEMPLATE_CODE_GENERATION_RETRIES,
    CREATOR_ROLE_TEMPLATE_MAX_SLOTS,
    EVENT_METADATA_JSON_MAX_CHARS,
    DISABLED_REASON_MAX_CHARS,
    REVIEW_CATEGORIES_JSON_MAX_CHARS,
    REVIEW_REASON_MAX_CHARS,
    ZHAOXI_APP_ID,
    CreatedCreatorRoleTemplate,
    CreatorRoleTemplate,
    CreatorRoleTemplateActorType,
    CreatorRoleTemplateContent,
    CreatorRoleTemplateError,
    CreatorRoleTemplateEventType,
    CreatorRoleTemplateMutation,
    CreatorRoleTemplateReviewClaim,
    CreatorRoleTemplateReviewRunStatus,
    CreatorRoleTemplateReviewStatus,
    CreatorRoleTemplateStatus,
    CreatorRoleTemplateSummaryEditStatus,
    CreatorRoleTemplateSummaryReviewClaim,
    CreatorRoleTemplateVersion,
    is_creator_role_template_campaign_code,
    new_creator_role_template_campaign_code,
    normalize_creator_role_template_content,
    normalize_creator_role_template_summary,
)
from app.products.zhaoxi.infrastructure.profiles import (
    write_creator_role_template_snapshot,
)
from app.time_utils import beijing_now_str


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _require_scope(*, creator_platform_user_id: str, app_id: str) -> tuple[str, str]:
    owner_id = str(creator_platform_user_id or "").strip()
    cleaned_app_id = str(app_id or "").strip()
    if not owner_id:
        raise CreatorRoleTemplateError("creator_platform_user_id_required")
    if not cleaned_app_id:
        raise CreatorRoleTemplateError("app_id_required")
    return owner_id, cleaned_app_id


def _encode_metadata(metadata: Optional[Mapping[str, Any]]) -> str:
    try:
        encoded = json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise CreatorRoleTemplateError("creator_role_template_event_metadata_invalid") from exc
    if len(encoded) > EVENT_METADATA_JSON_MAX_CHARS:
        raise CreatorRoleTemplateError("creator_role_template_event_metadata_too_long")
    return encoded


def _insert_event(
    conn: Connection,
    *,
    template_id: str,
    version_id: Optional[str],
    event_type: CreatorRoleTemplateEventType,
    actor_type: CreatorRoleTemplateActorType,
    actor_id: Optional[str],
    metadata: Optional[Mapping[str, Any]],
    created_at: str,
) -> str:
    event_id = _new_id("crtevt")
    conn.execute(
        """
        INSERT INTO creator_role_template_events(
            id, creator_role_template_id, creator_role_template_version_id,
            event_type, actor_type, actor_id, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            template_id,
            version_id,
            event_type.value,
            actor_type.value,
            actor_id,
            _encode_metadata(metadata),
            created_at,
        ),
    )
    return event_id


def _find_template_row(
    conn: Connection,
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    include_deleted: bool,
):
    deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
    return conn.execute(
        """
        SELECT *
        FROM creator_role_templates
        WHERE id = ? AND creator_platform_user_id = ? AND app_id = ?
        """
        + deleted_clause,
        (template_id, creator_platform_user_id, app_id),
    ).fetchone()


def _lock_template_row(
    conn: Connection,
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
):
    """用无值变化 UPDATE 取得同一模板的行锁并返回 live 行。"""
    cursor = conn.execute(
        """
        UPDATE creator_role_templates SET updated_at = updated_at
        WHERE id = ? AND creator_platform_user_id = ? AND app_id = ?
          AND deleted_at IS NULL
        """,
        (template_id, creator_platform_user_id, app_id),
    )
    if cursor.rowcount != 1:
        raise CreatorRoleTemplateError("creator_role_template_not_found")
    return _find_template_row(
        conn,
        creator_platform_user_id=creator_platform_user_id,
        app_id=app_id,
        template_id=template_id,
        include_deleted=False,
    )


def _version_row(conn: Connection, *, template_id: str, version_id: str):
    return conn.execute(
        """
        SELECT * FROM creator_role_template_versions
        WHERE id = ? AND creator_role_template_id = ?
        """,
        (version_id, template_id),
    ).fetchone()


def _encode_categories(categories: List[str]) -> str:
    encoded = json.dumps(categories, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > REVIEW_CATEGORIES_JSON_MAX_CHARS:
        raise CreatorRoleTemplateError("role_review_categories_too_long")
    return encoded


def create_creator_role_template(
    *,
    creator_platform_user_id: str,
    app_id: str,
    ai_name: str,
    personality_text: str,
    mission_text: str,
    opening_line: str,
    conn: Optional[Connection] = None,
) -> CreatedCreatorRoleTemplate:
    """原子申请 1–3 的空闲槽位，并创建模板、v1 pending 版本和 created 事件。

    槽位不通过 ``COUNT`` 预判；每个候选槽位都直接受 live 部分唯一索引仲裁。
    code 冲突与槽位冲突在 savepoint 回滚后分别识别，前者重生成、后者尝试下一槽。
    """
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    if cleaned_app_id != ZHAOXI_APP_ID:
        raise CreatorRoleTemplateError("creator_role_template_app_not_supported")
    content: CreatorRoleTemplateContent = normalize_creator_role_template_content(
        ai_name=ai_name,
        personality_text=personality_text,
        mission_text=mission_text,
        opening_line=opening_line,
    )

    with _tx(conn) as tx:
        for slot_no in range(1, CREATOR_ROLE_TEMPLATE_MAX_SLOTS + 1):
            for code_attempt in range(CREATOR_ROLE_TEMPLATE_CODE_GENERATION_RETRIES):
                campaign_code = new_creator_role_template_campaign_code()
                if not is_creator_role_template_campaign_code(campaign_code):
                    raise CreatorRoleTemplateError(
                        "creator_role_template_code_generation_failed"
                    )
                template_id = _new_id("crtpl")
                now = beijing_now_str()
                try:
                    with _savepoint(tx, f"crtpl_slot_{slot_no}_{code_attempt}"):
                        tx.execute(
                            """
                            INSERT INTO creator_role_templates(
                                id, app_id, creator_platform_user_id, slot_no,
                                campaign_code, status, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                template_id,
                                cleaned_app_id,
                                owner_id,
                                slot_no,
                                campaign_code,
                                CreatorRoleTemplateStatus.PENDING_REVIEW.value,
                                now,
                                now,
                            ),
                        )
                except IntegrityError:
                    code_taken = tx.execute(
                        "SELECT 1 FROM creator_role_templates WHERE campaign_code = ?",
                        (campaign_code,),
                    ).fetchone()
                    if code_taken is not None:
                        continue
                    slot_taken = tx.execute(
                        """
                        SELECT 1 FROM creator_role_templates
                        WHERE creator_platform_user_id = ? AND app_id = ?
                          AND slot_no = ? AND deleted_at IS NULL
                        """,
                        (owner_id, cleaned_app_id, slot_no),
                    ).fetchone()
                    if slot_taken is not None:
                        break
                    raise

                version_id = _new_id("crtv")
                tx.execute(
                    """
                    INSERT INTO creator_role_template_versions(
                        id, creator_role_template_id, version_no,
                        ai_name, personality_text, mission_text, opening_line,
                        review_status, is_published, created_at, updated_at
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        version_id,
                        template_id,
                        content.ai_name,
                        content.personality_text,
                        content.mission_text,
                        content.opening_line,
                        CreatorRoleTemplateReviewStatus.PENDING.value,
                        now,
                        now,
                    ),
                )
                _insert_event(
                    tx,
                    template_id=template_id,
                    version_id=version_id,
                    event_type=CreatorRoleTemplateEventType.CREATED,
                    actor_type=CreatorRoleTemplateActorType.CREATOR,
                    actor_id=owner_id,
                    metadata={"slot_no": slot_no, "version_no": 1},
                    created_at=now,
                )
                template_row = _find_template_row(
                    tx,
                    creator_platform_user_id=owner_id,
                    app_id=cleaned_app_id,
                    template_id=template_id,
                    include_deleted=False,
                )
                version_row = tx.execute(
                    "SELECT * FROM creator_role_template_versions WHERE id = ?",
                    (version_id,),
                ).fetchone()
                return CreatedCreatorRoleTemplate(
                    template=CreatorRoleTemplate.from_row(template_row),
                    version=CreatorRoleTemplateVersion.from_row(version_row),
                )
            else:
                raise CreatorRoleTemplateError(
                    "creator_role_template_code_generation_failed"
                )

        raise CreatorRoleTemplateError("creator_role_template_limit_reached")


def get_creator_role_template(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    include_deleted: bool = False,
    conn: Optional[Connection] = None,
) -> Optional[CreatorRoleTemplate]:
    """按 owner + app + template 三重范围读取；越权与不存在统一返回 None。"""
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    cleaned_template_id = str(template_id or "").strip()
    if not cleaned_template_id:
        return None
    with _tx(conn) as tx:
        row = _find_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=cleaned_template_id,
            include_deleted=include_deleted,
        )
    return CreatorRoleTemplate.from_row(row) if row is not None else None


def list_creator_role_templates(
    *,
    creator_platform_user_id: str,
    app_id: str,
    include_deleted: bool = False,
    limit: int = 100,
    conn: Optional[Connection] = None,
) -> List[CreatorRoleTemplate]:
    """列出指定 owner 在朝夕下的模板，默认排除已软删除资产。"""
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    safe_limit = max(1, min(int(limit or 100), 500))
    deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
    with _tx(conn) as tx:
        rows = tx.execute(
            """
            SELECT * FROM creator_role_templates
            WHERE creator_platform_user_id = ? AND app_id = ?
            """
            + deleted_clause
            + " ORDER BY slot_no ASC, created_at ASC LIMIT ?",
            (owner_id, cleaned_app_id, safe_limit),
        ).fetchall()
    return [CreatorRoleTemplate.from_row(row) for row in rows]


def get_creator_role_template_for_admin(
    *,
    app_id: str,
    template_id: str,
    include_deleted: bool = True,
    conn: Optional[Connection] = None,
) -> Optional[CreatorRoleTemplate]:
    """在固定产品范围内供 Admin/Staff 读取模板，不信任客户端提供 owner。"""
    cleaned_app_id = str(app_id or "").strip()
    cleaned_template_id = str(template_id or "").strip()
    if cleaned_app_id != ZHAOXI_APP_ID or not cleaned_template_id:
        return None
    deleted_clause = "" if include_deleted else " AND deleted_at IS NULL"
    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT * FROM creator_role_templates
            WHERE id = ? AND app_id = ?
            """
            + deleted_clause,
            (cleaned_template_id, cleaned_app_id),
        ).fetchone()
    return CreatorRoleTemplate.from_row(row) if row is not None else None


def list_creator_role_templates_for_admin(
    *,
    app_id: str,
    creator_platform_user_id: Optional[str] = None,
    effective_status: Optional[str] = None,
    review_status: Optional[str] = None,
    created_from: Optional[str] = None,
    created_to_exclusive: Optional[str] = None,
    now: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    conn: Optional[Connection] = None,
) -> Tuple[List[CreatorRoleTemplate], int]:
    """按产品分页筛选全部用户模板；返回当前页和过滤后的总数。

    ``effective_status`` 在 SQL 中使用与 application 投影相同的优先级，确保过期模板
    不会继续出现在 active 等持久化状态筛选结果中。
    """
    cleaned_app_id = str(app_id or "").strip()
    if cleaned_app_id != ZHAOXI_APP_ID:
        raise CreatorRoleTemplateError("creator_role_template_app_not_supported")
    safe_limit = max(1, min(int(limit or 50), 100))
    safe_offset = max(0, int(offset or 0))
    conditions = ["t.app_id = ?"]
    values: List[Any] = [cleaned_app_id]
    owner_id = str(creator_platform_user_id or "").strip()
    if owner_id:
        conditions.append("t.creator_platform_user_id = ?")
        values.append(owner_id)
    current = str(now or beijing_now_str())
    if effective_status:
        conditions.append(
            """
            CASE
              WHEN t.deleted_at IS NOT NULL OR t.status = 'deleted' THEN 'deleted'
              WHEN t.status = 'disabled_admin' THEN 'disabled_admin'
              WHEN t.expires_at IS NOT NULL AND t.expires_at <= ? THEN 'expired'
              ELSE t.status
            END = ?
            """
        )
        values.extend((current, effective_status))
    if review_status:
        conditions.append(
            """
            (SELECT v.review_status
             FROM creator_role_template_versions AS v
             WHERE v.creator_role_template_id = t.id
             ORDER BY v.version_no DESC LIMIT 1) = ?
            """
        )
        values.append(review_status)
    if created_from:
        conditions.append("t.created_at >= ?")
        values.append(created_from)
    if created_to_exclusive:
        conditions.append("t.created_at < ?")
        values.append(created_to_exclusive)
    where_clause = " AND ".join(conditions)
    with _tx(conn) as tx:
        count_row = tx.execute(
            "SELECT COUNT(*) AS total FROM creator_role_templates AS t WHERE "
            + where_clause,
            tuple(values),
        ).fetchone()
        rows = tx.execute(
            "SELECT t.* FROM creator_role_templates AS t WHERE "
            + where_clause
            + " ORDER BY t.created_at DESC, t.id DESC LIMIT ? OFFSET ?",
            tuple(values + [safe_limit, safe_offset]),
        ).fetchall()
    return (
        [CreatorRoleTemplate.from_row(row) for row in rows],
        int(count_row["total"] if count_row is not None else 0),
    )


def get_creator_role_template_version(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    version_id: str,
    include_deleted_template: bool = False,
    conn: Optional[Connection] = None,
) -> Optional[CreatorRoleTemplateVersion]:
    """在 owner/app/template 范围内读取确切版本，避免仅凭 version id 越权。"""
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    deleted_clause = "" if include_deleted_template else " AND t.deleted_at IS NULL"
    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT v.*
            FROM creator_role_template_versions AS v
            JOIN creator_role_templates AS t ON t.id = v.creator_role_template_id
            WHERE t.id = ? AND t.creator_platform_user_id = ? AND t.app_id = ?
              AND v.id = ?
            """
            + deleted_clause,
            (template_id, owner_id, cleaned_app_id, version_id),
        ).fetchone()
    return CreatorRoleTemplateVersion.from_row(row) if row is not None else None


def get_account_creator_role_template_attribution(
    *,
    account_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """按 ``account_id`` 读取不可变模板实例化快照；未归因账号返回 ``None``。"""
    cleaned_account_id = str(account_id or "").strip()
    if not cleaned_account_id:
        return None
    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT *
            FROM account_creator_role_template_attribution
            WHERE account_id = ?
            """,
            (cleaned_account_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def get_creator_role_template_by_campaign_code(
    *,
    campaign_code: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """按大小写敏感的稳定 code 读取模板及当前 published 版本。

    该方法用于注册分派和匿名曝光存在性检查，不承担 owner 授权；创建者管理 API
    仍必须使用 owner + app + template 三重范围方法。
    """
    cleaned_code = str(campaign_code or "").strip()
    if not cleaned_code:
        return None
    with _tx(conn) as tx:
        row = tx.execute(
            """
            SELECT
                t.*,
                v.id AS published_version_id,
                v.version_no AS published_version_no,
                v.ai_name AS published_ai_name,
                v.personality_text AS published_personality_text,
                v.mission_text AS published_mission_text,
                v.opening_line AS published_opening_line,
                v.public_summary AS published_public_summary,
                v.review_status AS published_review_status,
                v.published_at AS version_published_at
            FROM creator_role_templates AS t
            LEFT JOIN creator_role_template_versions AS v
              ON v.creator_role_template_id = t.id AND v.is_published = 1
            WHERE t.campaign_code = ?
            """,
            (cleaned_code,),
        ).fetchone()
    return dict(row) if row is not None else None


def apply_creator_role_template_attribution(
    *,
    account_id: str,
    campaign_code: str,
    expected_creator_platform_user_id: str,
    attributed_at: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """原子实例化已发布模板到一个朝夕账号。

    通用 campaign 归因、模板版本快照、三份 profile、``used_count`` 与审计事件
    共享同一事务。业务失效返回稳定 reason；意外异常抛给上层 dispatcher 统一
    记录并 fail-open，绝不由本方法吞掉半成品写入。
    """
    cleaned_account_id = str(account_id or "").strip()
    cleaned_code = str(campaign_code or "").strip()
    expected_creator_id = str(expected_creator_platform_user_id or "").strip()
    if not cleaned_account_id:
        raise CreatorRoleTemplateError("account_id_required")
    if not cleaned_code:
        return {"applied": False, "reason": "empty"}
    if not expected_creator_id:
        return {"applied": False, "reason": "creator_required"}

    now = attributed_at or beijing_now_str()
    with _tx(conn) as tx:
        # 无值变化 UPDATE 取得模板写锁，避免停用、删除、发布新版
        # 与实例化互相穿透；同一模板的 used_count 也因此不会丢更新。
        locked = tx.execute(
            """
            UPDATE creator_role_templates SET updated_at = updated_at
            WHERE campaign_code = ?
            """,
            (cleaned_code,),
        )
        if locked.rowcount != 1:
            return {"applied": False, "reason": "not_found"}

        source = tx.execute(
            """
            SELECT
                t.*,
                v.id AS published_version_id,
                v.ai_name AS published_ai_name,
                v.personality_text AS published_personality_text,
                v.mission_text AS published_mission_text,
                v.opening_line AS published_opening_line,
                v.review_status AS published_review_status
            FROM creator_role_templates AS t
            LEFT JOIN creator_role_template_versions AS v
              ON v.creator_role_template_id = t.id AND v.is_published = 1
            WHERE t.campaign_code = ?
            """,
            (cleaned_code,),
        ).fetchone()
        if source is None:
            return {"applied": False, "reason": "not_found"}
        if source["app_id"] != ZHAOXI_APP_ID:
            return {"applied": False, "reason": "wrong_app"}
        if source["creator_platform_user_id"] != expected_creator_id:
            return {"applied": False, "reason": "creator_mismatch"}
        if source["deleted_at"] is not None or source["status"] == "deleted":
            return {"applied": False, "reason": "deleted"}
        if source["status"] != CreatorRoleTemplateStatus.ACTIVE.value:
            reason = (
                "disabled"
                if source["status"] in {"disabled_creator", "disabled_admin"}
                else "inactive"
            )
            return {"applied": False, "reason": reason}
        if source["expires_at"] is not None and now >= source["expires_at"]:
            return {"applied": False, "reason": "expired"}
        if (
            source["published_version_id"] is None
            or source["published_review_status"]
            != CreatorRoleTemplateReviewStatus.PASSED.value
        ):
            return {"applied": False, "reason": "no_published_version"}

        account = tx.execute(
            "SELECT app_id FROM accounts WHERE id = ?",
            (cleaned_account_id,),
        ).fetchone()
        if account is None:
            return {"applied": False, "reason": "account_not_found"}
        if account["app_id"] != ZHAOXI_APP_ID:
            return {"applied": False, "reason": "wrong_app"}
        if tx.execute(
            "SELECT 1 FROM account_campaign_attribution WHERE account_id = ?",
            (cleaned_account_id,),
        ).fetchone() is not None:
            return {"applied": False, "reason": "already_attributed"}

        content = normalize_creator_role_template_content(
            ai_name=source["published_ai_name"],
            personality_text=source["published_personality_text"],
            mission_text=source["published_mission_text"],
            opening_line=source["published_opening_line"],
        )
        tx.execute(
            """
            INSERT INTO account_campaign_attribution(
                account_id, campaign_code, mission_id, onboarding_script_variant,
                soul_preset_key, ai_name_preset, attributed_at, created_at
            ) VALUES (?, ?, NULL, NULL, NULL, NULL, ?, ?)
            """,
            (cleaned_account_id, cleaned_code, now, now),
        )
        tx.execute(
            """
            INSERT INTO account_creator_role_template_attribution(
                account_id, creator_role_template_id,
                creator_role_template_version_id, creator_platform_user_id,
                campaign_code, ai_name_snapshot, personality_snapshot,
                mission_snapshot, opening_line_snapshot, attributed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cleaned_account_id,
                source["id"],
                source["published_version_id"],
                source["creator_platform_user_id"],
                cleaned_code,
                content.ai_name,
                content.personality_text,
                content.mission_text,
                content.opening_line,
                now,
            ),
        )
        write_creator_role_template_snapshot(
            conn=tx,
            account_id=cleaned_account_id,
            snapshot=content,
        )
        tx.execute(
            """
            UPDATE creator_role_templates
            SET used_count = used_count + 1, updated_at = ?
            WHERE id = ?
            """,
            (now, source["id"]),
        )
        _insert_event(
            tx,
            template_id=source["id"],
            version_id=source["published_version_id"],
            event_type=CreatorRoleTemplateEventType.ATTRIBUTION_APPLIED,
            actor_type=CreatorRoleTemplateActorType.SYSTEM,
            actor_id=None,
            metadata={"account_id": cleaned_account_id},
            created_at=now,
        )
    return {
        "applied": True,
        "campaign_code": cleaned_code,
        "creator_role_template_id": source["id"],
        "creator_role_template_version_id": source["published_version_id"],
    }


def list_creator_role_template_versions(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    include_deleted_template: bool = False,
    conn: Optional[Connection] = None,
) -> List[CreatorRoleTemplateVersion]:
    """在 owner/app/template 范围内按版本号倒序读取版本历史。"""
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    deleted_clause = "" if include_deleted_template else " AND t.deleted_at IS NULL"
    with _tx(conn) as tx:
        rows = tx.execute(
            """
            SELECT v.*
            FROM creator_role_template_versions AS v
            JOIN creator_role_templates AS t ON t.id = v.creator_role_template_id
            WHERE t.id = ? AND t.creator_platform_user_id = ? AND t.app_id = ?
            """
            + deleted_clause
            + " ORDER BY v.version_no DESC",
            (template_id, owner_id, cleaned_app_id),
        ).fetchall()
    return [CreatorRoleTemplateVersion.from_row(row) for row in rows]


def create_creator_role_template_candidate_version(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    ai_name: Optional[str] = None,
    personality_text: Optional[str] = None,
    mission_text: Optional[str] = None,
    opening_line: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> CreatorRoleTemplateVersion:
    """基于 published（未发布模板则 latest）快照合并字段并创建 pending 新版本。"""
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    if all(
        value is None
        for value in (ai_name, personality_text, mission_text, opening_line)
    ):
        raise CreatorRoleTemplateError("creator_role_template_no_changes")
    with _tx(conn) as tx:
        template_row = _lock_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=template_id,
        )
        if template_row["status"] == CreatorRoleTemplateStatus.DISABLED_ADMIN.value:
            raise CreatorRoleTemplateError("creator_role_template_disabled_by_admin")
        open_row = tx.execute(
            """
            SELECT review_status FROM creator_role_template_versions
            WHERE creator_role_template_id = ?
              AND review_status IN ('pending', 'reviewing')
            LIMIT 1
            """,
            (template_id,),
        ).fetchone()
        if open_row is not None:
            code = (
                "role_review_in_progress"
                if open_row["review_status"] == "reviewing"
                else "role_review_pending"
            )
            raise CreatorRoleTemplateError(code)
        baseline = tx.execute(
            """
            SELECT * FROM creator_role_template_versions
            WHERE creator_role_template_id = ?
            ORDER BY is_published DESC, version_no DESC
            LIMIT 1
            """,
            (template_id,),
        ).fetchone()
        if baseline is None:
            raise CreatorRoleTemplateError("creator_role_template_version_not_found")
        content = normalize_creator_role_template_content(
            ai_name=baseline["ai_name"] if ai_name is None else ai_name,
            personality_text=(
                baseline["personality_text"]
                if personality_text is None
                else personality_text
            ),
            mission_text=(
                baseline["mission_text"] if mission_text is None else mission_text
            ),
            opening_line=(
                baseline["opening_line"] if opening_line is None else opening_line
            ),
        )
        next_version_no = int(
            tx.execute(
                """
                SELECT COALESCE(MAX(version_no), 0) + 1 AS next_no
                FROM creator_role_template_versions
                WHERE creator_role_template_id = ?
                """,
                (template_id,),
            ).fetchone()["next_no"]
        )
        version_id = _new_id("crtv")
        now = beijing_now_str()
        tx.execute(
            """
            INSERT INTO creator_role_template_versions(
                id, creator_role_template_id, version_no, ai_name,
                personality_text, mission_text, opening_line, review_status,
                is_published, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (
                version_id,
                template_id,
                next_version_no,
                content.ai_name,
                content.personality_text,
                content.mission_text,
                content.opening_line,
                now,
                now,
            ),
        )
        if template_row["activated_at"] is None:
            tx.execute(
                """
                UPDATE creator_role_templates
                SET status = 'pending_review', updated_at = ? WHERE id = ?
                """,
                (now, template_id),
            )
        row = _version_row(tx, template_id=template_id, version_id=version_id)
    return CreatorRoleTemplateVersion.from_row(row)


def claim_creator_role_template_review(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    version_id: str,
    conn: Optional[Connection] = None,
) -> CreatorRoleTemplateReviewClaim:
    """CAS claim 一个 pending 版本并创建 running review run；并发只有一个成功。"""
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    with _tx(conn) as tx:
        template_row = _lock_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=template_id,
        )
        if template_row["status"] == CreatorRoleTemplateStatus.DISABLED_ADMIN.value:
            raise CreatorRoleTemplateError("creator_role_template_disabled_by_admin")
        version_row = _version_row(tx, template_id=template_id, version_id=version_id)
        if version_row is None:
            raise CreatorRoleTemplateError("creator_role_template_version_not_found")
        if version_row["review_status"] == "reviewing":
            raise CreatorRoleTemplateError("role_review_in_progress")
        if version_row["review_status"] != "pending":
            raise CreatorRoleTemplateError("role_review_not_pending")
        now = beijing_now_str()
        claimed = tx.execute(
            """
            UPDATE creator_role_template_versions
            SET review_status = 'reviewing', updated_at = ?
            WHERE id = ? AND creator_role_template_id = ? AND review_status = 'pending'
            """,
            (now, version_id, template_id),
        )
        if claimed.rowcount != 1:
            raise CreatorRoleTemplateError("role_review_in_progress")
        attempt_no = int(
            tx.execute(
                """
                SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no
                FROM creator_role_template_review_runs
                WHERE creator_role_template_version_id = ?
                """,
                (version_id,),
            ).fetchone()["next_no"]
        )
        run_id = _new_id("crtrun")
        tx.execute(
            """
            INSERT INTO creator_role_template_review_runs(
                id, creator_role_template_version_id, attempt_no, status, started_at
            ) VALUES (?, ?, ?, 'running', ?)
            """,
            (run_id, version_id, attempt_no, now),
        )
        _insert_event(
            tx,
            template_id=template_id,
            version_id=version_id,
            event_type=CreatorRoleTemplateEventType.REVIEW_STARTED,
            actor_type=CreatorRoleTemplateActorType.CREATOR,
            actor_id=owner_id,
            metadata={"attempt_no": attempt_no, "run_id": run_id},
            created_at=now,
        )
        version_row = _version_row(tx, template_id=template_id, version_id=version_id)
        template_row = _find_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=template_id,
            include_deleted=False,
        )
    return CreatorRoleTemplateReviewClaim(
        run_id=run_id,
        attempt_no=attempt_no,
        template=CreatorRoleTemplate.from_row(template_row),
        version=CreatorRoleTemplateVersion.from_row(version_row),
    )


def complete_creator_role_template_review(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    version_id: str,
    run_id: str,
    decision: str,
    categories: List[str],
    reason: str,
    model: Optional[str],
    provider: Optional[str],
    latency_ms: int,
    generated_summary: Optional[str] = None,
    failure_code: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> CreatorRoleTemplateMutation:
    """按 run/version 双 CAS 完成审核；旧 run 不能覆盖后续 attempt 或候选版本。"""
    if decision not in {"pass", "reject", "error"}:
        raise CreatorRoleTemplateError("role_review_decision_invalid")
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    categories_json = _encode_categories(categories)
    clean_reason = str(reason or "")
    if len(clean_reason) > REVIEW_REASON_MAX_CHARS:
        raise CreatorRoleTemplateError("role_review_reason_too_long")
    clean_generated_summary: Optional[str] = None
    if decision == "pass":
        version_for_summary = get_creator_role_template_version(
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=template_id,
            version_id=version_id,
            conn=conn,
        )
        if version_for_summary is None:
            raise CreatorRoleTemplateError("creator_role_template_version_not_found")
        clean_generated_summary = normalize_creator_role_template_summary(
            summary=str(generated_summary or ""),
            ai_name=version_for_summary.ai_name,
        )
    now = beijing_now_str()
    run_status = {
        "pass": CreatorRoleTemplateReviewRunStatus.PASSED.value,
        "reject": CreatorRoleTemplateReviewRunStatus.REJECTED.value,
        "error": CreatorRoleTemplateReviewRunStatus.ERROR.value,
    }[decision]
    version_status = {
        "pass": CreatorRoleTemplateReviewStatus.PASSED.value,
        "reject": CreatorRoleTemplateReviewStatus.REJECTED.value,
        "error": CreatorRoleTemplateReviewStatus.PENDING.value,
    }[decision]
    event_type = {
        "pass": CreatorRoleTemplateEventType.REVIEW_PASSED,
        "reject": CreatorRoleTemplateEventType.REVIEW_REJECTED,
        "error": CreatorRoleTemplateEventType.REVIEW_ERROR,
    }[decision]
    with _tx(conn) as tx:
        template_row = _lock_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=template_id,
        )
        run_row = tx.execute(
            """
            SELECT r.status AS run_status, v.review_status AS version_status
            FROM creator_role_template_review_runs AS r
            JOIN creator_role_template_versions AS v
              ON v.id = r.creator_role_template_version_id
            WHERE r.id = ? AND r.creator_role_template_version_id = ?
              AND v.creator_role_template_id = ?
            """,
            (run_id, version_id, template_id),
        ).fetchone()
        if (
            run_row is None
            or run_row["run_status"] != "running"
            or run_row["version_status"] != "reviewing"
        ):
            raise CreatorRoleTemplateError("role_review_result_stale")
        updated_run = tx.execute(
            """
            UPDATE creator_role_template_review_runs
            SET status = ?, model = ?, provider = ?, latency_ms = ?,
                categories_json = ?, reason = ?, error_code = ?, finished_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (
                run_status,
                model,
                provider,
                max(0, int(latency_ms)),
                categories_json,
                clean_reason,
                failure_code,
                now,
                run_id,
            ),
        )
        updated_version = tx.execute(
            """
            UPDATE creator_role_template_versions
            SET review_status = ?, review_categories_json = ?, review_reason = ?,
                generated_summary = ?, public_summary = ?, summary_edit_status = ?,
                reviewed_at = ?, updated_at = ?
            WHERE id = ? AND creator_role_template_id = ? AND review_status = 'reviewing'
            """,
            (
                version_status,
                categories_json,
                clean_reason,
                clean_generated_summary,
                clean_generated_summary,
                (
                    CreatorRoleTemplateSummaryEditStatus.AVAILABLE.value
                    if decision == "pass"
                    else CreatorRoleTemplateSummaryEditStatus.UNAVAILABLE.value
                ),
                None if decision == "error" else now,
                now,
                version_id,
                template_id,
            ),
        )
        if updated_run.rowcount != 1 or updated_version.rowcount != 1:
            raise CreatorRoleTemplateError("role_review_result_stale")
        if template_row["activated_at"] is None and template_row["status"] != "disabled_admin":
            template_status = {
                "pass": CreatorRoleTemplateStatus.APPROVED.value,
                "reject": CreatorRoleTemplateStatus.REJECTED.value,
                "error": CreatorRoleTemplateStatus.PENDING_REVIEW.value,
            }[decision]
            tx.execute(
                "UPDATE creator_role_templates SET status = ?, updated_at = ? WHERE id = ?",
                (template_status, now, template_id),
            )
        _insert_event(
            tx,
            template_id=template_id,
            version_id=version_id,
            event_type=event_type,
            actor_type=CreatorRoleTemplateActorType.SYSTEM,
            actor_id=None,
            metadata={
                "attempt_no": tx.execute(
                    "SELECT attempt_no FROM creator_role_template_review_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()["attempt_no"],
                "failure_code": failure_code,
            },
            created_at=now,
        )
        template_row = _find_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=template_id,
            include_deleted=False,
        )
        version_row = _version_row(tx, template_id=template_id, version_id=version_id)
    return CreatorRoleTemplateMutation(
        template=CreatorRoleTemplate.from_row(template_row),
        version=CreatorRoleTemplateVersion.from_row(version_row),
    )


def claim_creator_role_template_summary_review(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    version_id: str,
    submitted_summary: str,
    conn: Optional[Connection] = None,
) -> CreatorRoleTemplateSummaryReviewClaim:
    """CAS claim 一个未发布版本的唯一简介编辑机会；并发只有一个进入审核。"""
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    with _tx(conn) as tx:
        template_row = _lock_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=template_id,
        )
        version_row = _version_row(tx, template_id=template_id, version_id=version_id)
        if version_row is None:
            raise CreatorRoleTemplateError("creator_role_template_version_not_found")
        if version_row["review_status"] != CreatorRoleTemplateReviewStatus.PASSED.value:
            raise CreatorRoleTemplateError("creator_role_template_version_not_approved")
        if bool(version_row["is_published"]):
            raise CreatorRoleTemplateError("creator_role_template_summary_edit_unavailable")
        clean_summary = normalize_creator_role_template_summary(
            summary=submitted_summary,
            ai_name=version_row["ai_name"],
        )
        status = str(version_row["summary_edit_status"] or "unavailable")
        if status == CreatorRoleTemplateSummaryEditStatus.REVIEWING.value:
            raise CreatorRoleTemplateError("creator_role_template_summary_review_in_progress")
        if status != CreatorRoleTemplateSummaryEditStatus.AVAILABLE.value:
            raise CreatorRoleTemplateError("creator_role_template_summary_edit_unavailable")
        now = beijing_now_str()
        claimed = tx.execute(
            """
            UPDATE creator_role_template_versions
            SET summary_edit_status = 'reviewing', updated_at = ?
            WHERE id = ? AND creator_role_template_id = ?
              AND summary_edit_status = 'available' AND is_published = 0
            """,
            (now, version_id, template_id),
        )
        if claimed.rowcount != 1:
            raise CreatorRoleTemplateError("creator_role_template_summary_review_in_progress")
        attempt_no = int(
            tx.execute(
                """
                SELECT COALESCE(MAX(attempt_no), 0) + 1 AS next_no
                FROM creator_role_template_summary_review_runs
                WHERE creator_role_template_version_id = ?
                """,
                (version_id,),
            ).fetchone()["next_no"]
        )
        run_id = _new_id("crtsumrun")
        tx.execute(
            """
            INSERT INTO creator_role_template_summary_review_runs(
                id, creator_role_template_version_id, attempt_no,
                submitted_summary, status, started_at
            ) VALUES (?, ?, ?, ?, 'running', ?)
            """,
            (run_id, version_id, attempt_no, clean_summary, now),
        )
        version_row = _version_row(tx, template_id=template_id, version_id=version_id)
    return CreatorRoleTemplateSummaryReviewClaim(
        run_id=run_id,
        attempt_no=attempt_no,
        template=CreatorRoleTemplate.from_row(template_row),
        version=CreatorRoleTemplateVersion.from_row(version_row),
        submitted_summary=clean_summary,
    )


def complete_creator_role_template_summary_review(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    version_id: str,
    run_id: str,
    decision: str,
    categories: List[str],
    reason: str,
    model: Optional[str],
    provider: Optional[str],
    latency_ms: int,
    failure_code: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> CreatorRoleTemplateMutation:
    """完成简介审核；pass/reject 消耗机会，error 恢复 available。"""
    if decision not in {"pass", "reject", "error"}:
        raise CreatorRoleTemplateError("creator_role_template_summary_decision_invalid")
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    categories_json = _encode_categories(categories)
    clean_reason = str(reason or "")
    if len(clean_reason) > REVIEW_REASON_MAX_CHARS:
        raise CreatorRoleTemplateError("role_review_reason_too_long")
    now = beijing_now_str()
    run_status = {
        "pass": CreatorRoleTemplateReviewRunStatus.PASSED.value,
        "reject": CreatorRoleTemplateReviewRunStatus.REJECTED.value,
        "error": CreatorRoleTemplateReviewRunStatus.ERROR.value,
    }[decision]
    summary_status = {
        "pass": CreatorRoleTemplateSummaryEditStatus.ACCEPTED.value,
        "reject": CreatorRoleTemplateSummaryEditStatus.REJECTED.value,
        "error": CreatorRoleTemplateSummaryEditStatus.AVAILABLE.value,
    }[decision]
    with _tx(conn) as tx:
        template_row = _lock_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=template_id,
        )
        run_row = tx.execute(
            """
            SELECT r.status, r.submitted_summary
            FROM creator_role_template_summary_review_runs AS r
            JOIN creator_role_template_versions AS v
              ON v.id = r.creator_role_template_version_id
            WHERE r.id = ? AND r.creator_role_template_version_id = ?
              AND v.creator_role_template_id = ?
              AND v.summary_edit_status = 'reviewing' AND v.is_published = 0
            """,
            (run_id, version_id, template_id),
        ).fetchone()
        if run_row is None or run_row["status"] != "running":
            raise CreatorRoleTemplateError("creator_role_template_summary_result_stale")
        updated_run = tx.execute(
            """
            UPDATE creator_role_template_summary_review_runs
            SET status = ?, model = ?, provider = ?, latency_ms = ?,
                categories_json = ?, reason = ?, error_code = ?, finished_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (
                run_status,
                model,
                provider,
                max(0, int(latency_ms)),
                categories_json,
                clean_reason,
                failure_code,
                now,
                run_id,
            ),
        )
        if decision == "pass":
            public_summary_sql = ", public_summary = ?"
            version_values: Tuple[Any, ...] = (
                summary_status,
                now,
                run_row["submitted_summary"],
                version_id,
                template_id,
            )
        else:
            public_summary_sql = ""
            version_values = (summary_status, now, version_id, template_id)
        updated_version = tx.execute(
            """
            UPDATE creator_role_template_versions
            SET summary_edit_status = ?, updated_at = ?
            """
            + public_summary_sql
            + " WHERE id = ? AND creator_role_template_id = ? "
            "AND summary_edit_status = 'reviewing' AND is_published = 0",
            version_values,
        )
        if updated_run.rowcount != 1 or updated_version.rowcount != 1:
            raise CreatorRoleTemplateError("creator_role_template_summary_result_stale")
        version_row = _version_row(tx, template_id=template_id, version_id=version_id)
    return CreatorRoleTemplateMutation(
        template=CreatorRoleTemplate.from_row(template_row),
        version=CreatorRoleTemplateVersion.from_row(version_row),
    )


def publish_creator_role_template_version(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    version_id: str,
    activated_at: str,
    first_expires_at: str,
    conn: Optional[Connection] = None,
) -> CreatorRoleTemplateMutation:
    """发布已通过版本；首次设置有效期，后续发布只切快照且不续期。"""
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    with _tx(conn) as tx:
        template_row = _lock_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=template_id,
        )
        if template_row["status"] == CreatorRoleTemplateStatus.DISABLED_ADMIN.value:
            raise CreatorRoleTemplateError("creator_role_template_disabled_by_admin")
        version_row = _version_row(tx, template_id=template_id, version_id=version_id)
        if version_row is None:
            raise CreatorRoleTemplateError("creator_role_template_version_not_found")
        if version_row["review_status"] != CreatorRoleTemplateReviewStatus.PASSED.value:
            raise CreatorRoleTemplateError("creator_role_template_version_not_approved")
        if (
            version_row["opening_line"] is not None
            and version_row["public_summary"] is None
        ):
            raise CreatorRoleTemplateError("creator_role_template_summary_unavailable")
        if version_row["summary_edit_status"] == "reviewing":
            raise CreatorRoleTemplateError("creator_role_template_summary_review_in_progress")
        if bool(version_row["is_published"]):
            return CreatorRoleTemplateMutation(
                template=CreatorRoleTemplate.from_row(template_row),
                version=CreatorRoleTemplateVersion.from_row(version_row),
            )
        tx.execute(
            """
            UPDATE creator_role_template_versions
            SET is_published = 0, updated_at = ?
            WHERE creator_role_template_id = ? AND is_published = 1 AND id != ?
            """,
            (activated_at, template_id, version_id),
        )
        tx.execute(
            """
            UPDATE creator_role_template_versions
            SET is_published = 1, published_at = ?, updated_at = ?
            WHERE id = ? AND creator_role_template_id = ?
            """,
            (activated_at, activated_at, version_id, template_id),
        )
        if template_row["activated_at"] is None:
            tx.execute(
                """
                UPDATE creator_role_templates
                SET status = 'active', activated_at = ?, expires_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (activated_at, first_expires_at, activated_at, template_id),
            )
        else:
            # 发布新版不改变 active/disabled_creator，也绝不重算有效期。
            tx.execute(
                "UPDATE creator_role_templates SET updated_at = ? WHERE id = ?",
                (activated_at, template_id),
            )
        _insert_event(
            tx,
            template_id=template_id,
            version_id=version_id,
            event_type=CreatorRoleTemplateEventType.VERSION_ACTIVATED,
            actor_type=CreatorRoleTemplateActorType.CREATOR,
            actor_id=owner_id,
            metadata={"version_no": int(version_row["version_no"])},
            created_at=activated_at,
        )
        template_row = _find_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=template_id,
            include_deleted=False,
        )
        version_row = _version_row(tx, template_id=template_id, version_id=version_id)
    return CreatorRoleTemplateMutation(
        template=CreatorRoleTemplate.from_row(template_row),
        version=CreatorRoleTemplateVersion.from_row(version_row),
    )


def set_creator_role_template_creator_enabled(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    enabled: bool,
    changed_at: str,
    conn: Optional[Connection] = None,
) -> CreatorRoleTemplate:
    """创建者停用或恢复已发布模板；不能解除 Admin 停用。"""
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    with _tx(conn) as tx:
        row = _lock_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=template_id,
        )
        expected = "disabled_creator" if enabled else "active"
        target = "active" if enabled else "disabled_creator"
        if row["status"] == "disabled_admin":
            raise CreatorRoleTemplateError("creator_role_template_disabled_by_admin")
        if row["status"] != expected:
            raise CreatorRoleTemplateError("creator_role_template_state_conflict")
        tx.execute(
            "UPDATE creator_role_templates SET status = ?, updated_at = ? WHERE id = ?",
            (target, changed_at, template_id),
        )
        _insert_event(
            tx,
            template_id=template_id,
            version_id=None,
            event_type=(
                CreatorRoleTemplateEventType.ENABLED
                if enabled
                else CreatorRoleTemplateEventType.DISABLED_BY_CREATOR
            ),
            actor_type=CreatorRoleTemplateActorType.CREATOR,
            actor_id=owner_id,
            metadata=None,
            created_at=changed_at,
        )
        row = _find_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=template_id,
            include_deleted=False,
        )
    return CreatorRoleTemplate.from_row(row)


def disable_creator_role_template_by_admin(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    admin_user_id: str,
    reason: str,
    changed_at: str,
    conn: Optional[Connection] = None,
) -> CreatorRoleTemplate:
    """Admin/Staff 停用任意 live 模板并记录停用前状态，供受控恢复。"""
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    clean_admin_id = str(admin_user_id or "").strip()
    clean_reason = str(reason or "").strip()
    if not clean_admin_id:
        raise CreatorRoleTemplateError("admin_user_id_required")
    if not clean_reason or len(clean_reason) > DISABLED_REASON_MAX_CHARS:
        raise CreatorRoleTemplateError("disabled_reason_invalid")
    with _tx(conn) as tx:
        row = _lock_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=template_id,
        )
        if row["status"] == "disabled_admin":
            raise CreatorRoleTemplateError("creator_role_template_state_conflict")
        previous_status = row["status"]
        tx.execute(
            """
            UPDATE creator_role_templates
            SET status = 'disabled_admin', disabled_by_admin_user_id = ?,
                disabled_reason = ?, status_before_admin_disable = ?,
                updated_at = ? WHERE id = ?
            """,
            (
                clean_admin_id,
                clean_reason,
                previous_status,
                changed_at,
                template_id,
            ),
        )
        _insert_event(
            tx,
            template_id=template_id,
            version_id=None,
            event_type=CreatorRoleTemplateEventType.DISABLED_BY_ADMIN,
            actor_type=CreatorRoleTemplateActorType.ADMIN,
            actor_id=clean_admin_id,
            metadata={"previous_status": previous_status, "reason": clean_reason},
            created_at=changed_at,
        )
        row = _find_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=template_id,
            include_deleted=False,
        )
    return CreatorRoleTemplate.from_row(row)


def enable_creator_role_template_by_admin(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    admin_user_id: str,
    changed_at: str,
    conn: Optional[Connection] = None,
) -> CreatorRoleTemplate:
    """仅 Admin/Staff 解除 admin disabled，并按当前版本与停用前状态恢复。"""
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    clean_admin_id = str(admin_user_id or "").strip()
    if not clean_admin_id:
        raise CreatorRoleTemplateError("admin_user_id_required")
    with _tx(conn) as tx:
        row = _lock_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=template_id,
        )
        if row["status"] != "disabled_admin":
            raise CreatorRoleTemplateError("creator_role_template_state_conflict")
        previous_status = str(row["status_before_admin_disable"] or "active")
        if row["activated_at"] is not None:
            restored = (
                "disabled_creator" if previous_status == "disabled_creator" else "active"
            )
        else:
            latest = tx.execute(
                """
                SELECT review_status FROM creator_role_template_versions
                WHERE creator_role_template_id = ? ORDER BY version_no DESC LIMIT 1
                """,
                (template_id,),
            ).fetchone()
            restored = {
                "passed": "approved",
                "rejected": "rejected",
                "reviewing": "pending_review",
                "pending": "pending_review",
            }.get(latest["review_status"] if latest else "pending", "pending_review")
        tx.execute(
            """
            UPDATE creator_role_templates
            SET status = ?, disabled_by_admin_user_id = NULL,
                disabled_reason = NULL, status_before_admin_disable = NULL,
                updated_at = ? WHERE id = ?
            """,
            (restored, changed_at, template_id),
        )
        _insert_event(
            tx,
            template_id=template_id,
            version_id=None,
            event_type=CreatorRoleTemplateEventType.ENABLED,
            actor_type=CreatorRoleTemplateActorType.ADMIN,
            actor_id=clean_admin_id,
            metadata={"restored_status": restored},
            created_at=changed_at,
        )
        row = _find_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=template_id,
            include_deleted=False,
        )
    return CreatorRoleTemplate.from_row(row)


def list_creator_role_template_review_runs(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    version_id: str,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """owner-scoped 读取某个版本的审核调用历史，不返回任何原始 prompt/输出。"""
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    with _tx(conn) as tx:
        rows = tx.execute(
            """
            SELECT r.* FROM creator_role_template_review_runs AS r
            JOIN creator_role_template_versions AS v
              ON v.id = r.creator_role_template_version_id
            JOIN creator_role_templates AS t ON t.id = v.creator_role_template_id
            WHERE t.id = ? AND t.creator_platform_user_id = ? AND t.app_id = ?
              AND v.id = ?
            ORDER BY r.attempt_no DESC
            """,
            (template_id, owner_id, cleaned_app_id, version_id),
        ).fetchall()
    return [dict(row) for row in rows]


def list_creator_role_template_summary_review_runs(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    version_id: str,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """owner-scoped 读取简介修改审核历史，供 Admin 审计。"""
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    with _tx(conn) as tx:
        rows = tx.execute(
            """
            SELECT r.* FROM creator_role_template_summary_review_runs AS r
            JOIN creator_role_template_versions AS v
              ON v.id = r.creator_role_template_version_id
            JOIN creator_role_templates AS t ON t.id = v.creator_role_template_id
            WHERE t.id = ? AND t.creator_platform_user_id = ? AND t.app_id = ?
              AND v.id = ?
            ORDER BY r.attempt_no DESC
            """,
            (template_id, owner_id, cleaned_app_id, version_id),
        ).fetchall()
    return [dict(row) for row in rows]


def soft_delete_creator_role_template(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    conn: Optional[Connection] = None,
) -> CreatorRoleTemplate:
    """owner-scoped 软删除模板并释放 slot；版本、事件与历史归因均保留。"""
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    cleaned_template_id = str(template_id or "").strip()
    if not cleaned_template_id:
        raise CreatorRoleTemplateError("creator_role_template_not_found")
    now = beijing_now_str()
    with _tx(conn) as tx:
        cursor = tx.execute(
            """
            UPDATE creator_role_templates
            SET status = ?, deleted_at = ?, updated_at = ?
            WHERE id = ? AND creator_platform_user_id = ? AND app_id = ?
              AND deleted_at IS NULL
            """,
            (
                CreatorRoleTemplateStatus.DELETED.value,
                now,
                now,
                cleaned_template_id,
                owner_id,
                cleaned_app_id,
            ),
        )
        if cursor.rowcount != 1:
            raise CreatorRoleTemplateError("creator_role_template_not_found")
        _insert_event(
            tx,
            template_id=cleaned_template_id,
            version_id=None,
            event_type=CreatorRoleTemplateEventType.DELETED,
            actor_type=CreatorRoleTemplateActorType.CREATOR,
            actor_id=owner_id,
            metadata=None,
            created_at=now,
        )
        row = _find_template_row(
            tx,
            creator_platform_user_id=owner_id,
            app_id=cleaned_app_id,
            template_id=cleaned_template_id,
            include_deleted=True,
        )
    return CreatorRoleTemplate.from_row(row)


def soft_delete_all_creator_role_templates(
    *,
    creator_platform_user_id: str,
    app_id: str,
    changed_at: str,
    conn: Optional[Connection] = None,
) -> int:
    """账号注销时幂等软删除 owner 在本产品下的全部 live 模板。"""
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id,
        app_id=app_id,
    )
    with _tx(conn) as tx:
        rows = tx.execute(
            """
            SELECT id FROM creator_role_templates
            WHERE creator_platform_user_id = ? AND app_id = ?
              AND deleted_at IS NULL
            ORDER BY id
            """,
            (owner_id, cleaned_app_id),
        ).fetchall()
        deleted = 0
        for row in rows:
            template_id = str(row["id"])
            cursor = tx.execute(
                """
                UPDATE creator_role_templates
                SET status = 'deleted', deleted_at = ?, updated_at = ?
                WHERE id = ? AND creator_platform_user_id = ? AND app_id = ?
                  AND deleted_at IS NULL
                """,
                (changed_at, changed_at, template_id, owner_id, cleaned_app_id),
            )
            if cursor.rowcount != 1:
                continue
            deleted += 1
            _insert_event(
                tx,
                template_id=template_id,
                version_id=None,
                event_type=CreatorRoleTemplateEventType.DELETED,
                actor_type=CreatorRoleTemplateActorType.SYSTEM,
                actor_id=None,
                metadata={"reason": "account_deletion"},
                created_at=changed_at,
            )
    return deleted


def list_creator_role_template_events(
    *,
    creator_platform_user_id: str,
    app_id: str,
    template_id: str,
    include_deleted_template: bool = False,
    limit: int = 100,
    conn: Optional[Connection] = None,
) -> List[Dict[str, Any]]:
    """owner-scoped 读取模板审计事件；默认不暴露已删除模板历史。"""
    owner_id, cleaned_app_id = _require_scope(
        creator_platform_user_id=creator_platform_user_id, app_id=app_id
    )
    safe_limit = max(1, min(int(limit or 100), 500))
    deleted_clause = "" if include_deleted_template else " AND t.deleted_at IS NULL"
    with _tx(conn) as tx:
        rows = tx.execute(
            """
            SELECT e.*
            FROM creator_role_template_events AS e
            JOIN creator_role_templates AS t ON t.id = e.creator_role_template_id
            WHERE t.id = ? AND t.creator_platform_user_id = ? AND t.app_id = ?
            """
            + deleted_clause
            + " ORDER BY e.created_at DESC, e.id DESC LIMIT ?",
            (template_id, owner_id, cleaned_app_id, safe_limit),
        ).fetchall()
    return [dict(row) for row in rows]


__all__ = [
    "apply_creator_role_template_attribution",
    "claim_creator_role_template_review",
    "claim_creator_role_template_summary_review",
    "complete_creator_role_template_review",
    "complete_creator_role_template_summary_review",
    "create_creator_role_template",
    "create_creator_role_template_candidate_version",
    "disable_creator_role_template_by_admin",
    "enable_creator_role_template_by_admin",
    "get_account_creator_role_template_attribution",
    "get_creator_role_template",
    "get_creator_role_template_for_admin",
    "get_creator_role_template_by_campaign_code",
    "get_creator_role_template_version",
    "list_creator_role_template_events",
    "list_creator_role_templates_for_admin",
    "list_creator_role_template_review_runs",
    "list_creator_role_template_summary_review_runs",
    "list_creator_role_template_versions",
    "list_creator_role_templates",
    "publish_creator_role_template_version",
    "set_creator_role_template_creator_enabled",
    "soft_delete_all_creator_role_templates",
    "soft_delete_creator_role_template",
]
