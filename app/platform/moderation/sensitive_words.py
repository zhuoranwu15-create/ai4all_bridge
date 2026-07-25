import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings
from app.platform.moderation.models import (
    RuleDecision,
    SyncModerationDecision,
    max_risk_level,
    normalize_risk_level,
)


DEFAULT_RULES_VERSION = "moderation_rules_v1"


def _terms_path(path: Optional[str] = None) -> Path:
    configured = path or getattr(
        settings,
        "moderation_sensitive_terms_path",
        "data/moderation/sensitive_terms.json",
    )
    return Path(str(configured)).expanduser()


def _normalize_categories(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_scopes(value: Any) -> List[str]:
    if not isinstance(value, list) or not value:
        return ["inbound", "outbound", "internal"]
    return [str(item).strip().lower() for item in value if str(item).strip()]


def _term_matches(term: Dict[str, Any], text: str) -> bool:
    needle = str(term.get("term") or "").strip()
    if not needle:
        return False
    case_sensitive = bool(term.get("case_sensitive"))
    haystack = text if case_sensitive else text.lower()
    pattern = needle if case_sensitive else needle.lower()
    match_type = str(term.get("match_type") or "contains").strip().lower()
    if match_type == "exact":
        return haystack == pattern
    if match_type == "regex":
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            return re.search(needle, text, flags=flags) is not None
        except re.error:
            return False
    return pattern in haystack


@lru_cache(maxsize=16)
def _load_sensitive_terms_for_path(path: str) -> Dict[str, Any]:
    target = Path(path)
    if not target.exists():
        return {"version": DEFAULT_RULES_VERSION, "terms": []}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": DEFAULT_RULES_VERSION, "terms": []}
    if not isinstance(data, dict):
        return {"version": DEFAULT_RULES_VERSION, "terms": []}
    terms = data.get("terms")
    if not isinstance(terms, list):
        terms = []
    return {
        "version": str(data.get("version") or DEFAULT_RULES_VERSION),
        "terms": [item for item in terms if isinstance(item, dict)],
    }


def load_sensitive_terms(path: Optional[str] = None) -> Dict[str, Any]:
    """Load the local sensitive-term JSON file used by deterministic rules."""

    return _load_sensitive_terms_for_path(str(_terms_path(path)))


def clear_sensitive_terms_cache() -> None:
    """Clear the sensitive-term file cache; tests call this after changing files."""

    _load_sensitive_terms_for_path.cache_clear()


def check_text_rules(
    *,
    account_id: str,
    text: Optional[str],
    direction: str,
    content_kind: str,
    terms_path: Optional[str] = None,
) -> RuleDecision:
    """Run deterministic sensitive-word checks for one text snapshot."""

    del account_id
    normalized_text = str(text or "")
    rules = load_sensitive_terms(terms_path)
    matched_terms: List[Dict[str, Any]] = []
    levels: List[str] = []
    categories: List[str] = []
    normalized_direction = str(direction or "").strip().lower()
    normalized_content_kind = str(content_kind or "").strip().lower()

    for term in rules["terms"]:
        if term.get("enabled") is False:
            continue
        if normalized_direction not in _normalize_scopes(term.get("scopes")):
            continue
        content_kinds = _normalize_scopes(term.get("content_kinds"))
        if term.get("content_kinds") and normalized_content_kind not in content_kinds:
            continue
        if not _term_matches(term, normalized_text):
            continue

        level = normalize_risk_level(term.get("level"), default="review")
        term_categories = _normalize_categories(term.get("categories"))
        levels.append(level)
        categories.extend(term_categories)
        matched_terms.append(
            {
                "id": str(term.get("id") or term.get("term") or "term"),
                "level": level,
                "categories": term_categories,
                "match_type": str(term.get("match_type") or "contains"),
            }
        )

    level = max_risk_level(levels) if levels else "pass"
    unique_categories = list(dict.fromkeys(categories))
    reason = "matched deterministic moderation rule" if matched_terms else "no deterministic rule matched"
    return RuleDecision(
        level=level,
        categories=unique_categories,
        matched_terms=matched_terms,
        reason=reason,
        confidence=1.0 if matched_terms else None,
        policy_version=str(rules["version"]),
    )


def check_sync_guard(
    *,
    account_id: str,
    text: Optional[str],
    direction: str,
    content_kind: str,
    source_type: str,
    source_id: str,
) -> SyncModerationDecision:
    """Run the synchronous outbound guard without calling external services."""

    del source_type, source_id
    if not bool(getattr(settings, "moderation_enabled", True)):
        return SyncModerationDecision(allowed=True)
    if not bool(getattr(settings, "moderation_sync_guard_enabled", True)):
        return SyncModerationDecision(allowed=True)
    if str(direction or "").strip().lower() != "outbound":
        return SyncModerationDecision(allowed=True)

    decision = check_text_rules(
        account_id=account_id,
        text=text,
        direction=direction,
        content_kind=content_kind,
    )
    blocked = decision.level in {"block", "escalate"}
    return SyncModerationDecision(
        allowed=not blocked,
        level=decision.level,
        categories=decision.categories,
        matched_terms=decision.matched_terms,
        reason=decision.reason,
        policy_version=decision.policy_version,
    )
