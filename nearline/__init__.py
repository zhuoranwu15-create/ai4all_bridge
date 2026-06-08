"""nearline：离线数据分析基建（与主 app 解耦，只读操作库）。

分层：操作库(只读) → facts.sqlite3(dim_/fct_，只追加) → marts.sqlite3(agg_，可重建) → 报告。
口径事实源见 docs/tech_design/analytics_foundation_design.md。
"""
