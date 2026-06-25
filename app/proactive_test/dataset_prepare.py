from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.db import get_proactive_test_sample_by_sample_id, import_proactive_test_sample, init_db
from app.proactive_test.converters.common import DatasetResult, write_jsonl

SUPPORTED_DATASETS = {"synthetic", "naturalconv", "cped", "dailydialog", "empathetic"}
RESERVED_DATASETS = {"cped", "dailydialog", "empathetic"}


def _expand_datasets(dataset_names: List[str]) -> List[str]:
    expanded: List[str] = []
    for name in dataset_names:
        cleaned = name.strip().lower()
        if not cleaned:
            continue
        if cleaned == "mixed":
            expanded.extend(["synthetic", "naturalconv"])
        else:
            expanded.append(cleaned)
    return list(dict.fromkeys(expanded))


def _scenario_tuple(value: Optional[str]) -> Tuple[str, ...]:
    allowed = []
    for part in str(value or "account_check,reactivation_topic,content_invitation").split(","):
        cleaned = part.strip()
        if cleaned in {"account_check", "reactivation_topic", "content_invitation"}:
            allowed.append(cleaned)
    return tuple(allowed or ["reactivation_topic"])


def _reserved_result(name: str) -> DatasetResult:
    reason = (
        f"{name} converter is reserved for a later iteration; "
        f"manually place converted samples in the output JSONL if needed"
    )
    if name in {"dailydialog", "empathetic"}:
        reason += "; optional Hugging Face support will require installing datasets"
    return DatasetResult(dataset=name, downloaded=False, reason=reason)


def _import_samples(samples: List[Dict[str, Any]], *, upsert: bool) -> Dict[str, Any]:
    init_db()
    inserted = 0
    skipped_existing = 0
    failed = 0
    errors = []
    for sample in samples:
        sample_id = str(sample.get("sample_id") or "").strip()
        if not sample_id:
            failed += 1
            errors.append({"sample_id": None, "error": "sample_id is required"})
            continue
        if not upsert and get_proactive_test_sample_by_sample_id(sample_id) is not None:
            skipped_existing += 1
            continue
        try:
            import_proactive_test_sample(
                sample_id=sample_id,
                source=sample.get("source") or "public_dataset",
                scenario_type=sample.get("scenario_type"),
                chat_history=sample.get("chat_history") or [],
                silence_hours=sample.get("silence_hours"),
                expected_active_message_type=sample.get("expected_active_message_type"),
                notes=sample.get("notes"),
            )
            inserted += 1
        except Exception as err:  # noqa: BLE001 - import should continue per sample
            failed += 1
            errors.append({"sample_id": sample_id, "error": str(err)})
    return {
        "enabled": True,
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "failed": failed,
        "errors": errors,
    }


def prepare_datasets(
    *,
    dataset_names: List[str],
    limit: int = 100,
    output_path: str = "data/generated/proactive_samples.jsonl",
    import_db: bool = False,
    upsert: bool = False,
    force_download: bool = False,
    cache_dir: str = "data/cache/dialogue_datasets",
    language: str = "all",
    scenario_types: Optional[str] = None,
    default_silence_hours: Optional[float] = None,
    seed: int = 42,
) -> Dict[str, Any]:
    output = Path(output_path)
    cache_root = Path(cache_dir)
    names = _expand_datasets(dataset_names)
    samples: List[Dict[str, Any]] = []
    results: List[DatasetResult] = []
    errors: List[Dict[str, Any]] = []
    scenario_tuple = _scenario_tuple(scenario_types)

    for name in names:
        if name not in SUPPORTED_DATASETS:
            result = DatasetResult(dataset=name, reason="unsupported dataset")
            results.append(result)
            errors.append({"dataset": name, "error": result.reason})
            continue
        if name in RESERVED_DATASETS:
            result = _reserved_result(name)
            results.append(result)
            errors.append({"dataset": name, "error": result.reason})
            continue
        try:
            module = importlib.import_module(f"app.proactive_test.converters.{name}")
            converted, result = module.convert(
                limit=max(1, int(limit)),
                seed=int(seed),
                scenario_types=scenario_tuple,
                default_silence_hours=default_silence_hours,
                cache_dir=cache_root,
                force_download=force_download,
                language=language,
            )
            samples.extend(converted)
            results.append(result)
            errors.extend(result.errors)
            if result.reason and not converted:
                errors.append({"dataset": name, "error": result.reason})
        except Exception as err:  # noqa: BLE001 - one dataset must not block the others
            result = DatasetResult(dataset=name, reason=str(err))
            results.append(result)
            errors.append({"dataset": name, "error": str(err)})

    write_jsonl(output, samples)
    error_path = output.with_name(output.stem + ".errors.jsonl")
    write_jsonl(error_path, errors)
    import_summary = (
        _import_samples(samples, upsert=upsert)
        if import_db
        else {"enabled": False, "inserted": 0, "skipped_existing": 0, "failed": 0, "errors": []}
    )
    license_reminders: List[str] = []
    for result in results:
        license_reminders.extend(result.license_reminders)
    return {
        "status": "ok",
        "datasets": [result.__dict__ for result in results],
        "output": str(output),
        "errors_output": str(error_path),
        "sample_count": len(samples),
        "import_db": import_summary,
        "license_reminders": list(dict.fromkeys(license_reminders)),
    }
