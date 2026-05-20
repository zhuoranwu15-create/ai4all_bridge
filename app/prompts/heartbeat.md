# HEARTBEAT

这是 AI4ALL 实例级 heartbeat 策略文件。

用途：

- 监督后端、OpenClaw bridge、gateway、队列、LLM 可用性和账号连接状态。
- 约束实例级健康检查、调度和告警策略。
- 明确普通用户聊天 prompt 不注入本文件。

用户主动触达、提醒、定时任务应通过独立用户任务模型处理，不从本文件读取个人任务。
