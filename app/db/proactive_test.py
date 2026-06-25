"""DB helpers for the local proactive message test lab."""
import json
from typing import Any, Dict, List, Optional

from app.db._backend import Row
from app.db._core import _clean_text, _new_id, connect

__all__ = [
    "create_proactive_test_candidate",
    "get_proactive_test_candidate",
    "get_proactive_test_sample_by_sample_id",
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
                silence_hours, expected_active_message_type, notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')), strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
            ON CONFLICT(sample_id) DO UPDATE SET
                source = excluded.source,
                scenario_type = excluded.scenario_type,
                chat_history_json = excluded.chat_history_json,
                silence_hours = excluded.silence_hours,
                expected_active_message_type = excluded.expected_active_message_type,
                notes = excluded.notes,
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
                generation_status, generation_error, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')), strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
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
                   r.privacy_risk, r.hallucination_risk, r.review_notes, r.reviewer
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
                   r.id AS review_id, r.human_should_promote, r.reject_reason,
                   r.tone_score, r.pressure_score, r.marketing_score,
                   r.privacy_risk, r.hallucination_risk, r.review_notes, r.reviewer
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
                review_notes, reviewer, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')), strftime('%Y-%m-%d %H:%M:%S', datetime('now', '+8 hours')))
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
