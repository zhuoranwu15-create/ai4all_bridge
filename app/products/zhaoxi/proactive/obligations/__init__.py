"""义务型主动消息：用户锚定、时间精确、不走推荐器。

`reminders` / `commitments`：用户明确要或承诺过的消息，绑 due_at 到点直发，policy 里豁免配额。
`content_invitations`：内容邀请行的过期维护。

义务型只向下够到 delivery（+ 一个 account_state 标记），不穿透 recall/selection/scheduling。
未来"运营强插"（公告/活动/强推）不走这里，而是作为带优先级的候选注入选择层。
"""
