import json


def test_simulate_dispatch_reads_candidate_from_result_file(tmp_path):
    from scripts.simulate_content_invitation_dispatch import (
        load_candidates_from_result_files,
        simulate_dispatch_rows,
    )

    result_file = tmp_path / "diagnosis.json"
    result_file.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "account_id": "acc-1",
                        "generation_run": {
                            "content_invitation": {
                                "id": "cinv-1",
                                "topic": "亲子陪伴",
                                "invitation_text": "要不要看几条亲子陪伴的小内容？",
                                "title_count": 3,
                                "titles": [
                                    {"title": "标题一"},
                                    {"title": "标题二"},
                                    {"title": "标题三"},
                                ],
                            }
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    candidates = load_candidates_from_result_files([str(result_file)])
    rows = simulate_dispatch_rows(candidates, send_at="2026-06-06 12:00:00")

    assert rows[0]["account_id"] == "acc-1"
    assert rows[0]["actual_dispatch_text"] == "要不要看几条亲子陪伴的小内容？"
    assert rows[0]["title_count"] == 3
    assert rows[0]["titles"][0]["title"] == "标题一"
