"""主动消息选择层（推荐器的"排序 + 选择"段）。

把"从多个召回候选里挑哪一条发"从 `plan_reactivation_candidate` 的内联 if/else
提成独立、可替换的一层：

- `candidate.ProactiveCandidate`：统一候选协议，与现有 reactivation 候选 dict 双向 round-trip。
- `ranker`：离散族（自主外联）优先级登记，纯数据，沿 `categories.py` registry 范式。
- `selector.select_first`：纯机制，按 rank 顺序短路选择，零 proactive 依赖（不会成环）。

阶段2 只引入接缝、行为等价（平凡 ranker = 现有固定优先级）；阶段3 在 selector 背后换成
跨账号候选池表，阶段4 接真实排序信号（兴趣/回复率/疲劳）—— 上层 planning/scheduler 不再动。
"""
