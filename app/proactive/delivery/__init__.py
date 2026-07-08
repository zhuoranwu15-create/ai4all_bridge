"""派发/发送层：出站闸门 + 实际发送——离散候选与义务型共享的"地板"。

`policy`：硬约束闸门（配额/时段/avoidance/内容偏好）。
`outbound`：出站账本 + openclaw 网关（policy 在此被调用）。
`dispatch`：自主外联候选到期后的派发（revalidate → policy → 发送 → 状态机）。
`account_check`：admin 手工关怀候选的 decide/execute。
`touch_state`：微信送达窗口可触达性判断（`get_account_touch_state`），
候选生成层在生成前调用，不属于 policy 的配额/时段闸门。
"""
