"""存储层：主动消息候选与账号主动状态的持久化（I/O）。

`candidates`：自主外联候选的持久化（account_state metadata）+ 归一化 + 去重规则。
`account_state`：账号级 proactive 状态 CRUD。账号隔离不变量在此层落地。
"""
