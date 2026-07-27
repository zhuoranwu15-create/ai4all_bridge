# Nooki P0+P1 PRD

更新时间：2026-07-27

## 1. 产品定义

Nooki 是一个帮助用户“开始行动”的 AI 陪伴产品，不是完整项目管理器。P1 的 Task
代表一次单行动闭环；完成卡片只能表达“完成了这次行动”，不能宣称用户已经完成原始
人生目标。

## 2. 核心边界

- AI 理解意图和生成三档行动方案。
- Tool 只表达业务意图。
- `GoalBreakdownService` 判断转换是否合法。
- PostgreSQL/SQLite 保存唯一真实状态。
- LLM 根据 Tool 的真实结果回复；UI 卡片只根据 `TaskProjection` 生成。
- 一个 `platform_user_id` 同时最多一个未终结 Task；P2 再支持多任务与切换。
- 一个 `platform_user_id` 在 P1 只有一个 active Conversation；聊天原文只保存在共享
  Runtime `messages`，小程序只缓存，不按日期或角色建立第二份权威记录。
- 稍后盒子以服务端 `nooki_later_items` 为准；本地只缓存 inbox 投影，修改使用版本检查。

## 3. 状态机

```text
create_task_with_options → Task=draft，恰好三个 Plan
select_plan             → Task=ready，Step=pending
start_step              → Task=active，Step=active
complete_step           → Task=done，Step=done
abandon                 → Task=abandoned，当前 pending/active Step=skipped
```

缩小行动：旧 Step 变 `skipped`，新 Step 继承旧 Step 的 `pending` 或 `active`；
replacement 的 `suggested_minutes` 必须严格小于旧 Step，旧 Step 没有预计时间时拒绝猜测。

`done` 与 `abandoned` 是终态。终态释放“单未终结 Task”约束，允许创建下一次行动。

## 4. 三档方案

- `tiny`：1～3 分钟
- `light`：3～10 分钟
- `normal`：8～30 分钟
- 必须满足 `tiny < light < normal`

三档必须是不同粒度的具体物理行动，不能是同一件事换写法。

## 5. 幂等与并发

- 所有写操作必须携带确定性 `operation_id`。
- `nooki_task_events.operation_id` 数据库唯一且非空。
- Tool 使用 `message:{message_id}:{action}:{resource_ids}`。
- 按钮使用 `button:{platform_user_id}:{client_request_id}`。
- 同一 operation 重试重载已提交资源，不重复写；同一 operation 用于不同动作时拒绝。
- `expected_version` 不一致返回 `task_version_conflict`。
- 任务和 Step 的联动更新与 event 必须在同一事务内完成。

## 6. 用户表达边界

- “太难了”优先陪伴或缩小，不等于放弃。
- 只有用户明确永久放弃时才能进入 `abandoned`。
- “今天先休息”不是永久放弃；P1 暂无 paused 状态，含糊时先确认。
- 用户提到第二个目标时不得创建第二个未终结 Task，但仍可正常聊天。
