import json
from pathlib import Path
from unittest.mock import patch


def test_prepare_synthetic_outputs_seed_samples(tmp_path):
    from app.proactive_test.dataset_prepare import prepare_datasets

    output = tmp_path / "synthetic.jsonl"
    summary = prepare_datasets(
        dataset_names=["synthetic"],
        limit=100,
        output_path=str(output),
        cache_dir=str(tmp_path / "cache"),
        seed=7,
    )

    assert summary["sample_count"] == 100
    assert summary["datasets"][0]["dataset"] == "synthetic"
    assert summary["datasets"][0]["converted"] == 100
    assert "expanded synthetic seed" in summary["datasets"][0]["reason"]
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 100
    assert {row["scenario_type"] for row in rows} == {
        "account_check",
        "reactivation_topic",
        "content_invitation",
    }
    assert all(row["source"] == "synthetic" for row in rows)
    assert any(row["sample_id"].startswith("synthetic_generated_") for row in rows)


def test_prepare_synthetic_imports_db_and_skips_existing(tmp_path, fresh_db):
    from app.db import get_proactive_test_sample_by_sample_id
    from app.proactive_test.dataset_prepare import prepare_datasets

    output = tmp_path / "import.jsonl"
    first = prepare_datasets(
        dataset_names=["synthetic"],
        limit=3,
        output_path=str(output),
        cache_dir=str(tmp_path / "cache"),
        import_db=True,
    )
    second = prepare_datasets(
        dataset_names=["synthetic"],
        limit=3,
        output_path=str(output),
        cache_dir=str(tmp_path / "cache"),
        import_db=True,
    )

    assert first["import_db"]["inserted"] == 3
    assert second["import_db"]["skipped_existing"] == 3
    first_sample_id = json.loads(output.read_text(encoding="utf-8").splitlines()[0])["sample_id"]
    assert get_proactive_test_sample_by_sample_id(first_sample_id) is not None


def test_prepare_mixed_survives_naturalconv_download_failure(tmp_path):
    from app.proactive_test.dataset_prepare import prepare_datasets

    with patch(
        "app.proactive_test.converters.naturalconv._download",
        return_value=(False, "network unavailable"),
    ):
        summary = prepare_datasets(
            dataset_names=["synthetic", "naturalconv"],
            limit=2,
            output_path=str(tmp_path / "mixed.jsonl"),
            cache_dir=str(tmp_path / "cache"),
        )

    assert summary["sample_count"] == 2
    naturalconv = next(item for item in summary["datasets"] if item["dataset"] == "naturalconv")
    assert "network unavailable" in naturalconv["reason"]
    assert "manually download NaturalConv files" in naturalconv["reason"]
    assert "NaturalConv: non-commercial research purpose only" in summary["license_reminders"]


def test_prepare_naturalconv_converts_local_jsonl(tmp_path):
    from app.proactive_test.dataset_prepare import prepare_datasets

    raw = tmp_path / "cache" / "naturalconv" / "raw"
    raw.mkdir(parents=True)
    (raw / "sample.jsonl").write_text(
        json.dumps({"conversation": ["我想找云南旅行攻略", "可以先看路线和季节。"]}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    summary = prepare_datasets(
        dataset_names=["naturalconv"],
        limit=10,
        output_path=str(tmp_path / "naturalconv.jsonl"),
        cache_dir=str(tmp_path / "cache"),
    )

    assert summary["sample_count"] == 1
    rows = [
        json.loads(line)
        for line in Path(summary["output"]).read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["dataset_name"] == "naturalconv"
    assert rows[0]["source"] == "public_dataset"
    assert rows[0]["scenario_type"] == "content_invitation"


def test_prepare_reserved_dataset_reports_friendly_reason(tmp_path):
    from app.proactive_test.dataset_prepare import prepare_datasets

    summary = prepare_datasets(
        dataset_names=["cped"],
        output_path=str(tmp_path / "cped.jsonl"),
        cache_dir=str(tmp_path / "cache"),
    )

    assert summary["sample_count"] == 0
    assert "reserved for a later iteration" in summary["datasets"][0]["reason"]
