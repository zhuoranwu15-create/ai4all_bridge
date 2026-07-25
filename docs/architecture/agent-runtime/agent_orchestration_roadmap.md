# Agent 编排与工具路线图

更新时间：2026-06-02

本文是路线图级摘要，不替代 [Conversation Orchestrator 主对话场景技术设计](conversation_orchestrator_design.md)、[Web Search 同步工具调用技术设计](search_async_tasks_design.md) 或 [主动消息与提醒设计](../products/zhaoxi/proactive_messaging_design.md)。

## 当前决策

- Phase 1 主对话保持同步优先：普通聊天、提醒识别和 `web_search` tool use 都在当前 turn 内完成或给出失败说明。
- Web Search 只走同步 LLM tool use；超时、provider 失败、复杂资料整理、后台报告和“整理好后再发我”当前回合返回失败/不支持说明。
- Phase 1 不支持用户请求后的后台整理、异步任务补发或长耗时报告生成；不新增 `tasks` / `task_runs` 作为通用用户任务底座。
- Scheduler / due dispatcher 保留，但只负责用户提醒、陪伴跟进、内容邀请等主动消息调度，不属于用户请求异步任务机制。
- After-turn daily notes、hidden commitment extraction 等内部后置动作可以继续不阻塞用户回复；它们不是用户可见任务，也不补发结果。

## Phase 1 编排边界

```text
Inbound WeChat Message
-> OpenClaw / Bridge
-> /openclaw/turn
-> identity resolve
-> account policy / dedupe / entitlement precheck
-> intent gate
   -> explicit reminder: create/confirm reminder
   -> background request: unsupported/failure explanation
   -> normal chat: context assembly + enabled tool schema
-> LLM tool use / reply
-> persist messages / trace / cost events
-> Bridge synthetic reply
-> after-turn internal actions
```

## 工具路线

Phase 1 优先完成：

- `web_search` tool schema 和 provider adapter。
- DuckDuckGo、Bing RSS、Aliyun IQS、Baidu AI Search 的回退策略。
- `tool_invocations`、`search_provider_runs`、`cost_events`。
- 成功触发商业搜索 provider 后固定扣减 5 贝壳。
- 搜索引用格式、失败说明和复杂后台请求不支持话术。

暂不进入 Phase 1：

- 通用后台任务 runner。
- 复杂任务规划器。
- 后台研究、报告生成、多步骤资料整理。
- 搜索结果或长任务完成后的异步补发。
- 后端 ASR fallback；当前语音依赖 `openclaw-weixin` 上游转写文本。

## 主动调度路线

Scheduler 的范围是主动消息，而不是用户请求任务：

- 用户提醒：按用户设定时间发送。
- 陪伴跟进：低频、受主动触达总开关、quiet hours、日上限和 6 小时避让约束。
- 内容邀请：先询问，用户确认后在入站回合返回标题列表。

部署上可以使用单进程 FastAPI in-process scheduler 或独立 scheduler worker。正式内测前推荐独立 scheduler worker，并补齐 lease / leader election、DB 原子 claim、失败重试和端到端验证。

## 后续单独立项条件

只有当内测数据证明同步搜索不足以覆盖核心需求，且用户确实高频要求后台研究、资料整理或报告生成时，再单独立项用户可见后台任务。届时需要重新设计：

- 用户可见任务状态。
- 幂等和失败说明。
- 结果投递与 quiet hours 的关系。
- 成本归因和贝壳扣减。
- Admin 排障和隐私边界。
