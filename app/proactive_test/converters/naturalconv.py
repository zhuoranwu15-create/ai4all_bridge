from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.proactive_test.converters.common import (
    DatasetResult,
    make_sample,
    normalize_chat_history,
    sample_dialogues,
)

_ZIP_URL = "https://github.com/naturalconv/NaturalConvDataSet/archive/refs/heads/master.zip"
_LICENSE = "NaturalConv: non-commercial research purpose only"


def _download(cache_root: Path, *, force_download: bool) -> tuple[bool, Optional[str]]:
    raw_dir = cache_root / "naturalconv" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    if any(raw_dir.iterdir()) and not force_download:
        return False, None
    if force_download and raw_dir.exists():
        shutil.rmtree(raw_dir)
        raw_dir.mkdir(parents=True, exist_ok=True)
    zip_path = raw_dir / "naturalconv.zip"
    try:
        urllib.request.urlretrieve(_ZIP_URL, zip_path)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(raw_dir)
        return True, None
    except (urllib.error.URLError, OSError, zipfile.BadZipFile) as err:
        return False, str(err)


def _iter_texts(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        text = value.strip()
        if text:
            yield text
        return
    if isinstance(value, dict):
        for key in ("text", "utterance", "content", "response", "message"):
            if key in value:
                yield from _iter_texts(value[key])
                return
        for key in ("conversation", "dialogue", "dialog", "messages", "utterances"):
            if key in value:
                yield from _iter_texts(value[key])
                return
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_texts(item)


def _load_json_file(path: Path) -> List[List[Dict[str, str]]]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    candidates = parsed if isinstance(parsed, list) else [parsed]
    dialogues: List[List[Dict[str, str]]] = []
    for item in candidates:
        texts = list(_iter_texts(item))
        if len(texts) >= 2:
            dialogues.append([{"text": text} for text in texts])
    return dialogues


def _load_jsonl_file(path: Path) -> List[List[Dict[str, str]]]:
    dialogues: List[List[Dict[str, str]]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return dialogues
    for line in lines:
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        texts = list(_iter_texts(parsed))
        if len(texts) >= 2:
            dialogues.append([{"text": item} for item in texts])
    return dialogues


def _load_txt_file(path: Path) -> List[List[Dict[str, str]]]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    dialogues = []
    blocks = [block for block in text.split("\n\n") if block.strip()]
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) >= 2:
            dialogues.append([{"text": line.split("\t")[-1]} for line in lines])
    return dialogues


def _load_dialogues(raw_dir: Path) -> List[List[Dict[str, str]]]:
    dialogues: List[List[Dict[str, str]]] = []
    for path in raw_dir.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".json":
            dialogues.extend(_load_json_file(path))
        elif suffix == ".jsonl":
            dialogues.extend(_load_jsonl_file(path))
        elif suffix in {".txt", ".tsv"}:
            dialogues.extend(_load_txt_file(path))
    return dialogues


def convert(
    *,
    limit: int,
    seed: int,
    scenario_types: Tuple[str, ...],
    default_silence_hours: Optional[float],
    cache_dir: Path,
    force_download: bool = False,
    **_: Any,
) -> tuple[list[dict[str, Any]], DatasetResult]:
    result = DatasetResult(dataset="naturalconv", license_reminders=[_LICENSE])
    downloaded, error = _download(cache_dir, force_download=force_download)
    result.downloaded = downloaded
    raw_dir = cache_dir / "naturalconv" / "raw"
    if error:
        result.reason = (
            f"download failed: {error}; manually download NaturalConv files to {raw_dir}"
        )
    dialogues = _load_dialogues(raw_dir) if raw_dir.exists() else []
    result.loaded = len(dialogues)
    if not dialogues:
        if not result.reason:
            result.reason = f"no NaturalConv dialogues found; manually download files to {raw_dir}"
        return [], result

    samples: List[Dict[str, Any]] = []
    selected = sample_dialogues(dialogues, limit=limit, seed=seed)
    for idx, dialogue in enumerate(selected, start=1):
        try:
            history, flags = normalize_chat_history(dialogue)
            if len(history) < 2:
                result.skipped += 1
                continue
            notes = "; ".join([*flags, "auto-converted from naturalconv", _LICENSE])
            samples.append(
                make_sample(
                    dataset_name="naturalconv",
                    index=idx,
                    source="public_dataset",
                    chat_history=history,
                    scenario_types=scenario_types,
                    default_silence_hours=default_silence_hours,
                    notes=notes,
                )
            )
        except Exception as err:  # noqa: BLE001 - per-dialogue conversion should not stop batch
            result.skipped += 1
            result.errors.append({"dataset": "naturalconv", "index": idx, "error": str(err)})
    result.converted = len(samples)
    return samples, result
