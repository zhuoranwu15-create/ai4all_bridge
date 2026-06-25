from __future__ import annotations

import argparse
import json
from typing import Any, Dict

from app.proactive_test.dataset_prepare import prepare_datasets


def _print_summary(summary: Dict[str, Any]) -> None:
    print("Dataset preparation complete")
    print("datasets:")
    for item in summary.get("datasets", []):
        downloaded = item.get("downloaded")
        downloaded_text = "n/a" if downloaded is None else ("yes" if downloaded else "no")
        line = (
            f"- {item.get('dataset')}: downloaded {downloaded_text}, "
            f"loaded {item.get('loaded', 0)}, converted {item.get('converted', 0)}, "
            f"skipped {item.get('skipped', 0)}"
        )
        if item.get("reason"):
            line += f", reason: {item['reason']}"
        print(line)
    print("output:")
    print(summary.get("output"))
    print("errors_output:")
    print(summary.get("errors_output"))
    print("import_db:")
    import_db = summary.get("import_db") or {}
    print(json.dumps(import_db, ensure_ascii=False, indent=2))
    reminders = summary.get("license_reminders") or []
    if reminders:
        print("license reminders:")
        for reminder in reminders:
            print(f"- {reminder}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare public dialogue datasets for Proactive Test Lab.")
    parser.add_argument("--dataset", default="synthetic", help="synthetic,naturalconv,cped,dailydialog,empathetic,mixed")
    parser.add_argument("--limit", type=int, default=100, help="Maximum samples per dataset.")
    parser.add_argument("--output", default="data/generated/proactive_samples.jsonl")
    parser.add_argument("--import-db", action="store_true", help="Import generated samples into proactive_test_samples.")
    parser.add_argument("--upsert", action="store_true", help="Overwrite existing sample_id rows when importing.")
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument("--cache-dir", default="data/cache/dialogue_datasets")
    parser.add_argument("--language", choices=("zh", "en", "all"), default="all")
    parser.add_argument("--scenario-types", default="account_check,reactivation_topic,content_invitation")
    parser.add_argument("--default-silence-hours", type=float, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON summary.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    summary = prepare_datasets(
        dataset_names=[part.strip() for part in args.dataset.split(",") if part.strip()],
        limit=args.limit,
        output_path=args.output,
        import_db=args.import_db,
        upsert=args.upsert,
        force_download=args.force_download,
        cache_dir=args.cache_dir,
        language=args.language,
        scenario_types=args.scenario_types,
        default_silence_hours=args.default_silence_hours,
        seed=args.seed,
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_summary(summary)


if __name__ == "__main__":
    main()
