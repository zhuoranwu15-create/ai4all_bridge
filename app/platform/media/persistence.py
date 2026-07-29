"""``media_assets`` 存储原语（v1.5 S1 / m0054）。

跨产品共用（放 `app.platform` 而非 `app.products.zhaoxi`），但**每个函数都以
``owner_platform_user_id`` 锚定**：这是账号隔离不变量在媒体侧的落点。唯一不带 owner 的读是
:func:`get_media_asset_unscoped`，它专供签名读端点——那条链路的凭据是 URL 签名里的 scope，
调用方必须自己判 scope 是否仍有权访问（见 D-3）。

状态机只有两态：``pending`` → ``referenced``。``pending`` 带 ``expires_at``，到点连行带文件回收；
``referenced`` 把 ``expires_at`` 置 NULL，从此不过期。引用与状态翻转必须在**同一个事务**里做
（把 ``conn`` 传进来），否则会出现"消息已存、媒体已被回收"的坏读。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence

from app.db._backend import Connection
from app.db._core import _tx
from app.time_utils import beijing_now, beijing_now_str

__all__ = [
    "MEDIA_STATUS_PENDING",
    "MEDIA_STATUS_REFERENCED",
    "delete_media_asset_row",
    "get_media_asset",
    "get_media_asset_unscoped",
    "insert_media_asset",
    "list_expired_pending_media_assets",
    "mark_media_assets_referenced",
    "pending_expires_at",
    "update_media_transcript",
]

MEDIA_STATUS_PENDING = "pending"
MEDIA_STATUS_REFERENCED = "referenced"

_MEDIA_KINDS = {"image", "voice"}


def _required(value: str, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


def pending_expires_at(*, ttl_hours: int) -> str:
    """未被引用资产的回收截止时间（北京时区字符串，与库内其余时间同格式）。"""
    deadline = beijing_now() + timedelta(hours=max(int(ttl_hours), 1))
    return deadline.strftime("%Y-%m-%d %H:%M:%S")


def insert_media_asset(
    *,
    media_id: str,
    owner_platform_user_id: str,
    kind: str,
    mime: str,
    bytes_len: int,
    sha256: str,
    storage_path: str,
    width: Optional[int] = None,
    height: Optional[int] = None,
    duration_ms: Optional[int] = None,
    transcript: Optional[str] = None,
    expires_at: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> Dict[str, Any]:
    """写入一条 ``pending`` 媒体资产。落盘在前、写库在后，由调用方保证顺序。"""
    media_id = _required(media_id, "media_id")
    owner_platform_user_id = _required(owner_platform_user_id, "owner_platform_user_id")
    kind = _required(kind, "kind")
    if kind not in _MEDIA_KINDS:
        raise ValueError(f"invalid media kind: {kind}")
    mime = _required(mime, "mime")
    sha256 = _required(sha256, "sha256")
    storage_path = _required(storage_path, "storage_path")
    if int(bytes_len) <= 0:
        raise ValueError("bytes_len must be positive")
    created_at = beijing_now_str()
    with _tx(conn) as tx:
        tx.execute(
            """
            INSERT INTO media_assets(
                id, owner_platform_user_id, kind, mime, bytes, width, height,
                duration_ms, sha256, storage_path, transcript, status,
                expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                media_id,
                owner_platform_user_id,
                kind,
                mime,
                int(bytes_len),
                None if width is None else int(width),
                None if height is None else int(height),
                None if duration_ms is None else int(duration_ms),
                sha256,
                storage_path,
                transcript,
                MEDIA_STATUS_PENDING,
                expires_at,
                created_at,
            ),
        )
        row = tx.execute("SELECT * FROM media_assets WHERE id = ?", (media_id,)).fetchone()
    if row is None:
        raise RuntimeError("media asset was not created")
    return dict(row)


def get_media_asset(
    *,
    media_id: str,
    owner_platform_user_id: str,
    conn: Optional[Connection] = None,
) -> Optional[Dict[str, Any]]:
    """按 id + owner 锚读取；跨 owner 一律返回 None（不区分"不存在"与"不属于你"）。"""
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT * FROM media_assets WHERE id = ? AND owner_platform_user_id = ?",
            (str(media_id or "").strip(), str(owner_platform_user_id or "").strip()),
        ).fetchone()
    return dict(row) if row is not None else None


def get_media_asset_unscoped(
    *, media_id: str, conn: Optional[Connection] = None
) -> Optional[Dict[str, Any]]:
    """**仅供签名读端点**：凭据是 URL 签名，owner 由调用方按 scope 复查。

    别在业务代码里用它——任何按用户维度的读都必须走 :func:`get_media_asset`。
    """
    with _tx(conn) as tx:
        row = tx.execute(
            "SELECT * FROM media_assets WHERE id = ?", (str(media_id or "").strip(),)
        ).fetchone()
    return dict(row) if row is not None else None


def mark_media_assets_referenced(
    *,
    media_ids: Sequence[str],
    owner_platform_user_id: str,
    conn: Connection,
) -> List[Dict[str, Any]]:
    """把 ``pending`` 资产翻成 ``referenced`` 并清掉 ``expires_at``。

    必须在发消息/发动态的**同一事务**内调用（因此 ``conn`` 是必填）。任一 id 不存在、
    不属于该 owner，或已被引用过，就抛 ``ValueError``——上层映射成 ``media_ref_invalid``。
    """
    owner_platform_user_id = _required(owner_platform_user_id, "owner_platform_user_id")
    cleaned = [str(mid or "").strip() for mid in media_ids]
    if any(not mid for mid in cleaned):
        raise ValueError("media_ref is required")
    if len(set(cleaned)) != len(cleaned):
        raise ValueError("duplicated media_ref")
    referenced: List[Dict[str, Any]] = []
    for media_id in cleaned:
        updated = conn.execute(
            "UPDATE media_assets SET status = ?, expires_at = NULL "
            "WHERE id = ? AND owner_platform_user_id = ? AND status = ?",
            (
                MEDIA_STATUS_REFERENCED,
                media_id,
                owner_platform_user_id,
                MEDIA_STATUS_PENDING,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError(f"media_ref is not claimable: {media_id}")
        row = conn.execute(
            "SELECT * FROM media_assets WHERE id = ?", (media_id,)
        ).fetchone()
        if row is None:  # pragma: no cover - UPDATE 刚刚命中，读不到只可能是并发删
            raise ValueError(f"media_ref disappeared: {media_id}")
        referenced.append(dict(row))
    return referenced


def update_media_transcript(
    *,
    media_id: str,
    owner_platform_user_id: str,
    transcript: Optional[str],
    conn: Optional[Connection] = None,
) -> bool:
    """回填语音转写文本；转写失败时不调用（保持 NULL，上层落兜底话术）。"""
    with _tx(conn) as tx:
        updated = tx.execute(
            "UPDATE media_assets SET transcript = ? WHERE id = ? AND owner_platform_user_id = ?",
            (transcript, str(media_id or "").strip(), str(owner_platform_user_id or "").strip()),
        )
    return updated.rowcount == 1


def list_expired_pending_media_assets(
    *, limit: int = 200, now: Optional[str] = None, conn: Optional[Connection] = None
) -> List[Dict[str, Any]]:
    """回收 job 的唯一扫描路径（走 ``ix_media_assets_reclaim``）。"""
    cutoff = now or beijing_now_str()
    with _tx(conn) as tx:
        rows = tx.execute(
            "SELECT * FROM media_assets WHERE status = ? AND expires_at IS NOT NULL "
            "AND expires_at <= ? ORDER BY expires_at ASC LIMIT ?",
            (MEDIA_STATUS_PENDING, cutoff, max(int(limit), 1)),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_media_asset_row(
    *, media_id: str, conn: Optional[Connection] = None, only_pending: bool = True
) -> bool:
    """删除资产行；``only_pending`` 保证回收 job 不会误删已被引用的媒体。"""
    sql = "DELETE FROM media_assets WHERE id = ?"
    params: List[Any] = [str(media_id or "").strip()]
    if only_pending:
        sql += " AND status = ?"
        params.append(MEDIA_STATUS_PENDING)
    with _tx(conn) as tx:
        deleted = tx.execute(sql, tuple(params))
    return deleted.rowcount == 1
