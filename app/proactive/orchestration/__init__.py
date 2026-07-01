"""编排层：驱动"召回→选择→存储→调度→派发"的循环。

`planning`：per-account 规划（含单账号候选的 plan：召回→选择→落库）。
`scheduler`：系统级 tick 循环，步骤级隔离地调用各 dispatcher 与 planning。
"""
