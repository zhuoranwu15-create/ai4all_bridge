"""账号注销的**立即执行**清除（ME-06/07，Q14 已拍板 2026-07-26）。

产品口径：注销**立即**删聊天记录和相关记忆，不设冷静期、不可撤销。因此本模块是
`me_settings` 的执行侧搭档——`me_settings` 只记录「谁、什么时候、为什么申请」，
真正动数据的全部逻辑集中在这里，便于单点审计。

清除范围（按 `platform_user_id` + `app_id` 严格限定）：

1. 该真人在本产品下的**每一个 runtime account** 走 `unbind_and_wipe_account`：
   聊天原文、session、L1/L2 记忆（`memory_events` / dreaming）、账号级 profile 文件
   （SOUL / IDENTITY / USER）、提醒与承诺、出站记录一并删除，账号置 `deactivated`。
   微信形态与 App 居民形态的账号都在内——同一个真人两边是同一份记忆资产。
2. `universe_memory_facts`：L3 世界级共享记忆。它挂在 universe 而不是 account 上，
   逐账号 wipe **抓不到**，必须单独删，否则「删了聊天记录但 AI 还记得你」。
3. `ai_conversations`：会话句柄本身（无任何表引用它，可直接删）。
4. `app_notifications`：站内通知全部由 AI 主动发起，属会话衍生物。
5. 居民置 `dismissed`、世界回到 `preparing`：世界行受 `UNIQUE(owner_platform_user_id)`
   约束不能删，重置成引导前状态即可——用户再登录就是一个干净的新世界。
6. 撤销该真人的全部登录 session：注销后所有设备立即登出。
7. `universe_posts` 及其派生物（`universe_post_media`、`companion_world_outbox`、
   `resident_lifecycle_events` / `resident_lifecycle_event_actions`）：Feed 归零。
   动态只可能发在自己的世界里（发布路径按 `owner_platform_user_id` 定位世界），
   所以按 `universe_id` 删就是 owner-scoped。删序由外键决定：先子表与 outbox、
   再告别事件（`farewell_post_id` 引用动态），最后动态本身。
   告别事件被这条链带进来不只是为了解外键——它自己也存了 `farewell_text` 与证据引用，
   是按真人存的用户衍生数据，留着同样违背「删了聊天记录但 AI 还记得你」。
8. `media_assets`：该真人上传的图片与语音，**连库行带磁盘文件**删掉。已被引用的资产
   `expires_at` 是 NULL，孤儿回收（D-10）永远抓不到它们，所以必须在这里删——否则
   「聊天记录已删除」之后 AI 会话图、语音和 Feed 图仍永久留在库和磁盘上。
   例外见下方保留清单第一条。
9. `resident_wishes` / `resident_wish_jobs` 与 `source='wish'` 来信：先取消持久任务，再清理
   愿望和专属生成物；已 claim 的旧 worker 因 wish CAS 锚消失，不能在注销后写回来。
10. 创建者持有的角色模板全部 soft delete，使分享链接立即停止服务；被邀请账号自己的
    不可变快照不受影响。逐账号 wipe 同时删除该账号的模板归因快照和 profile 文件。

**新增任何按真人/账号存数据的表，都必须在本模块的清除清单或下方保留清单里显式登记一次。**
这两份清单是注销口径的唯一来源，只靠"下次记得"必然漏（`media_assets` 就是这么漏的：
它在 S1 建表，而本模块写在更早的批次）。

**刻意不动**的三类，各有理由，改动前需要单独口径：

- 真人会话 `human_conversations` / `human_messages`、来访与邀请码：涉及**第三方**，
  删我方副本等于删对方的聊天记录。属 PRD §11.2-9 未决项。
  **同一条逻辑推到媒体上**：被 `human_messages` 引用的、由该真人自己发出的媒体一并保留
  （否则对方的聊天记录里会留下一张永远加载不出来的图）。对方发给他的媒体主人是对方，
  owner-scoped 清除天然带不走，不需要额外条件。
- 财务审计链（wallet / ledger / cost_events）：`wipe_account_data` 已有既定保留规则
  （推荐奖励引用的流水必须留，否则余额对不上流水合计）。
- `platform_users` 行本身：保留手机号唯一键。注销后用同一手机号登录 = 全新用户、
  空世界，而不是「手机号被永久占用」。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.db._core import connect
from app.db.lifecycle import unbind_and_wipe_account
from app.platform.media.assets import delete_media_file
from app.platform.media.persistence import (
    delete_media_asset_row,
    list_media_assets_for_owner,
)
from app.products.zhaoxi.infrastructure.persistence import me_settings

logger = logging.getLogger("ai4all.zhaoxi.account_deletion")

__all__ = [
    "delete_account_now",
    "execute_account_deletion",
    "list_runtime_accounts_for_deletion",
    "purge_owner_media",
]


def list_runtime_accounts_for_deletion(
    *, platform_user_id: str, app_id: str
) -> List[str]:
    """列出该真人在本产品下需要清除的全部 runtime account id（去重、稳定序）。

    两形态都要覆盖（见 `resolve_owner_platform_user_id` 的命名债说明）：
    形态 A 经 `account_owner_bindings`，形态 B 经 `universe_residents.runtime_account_id`。
    只认本 `app_id`——跨产品账号绝不能被本产品的注销顺手删掉。
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT a.id AS id
            FROM accounts a
            JOIN account_owner_bindings b ON b.account_id = a.id
            WHERE b.platform_user_id = ? AND b.status = 'active'
              AND b.app_id = ? AND a.app_id = ?
            UNION
            SELECT DISTINCT a.id AS id
            FROM accounts a
            JOIN universe_residents r ON r.runtime_account_id = a.id
            JOIN universes u ON u.id = r.universe_id
            WHERE u.owner_platform_user_id = ? AND a.app_id = ?
            """,
            (platform_user_id, app_id, app_id, platform_user_id, app_id),
        ).fetchall()
    return sorted({str(row["id"]) for row in rows})


def purge_owner_media(*, platform_user_id: str) -> Dict[str, int]:
    """删掉该真人拥有的媒体资产（库行 + 磁盘文件），保留真人会话里他自己发出的那些。

    保留口径见模块 docstring：删掉自发的真人会话媒体等于在**对方**的聊天记录里留一张
    加载不出来的图，与 `human_messages` 本身保留的理由是同一条。AI 会话与 Feed 的媒体
    无条件删——动态本身也在同一次注销里删掉（清除清单第 7 条），两侧口径一致。

    删除顺序与孤儿回收（D-10）一致：**先删行、再删文件**。删行是 owner-scoped 的原子判定，
    命中才说明这份资产确实归本次注销处置；反过来先删文件会在并发下留出"库里有行、磁盘没文件"
    的窗口。删文件失败只计数并打日志，不中断整批——注销幂等，重跑一次即可补齐。

    :returns: ``{"media_assets_deleted", "media_files_deleted", "media_assets_kept",
        "media_file_errors"}``。
    """
    assets = list_media_assets_for_owner(owner_platform_user_id=platform_user_id)
    if not assets:
        return {
            "media_assets_deleted": 0,
            "media_files_deleted": 0,
            "media_assets_kept": 0,
            "media_file_errors": 0,
        }
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT media_id FROM human_messages "
            "WHERE sender_platform_user_id = ? AND media_id IS NOT NULL",
            (platform_user_id,),
        ).fetchall()
    kept_ids = {str(row["media_id"]) for row in rows}
    deleted_rows = 0
    deleted_files = 0
    kept = 0
    file_errors = 0
    for asset in assets:
        media_id = str(asset.get("id") or "")
        if media_id in kept_ids:
            kept += 1
            continue
        if not delete_media_asset_row(
            media_id=media_id,
            only_pending=False,
            owner_platform_user_id=platform_user_id,
        ):
            # 幂等重跑或并发回收已经删过这行：文件也已由那一次处理，不重复删。
            continue
        deleted_rows += 1
        try:
            if delete_media_file(str(asset.get("storage_path") or "")):
                deleted_files += 1
        except Exception as err:  # noqa: BLE001 — 删文件失败不能中断整次注销
            file_errors += 1
            logger.warning(
                "account deletion failed to delete media file media_id=%s error_type=%s",
                media_id,
                type(err).__name__,
            )
    return {
        "media_assets_deleted": deleted_rows,
        "media_files_deleted": deleted_files,
        "media_assets_kept": kept,
        "media_file_errors": file_errors,
    }


def execute_account_deletion(
    *, platform_user_id: str, app_id: str, now: datetime
) -> Dict[str, Any]:
    """立即清除该真人在本产品下的聊天记录与记忆，返回可审计的删除计数。

    幂等：所有语句都是「按 owner 删/改」，重复执行第二次全部命中 0 行。因此重复注销
    不会报错，也不会把别人的数据卷进来。

    逐账号 wipe 各自一个事务（`unbind_and_wipe_account` 内部已保证 unbind+wipe 原子），
    世界级清理另起一个事务。**刻意不合成一个巨型事务**：wipe 单账号已经涉及三十余张表，
    再叠加会显著拉长 PG 的锁持有时间；即使中途失败，重跑一次即可补齐（幂等）。
    """
    current = now.strftime("%Y-%m-%d %H:%M:%S")
    account_ids = list_runtime_accounts_for_deletion(
        platform_user_id=platform_user_id, app_id=app_id
    )
    wiped: Dict[str, Any] = {
        "accounts_wiped": 0,
        "messages_deleted": 0,
        "sessions_deleted": 0,
        "memory_events_deleted": 0,
        "dreaming_memory_items_deleted": 0,
        "profile_files_deleted": 0,
    }
    for account_id in account_ids:
        stats = unbind_and_wipe_account(account_id=account_id)
        wiped["accounts_wiped"] += 1
        for key in (
            "messages_deleted",
            "sessions_deleted",
            "memory_events_deleted",
            "dreaming_memory_items_deleted",
            "profile_files_deleted",
        ):
            wiped[key] += int(stats.get(key) or 0)

    with connect() as conn:
        from app.products.zhaoxi.infrastructure.persistence.creator_role_templates import (  # noqa: PLC0415
            soft_delete_all_creator_role_templates,
        )

        creator_role_templates_deleted = soft_delete_all_creator_role_templates(
            creator_platform_user_id=platform_user_id,
            app_id=app_id,
            changed_at=current,
            conn=conn,
        )
        # 异步许愿任务先停再删；任何已 claim 的 worker 后续 CAS 都会因 wish 行消失而失效。
        resident_wish_jobs = conn.execute(
            "DELETE FROM resident_wish_jobs WHERE wish_id IN "
            "(SELECT id FROM resident_wishes WHERE owner_platform_user_id = ?)",
            (platform_user_id,),
        ).rowcount
        conn.execute(
            "UPDATE resident_wishes SET letter_id = NULL "
            "WHERE owner_platform_user_id = ?",
            (platform_user_id,),
        )
        resident_wish_letters = conn.execute(
            "DELETE FROM character_letters "
            "WHERE owner_platform_user_id = ? AND source = 'wish'",
            (platform_user_id,),
        ).rowcount
        resident_wishes = conn.execute(
            "DELETE FROM resident_wishes WHERE owner_platform_user_id = ?",
            (platform_user_id,),
        ).rowcount
        conn.execute(
            "DELETE FROM character_letter_catalog WHERE source = 'wish' "
            "AND character_template_id IN (SELECT id FROM character_templates "
            "WHERE owner_platform_user_id = ? AND source_type = 'generated')",
            (platform_user_id,),
        )
        conn.execute(
            "DELETE FROM character_templates WHERE owner_platform_user_id = ? "
            "AND source_type = 'generated' AND NOT EXISTS ("
            "SELECT 1 FROM universe_residents r WHERE r.character_template_id = character_templates.id)",
            (platform_user_id,),
        )
        universe = conn.execute(
            "SELECT id FROM universes WHERE owner_platform_user_id = ?",
            (platform_user_id,),
        ).fetchone()
        if universe is None:
            universe_memory_facts = 0
            ai_conversations = 0
            residents_dismissed = 0
            universe_posts = 0
            outbox_deleted = 0
            lifecycle_events = 0
        else:
            universe_id = str(universe["id"])
            # L3 共享记忆挂在 universe 上，逐账号 wipe 抓不到，必须单独删。
            universe_memory_facts = conn.execute(
                "DELETE FROM universe_memory_facts WHERE universe_id = ?",
                (universe_id,),
            ).rowcount
            ai_conversations = conn.execute(
                "DELETE FROM ai_conversations WHERE universe_id = ?",
                (universe_id,),
            ).rowcount
            residents_dismissed = conn.execute(
                """
                UPDATE universe_residents
                SET status = 'dismissed', updated_at = ?
                WHERE universe_id = ? AND status <> 'dismissed'
                """,
                (current, universe_id),
            ).rowcount
            # Feed 归零（清除清单第 7 条）。删序完全由外键决定，不能调换：
            # universe_post_media / companion_world_outbox 指向动态，
            # resident_lifecycle_events.farewell_post_id 也指向动态。
            conn.execute(
                "DELETE FROM universe_post_media WHERE post_id IN "
                "(SELECT id FROM universe_posts WHERE universe_id = ?)",
                (universe_id,),
            )
            # outbox 是动态的推送派生物，整个世界的都属于本次注销范围。
            outbox_deleted = conn.execute(
                "DELETE FROM companion_world_outbox WHERE universe_id = ?",
                (universe_id,),
            ).rowcount
            conn.execute(
                "DELETE FROM resident_lifecycle_event_actions WHERE event_id IN "
                "(SELECT id FROM resident_lifecycle_events WHERE owner_platform_user_id = ?)",
                (platform_user_id,),
            )
            lifecycle_events = conn.execute(
                "DELETE FROM resident_lifecycle_events WHERE owner_platform_user_id = ?",
                (platform_user_id,),
            ).rowcount
            universe_posts = conn.execute(
                "DELETE FROM universe_posts WHERE universe_id = ?",
                (universe_id,),
            ).rowcount
            # 世界行受 UNIQUE(owner_platform_user_id) 约束不能删，回到引导前状态即可。
            conn.execute(
                "UPDATE universes SET onboarding_state = 'preparing', updated_at = ? WHERE id = ?",
                (current, universe_id),
            )
        app_notifications = conn.execute(
            "DELETE FROM app_notifications WHERE platform_user_id = ?",
            (platform_user_id,),
        ).rowcount
        sessions_revoked = conn.execute(
            "DELETE FROM platform_user_sessions WHERE platform_user_id = ?",
            (platform_user_id,),
        ).rowcount

    # 媒体清除排在最后、且不在上面的事务里：它要删磁盘文件（不可回滚的副作用），
    # 必须等库侧的引用（messages / ai_conversations）都已经删完再动手。
    media = purge_owner_media(platform_user_id=platform_user_id)

    return {
        **wiped,
        **media,
        "universe_memory_facts_deleted": int(universe_memory_facts or 0),
        "ai_conversations_deleted": int(ai_conversations or 0),
        "residents_dismissed": int(residents_dismissed or 0),
        "universe_posts_deleted": int(universe_posts or 0),
        "companion_world_outbox_deleted": int(outbox_deleted or 0),
        "resident_lifecycle_events_deleted": int(lifecycle_events or 0),
        "resident_wish_jobs_deleted": int(resident_wish_jobs or 0),
        "resident_wishes_deleted": int(resident_wishes or 0),
        "resident_wish_letters_deleted": int(resident_wish_letters or 0),
        "app_notifications_deleted": int(app_notifications or 0),
        "sessions_revoked": int(sessions_revoked or 0),
        "creator_role_templates_deleted": int(
            creator_role_templates_deleted or 0
        ),
    }


def delete_account_now(
    *,
    platform_user_id: str,
    app_id: str,
    reason_code: Optional[str],
    now: datetime,
) -> Dict[str, Any]:
    """ME-06/07 的完整自助注销：先清数据，再落一条已执行的流水。

    顺序不可颠倒——先记录后清除时，如果清除中途失败就会留下一条「已删除」的假流水，
    对合规追溯来说比没有记录更糟。

    入参校验必须**赶在清除之前**：非法 ``reason_code`` 只该拿到 422，绝不能「数据已经
    删完了才报参数错」。
    """
    if reason_code is not None and reason_code not in me_settings.DELETION_REASON_CODES:
        raise ValueError("invalid deletion reason_code")
    stats = execute_account_deletion(
        platform_user_id=platform_user_id, app_id=app_id, now=now
    )
    record = me_settings.record_deletion_execution(
        platform_user_id=platform_user_id,
        app_id=app_id,
        reason_code=reason_code,
        purge_stats=stats,
        now=now,
    )
    return {"record": record, "stats": stats}
