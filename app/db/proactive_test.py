"""DB helpers for the local proactive message test lab."""
import json
from typing import Any, Dict, List, Optional

from app.db._backend import Row
from app.db._core import _clean_text, _new_id, connect

__all__ = [
    "create_proactive_test_candidate",
    "get_proactive_test_candidate",
    "get_proactive_test_sample_by_sample_id",
    "get_proactive_test_long_context",
    "hydrate_proactive_test_sample_context",
    "import_proactive_test_sample",
    "list_proactive_test_candidates",
    "list_proactive_test_samples",
    "proactive_test_stats",
    "upsert_proactive_test_review",
]


def _decode_json_object(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _decode_sample(row: Row) -> Dict[str, Any]:
    item = dict(row)
    item["chat_history"] = _decode_json_object(item.pop("chat_history_json", None), [])
    return item


def _decode_candidate(row: Row) -> Dict[str, Any]:
    item = dict(row)
    item["model_should_send"] = (
        None if item.get("model_should_send") is None else bool(item.get("model_should_send"))
    )
    item["model_raw"] = _decode_json_object(item.get("model_raw_json"), None)
    for source, target in (
        ("l0_context_json", "l0_context"),
        ("l1_trigger_json", "l1_trigger"),
        ("l2_when_json", "l2_when"),
        ("l3_how_json", "l3_how"),
        ("l4_safety_json", "l4_safety"),
        ("l5_outcome_json", "l5_outcome"),
    ):
        if source in item:
            item[target] = _decode_json_object(item.pop(source, None), {})
    item["review_status"] = "reviewed" if item.get("review_id") else "unreviewed"
    for key in ("human_should_promote", "privacy_risk", "hallucination_risk"):
        if key in item and item[key] is not None:
            item[key] = bool(item[key])
    return item


def import_proactive_test_sample(
    *,
    sample_id: str,
    source: str,
    scenario_type: str,
    chat_history: List[Dict[str, Any]],
    silence_hours: Optional[float] = None,
    expected_active_message_type: Optional[str] = None,
    notes: Optional[str] = None,
    user_context: Optional[str] = None,
    memory_evidence: Optional[str] = None,
    open_loop: Optional[str] = None,
    account_id: Optional[str] = None,
    session_id: Optional[int] = None,
    context_limit: Optional[int] = None,
    context_source: Optional[str] = None,
) -> Dict[str, Any]:
    cleaned_sample_id = _clean_text(sample_id)
    cleaned_source = _clean_text(source) or "manual"
    cleaned_scenario = _clean_text(scenario_type)
    if not cleaned_sample_id:
        raise ValueError("sample_id is required")
    if cleaned_scenario not in {"account_check", "reactivation_topic", "content_invitation"}:
        raise ValueError("scenario_type is invalid")
    if not isinstance(chat_history, list) or not chat_history:
        raise ValueError("chat_history must be a non-empty list")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO proactive_test_samples(
                id, sample_id, source, scenario_type, chat_history_json,
                silence_hours, expected_active_message_type, notes,
                user_context, memory_evidence, open_loop, account_id, session_id,
                context_limit, context_source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')), strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(sample_id) DO UPDATE SET
                source = excluded.source,
                scenario_type = excluded.scenario_type,
                chat_history_json = excluded.chat_history_json,
                silence_hours = excluded.silence_hours,
                expected_active_message_type = excluded.expected_active_message_type,
                notes = excluded.notes,
                user_context = excluded.user_context,
                memory_evidence = excluded.memory_evidence,
                open_loop = excluded.open_loop,
                account_id = excluded.account_id,
                session_id = excluded.session_id,
                context_limit = excluded.context_limit,
                context_source = excluded.context_source,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            """,
            (
                _new_id("pts"),
                cleaned_sample_id,
                cleaned_source,
                cleaned_scenario,
                json.dumps(chat_history, ensure_ascii=False),
                silence_hours,
                _clean_text(expected_active_message_type),
                _clean_text(notes),
                _clean_text(user_context),
                _clean_text(memory_evidence),
                _clean_text(open_loop),
                _clean_text(account_id),
                session_id,
                max(1, min(int(context_limit or 120), 200)),
                _clean_text(context_source),
            ),
        )
        row = conn.execute(
            "SELECT * FROM proactive_test_samples WHERE sample_id = ?",
            (cleaned_sample_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError("proactive_test_sample was not created")
    return _decode_sample(row)


def get_proactive_test_sample_by_sample_id(sample_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM proactive_test_samples WHERE sample_id = ?",
            (_clean_text(sample_id),),
        ).fetchone()
    return _decode_sample(row) if row else None


def _message_rows_to_chat(rows: List[Row]) -> List[Dict[str, Any]]:
    return [
        {
            "role": row["role"],
            "text": row["content"],
            "created_at": row["created_at"],
            "message_id": row["message_id"],
            "session_id": row["session_id"],
        }
        for row in rows
        if _clean_text(row["content"])
    ]


def _fetch_real_context(
    *,
    account_id: str,
    session_id: Optional[int],
    limit: int,
) -> tuple[List[Dict[str, Any]], str]:
    clauses = [
        "account_id = ?",
        "content IS NOT NULL",
        "content != ''",
        "NOT (role = 'assistant' AND error IS NOT NULL AND error != '')",
        "NOT (error IS NOT NULL AND error = 'moderation_blocked')",
    ]
    params: List[Any] = [account_id]
    source = "account_recent"
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(int(session_id))
        source = f"session:{session_id}"
    where = " AND ".join(clauses)
    with connect() as conn:
        if session_id is not None:
            session = conn.execute(
                "SELECT account_id FROM sessions WHERE id = ?",
                (int(session_id),),
            ).fetchone()
            if session is None or session["account_id"] != account_id:
                return [], "session_account_mismatch"
        rows = conn.execute(
            f"""
            SELECT id, session_id, message_id, role, content, created_at
            FROM messages
            WHERE {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    return _message_rows_to_chat(list(reversed(rows))), source


def get_proactive_test_long_context(
    *,
    account_id: str,
    session_id: Optional[int] = None,
    limit: int = 120,
) -> tuple[List[Dict[str, Any]], str]:
    cleaned_account_id = _clean_text(account_id)
    if not cleaned_account_id:
        return [], "missing_account_id"
    return _fetch_real_context(
        account_id=cleaned_account_id,
        session_id=session_id,
        limit=max(1, min(int(limit), 200)),
    )


def hydrate_proactive_test_sample_context(sample: Dict[str, Any]) -> Dict[str, Any]:
    """Refresh a test sample's chat_history from bound account/session when available."""
    account_id = _clean_text(sample.get("account_id"))
    if not account_id:
        sample["context_source"] = sample.get("context_source") or "sample_chat_history"
        return sample
    session_id = sample.get("session_id")
    try:
        cleaned_session_id = int(session_id) if session_id is not None else None
    except (TypeError, ValueError):
        cleaned_session_id = None
    limit = max(1, min(int(sample.get("context_limit") or 120), 200))
    chat_history, source = _fetch_real_context(
        account_id=account_id,
        session_id=cleaned_session_id,
        limit=limit,
    )
    if not chat_history:
        sample["context_source"] = source
        return sample
    with connect() as conn:
        conn.execute(
            """
            UPDATE proactive_test_samples
            SET chat_history_json = ?, context_source = ?,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            WHERE sample_id = ?
            """,
            (
                json.dumps(chat_history, ensure_ascii=False),
                source,
                sample["sample_id"],
            ),
        )
    refreshed = dict(sample)
    refreshed["chat_history"] = chat_history
    refreshed["context_source"] = source
    return refreshed


def list_proactive_test_samples(
    *,
    source: Optional[str] = None,
    scenario_type: Optional[str] = None,
    review_status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if source:
        clauses.append("s.source = ?")
        params.append(source)
    if scenario_type:
        clauses.append("s.scenario_type = ?")
        params.append(scenario_type)
    if review_status == "reviewed":
        clauses.append(
            "EXISTS (SELECT 1 FROM proactive_test_candidates c JOIN proactive_test_reviews r ON r.candidate_id = c.id WHERE c.sample_id = s.sample_id)"
        )
    elif review_status == "unreviewed":
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM proactive_test_candidates c JOIN proactive_test_reviews r ON r.candidate_id = c.id WHERE c.sample_id = s.sample_id)"
        )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT s.*
            FROM proactive_test_samples s
            {where}
            ORDER BY s.updated_at DESC, s.sample_id ASC
            LIMIT ? OFFSET ?
            """,
            (*params, max(1, min(int(limit), 500)), max(0, int(offset))),
        ).fetchall()
    return [_decode_sample(row) for row in rows]


def create_proactive_test_candidate(
    *,
    sample_id: str,
    run_id: str,
    scenario_type: str,
    generated_type: Optional[str],
    generated_text: Optional[str],
    model_should_send: Optional[bool],
    model_confidence: Optional[float],
    model_reason: Optional[str],
    model_raw_json: Any,
    generation_status: str,
    generation_error: Optional[str] = None,
    trigger_type: Optional[str] = None,
    candidate_message: Optional[str] = None,
    should_send_score: Optional[int] = None,
    when_reason: Optional[str] = None,
    content_quality: Optional[str] = None,
    risk_tag: Optional[str] = None,
    l0_context: Optional[Dict[str, Any]] = None,
    l1_trigger: Optional[Dict[str, Any]] = None,
    l2_when: Optional[Dict[str, Any]] = None,
    l3_how: Optional[Dict[str, Any]] = None,
    l4_safety: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    candidate_id = _new_id("ptc")
    raw_text = (
        model_raw_json
        if isinstance(model_raw_json, str)
        else json.dumps(model_raw_json, ensure_ascii=False)
    )
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO proactive_test_candidates(
                id, sample_id, run_id, scenario_type, generated_type, generated_text,
                model_should_send, model_confidence, model_reason, model_raw_json,
                generation_status, generation_error, trigger_type, candidate_message,
                should_send_score, when_reason, content_quality, risk_tag,
                l0_context_json, l1_trigger_json, l2_when_json, l3_how_json,
                l4_safety_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')), strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            """,
            (
                candidate_id,
                _clean_text(sample_id),
                _clean_text(run_id),
                _clean_text(scenario_type),
                _clean_text(generated_type),
                _clean_text(generated_text),
                None if model_should_send is None else int(bool(model_should_send)),
                model_confidence,
                _clean_text(model_reason),
                raw_text,
                _clean_text(generation_status) or "error",
                _clean_text(generation_error),
                _clean_text(trigger_type),
                _clean_text(candidate_message) or _clean_text(generated_text),
                should_send_score,
                _clean_text(when_reason),
                _clean_text(content_quality),
                _clean_text(risk_tag),
                json.dumps(l0_context or {}, ensure_ascii=False),
                json.dumps(l1_trigger or {}, ensure_ascii=False),
                json.dumps(l2_when or {}, ensure_ascii=False),
                json.dumps(l3_how or {}, ensure_ascii=False),
                json.dumps(l4_safety or {}, ensure_ascii=False),
            ),
        )
    item = get_proactive_test_candidate(candidate_id)
    if item is None:
        raise RuntimeError("proactive_test_candidate was not created")
    return item


def get_proactive_test_candidate(candidate_id: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT c.*, r.id AS review_id, r.human_should_promote, r.reject_reason,
                   r.tone_score, r.pressure_score, r.marketing_score,
                   r.privacy_risk, r.hallucination_risk, r.review_notes, r.reviewer,
                   r.revised_message, r.final_label, r.promote_level, r.user_response,
                   r.l5_outcome_json
            FROM proactive_test_candidates c
            LEFT JOIN proactive_test_reviews r ON r.candidate_id = c.id
            WHERE c.id = ?
            """,
            (_clean_text(candidate_id),),
        ).fetchone()
    return _decode_candidate(row) if row else None


def list_proactive_test_candidates(
    *,
    run_id: Optional[str] = None,
    scenario_type: Optional[str] = None,
    generation_status: Optional[str] = None,
    review_status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if run_id:
        clauses.append("c.run_id = ?")
        params.append(run_id)
    if scenario_type:
        clauses.append("c.scenario_type = ?")
        params.append(scenario_type)
    if generation_status:
        clauses.append("c.generation_status = ?")
        params.append(generation_status)
    if review_status == "reviewed":
        clauses.append("r.id IS NOT NULL")
    elif review_status == "unreviewed":
        clauses.append("r.id IS NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT c.*, s.chat_history_json, s.silence_hours, s.source, s.notes,
                   s.user_context, s.memory_evidence, s.open_loop,
                   r.id AS review_id, r.human_should_promote, r.reject_reason,
                   r.tone_score, r.pressure_score, r.marketing_score,
                   r.privacy_risk, r.hallucination_risk, r.review_notes, r.reviewer,
                   r.revised_message, r.final_label, r.promote_level, r.user_response,
                   r.l5_outcome_json
            FROM proactive_test_candidates c
            LEFT JOIN proactive_test_samples s ON s.sample_id = c.sample_id
            LEFT JOIN proactive_test_reviews r ON r.candidate_id = c.id
            {where}
            ORDER BY c.created_at DESC, c.id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, max(1, min(int(limit), 500)), max(0, int(offset))),
        ).fetchall()
    items = []
    for row in rows:
        item = _decode_candidate(row)
        item["chat_history"] = _decode_json_object(item.pop("chat_history_json", None), [])
        items.append(item)
    return items


def upsert_proactive_test_review(
    *,
    candidate_id: str,
    human_should_promote: bool,
    reject_reason: Optional[str],
    tone_score: Optional[int],
    pressure_score: Optional[int],
    marketing_score: Optional[int],
    privacy_risk: bool,
    hallucination_risk: bool,
    review_notes: Optional[str],
    reviewer: Optional[str],
    revised_message: Optional[str] = None,
    final_label: Optional[str] = None,
    promote_level: Optional[str] = None,
    user_response: Optional[str] = None,
    l5_outcome: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cleaned_candidate_id = _clean_text(candidate_id)
    if not cleaned_candidate_id:
        raise ValueError("candidate_id is required")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO proactive_test_reviews(
                id, candidate_id, human_should_promote, reject_reason, tone_score,
                pressure_score, marketing_score, privacy_risk, hallucination_risk,
                review_notes, reviewer, revised_message, final_label, promote_level,
                user_response, l5_outcome_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')), strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(candidate_id) DO UPDATE SET
                human_should_promote = excluded.human_should_promote,
                reject_reason = excluded.reject_reason,
                tone_score = excluded.tone_score,
                pressure_score = excluded.pressure_score,
                marketing_score = excluded.marketing_score,
                privacy_risk = excluded.privacy_risk,
                hallucination_risk = excluded.hallucination_risk,
                review_notes = excluded.review_notes,
                reviewer = excluded.reviewer,
                revised_message = excluded.revised_message,
                final_label = excluded.final_label,
                promote_level = excluded.promote_level,
                user_response = excluded.user_response,
                l5_outcome_json = excluded.l5_outcome_json,
                updated_at = strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours'))
            """,
            (
                _new_id("ptr"),
                cleaned_candidate_id,
                int(bool(human_should_promote)),
                _clean_text(reject_reason),
                tone_score,
                pressure_score,
                marketing_score,
                int(bool(privacy_risk)),
                int(bool(hallucination_risk)),
                _clean_text(review_notes),
                _clean_text(reviewer),
                _clean_text(revised_message),
                _clean_text(final_label),
                _clean_text(promote_level),
                _clean_text(user_response),
                json.dumps(l5_outcome or {}, ensure_ascii=False),
            ),
        )
        row = conn.execute(
            "SELECT * FROM proactive_test_reviews WHERE candidate_id = ?",
            (cleaned_candidate_id,),
        ).fetchone()
    return dict(row) if row else {}


def proactive_test_stats() -> Dict[str, Any]:
    with connect() as conn:
        total_samples = conn.execute("SELECT COUNT(*) AS n FROM proactive_test_samples").fetchone()["n"]
        total_candidates = conn.execute("SELECT COUNT(*) AS n FROM proactive_test_candidates").fetchone()["n"]
        success_count = conn.execute(
            "SELECT COUNT(*) AS n FROM proactive_test_candidates WHERE generation_status = 'success'"
        ).fetchone()["n"]
        reviewed_count = conn.execute("SELECT COUNT(*) AS n FROM proactive_test_reviews").fetchone()["n"]
        promoted_count = conn.execute(
            "SELECT COUNT(*) AS n FROM proactive_test_reviews WHERE human_should_promote = 1"
        ).fetchone()["n"]
        by_type_rows = conn.execute(
            """
            SELECT c.scenario_type,
                   COUNT(*) AS count,
                   SUM(CASE WHEN r.human_should_promote = 1 THEN 1 ELSE 0 END) AS promoted,
                   SUM(CASE WHEN r.id IS NOT NULL THEN 1 ELSE 0 END) AS reviewed
            FROM proactive_test_candidates c
            LEFT JOIN proactive_test_reviews r ON r.candidate_id = c.id
            GROUP BY c.scenario_type
            """
        ).fetchall()
        reject_rows = conn.execute(
            """
            SELECT reject_reason, COUNT(*) AS count
            FROM proactive_test_reviews
            WHERE reject_reason IS NOT NULL AND reject_reason <> ''
            GROUP BY reject_reason
            ORDER BY count DESC, reject_reason ASC
            """
        ).fetchall()
        avg_row = conn.execute(
            """
            SELECT AVG(tone_score) AS tone_score,
                   AVG(pressure_score) AS pressure_score,
                   AVG(marketing_score) AS marketing_score
            FROM proactive_test_reviews
            """
        ).fetchone()
    by_type = {}
    for row in by_type_rows:
        reviewed = int(row["reviewed"] or 0)
        promoted = int(row["promoted"] or 0)
        by_type[row["scenario_type"]] = {
            "count": int(row["count"] or 0),
            "reviewed": reviewed,
            "promote_rate": (promoted / reviewed) if reviewed else 0.0,
        }
    return {
        "total_samples": int(total_samples or 0),
        "total_candidates": int(total_candidates or 0),
        "generation_success_rate": (success_count / total_candidates) if total_candidates else 0.0,
        "human_promote_rate": (promoted_count / reviewed_count) if reviewed_count else 0.0,
        "by_type": by_type,
        "reject_reasons": {row["reject_reason"]: int(row["count"] or 0) for row in reject_rows},
        "average_scores": {
            "tone_score": float(avg_row["tone_score"] or 0),
            "pressure_score": float(avg_row["pressure_score"] or 0),
            "marketing_score": float(avg_row["marketing_score"] or 0),
        },
    }
