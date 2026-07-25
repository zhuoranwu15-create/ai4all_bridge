# Agent Context Files 落地技术方案

> 创建于：2026-05-18
> 状态：Step 1/2 已落地，Step 3 待实现
> 关联机制文档：`docs/architecture/designs/agent_context_files.md`

## 目标

把 `AGENTS.md / SOUL.md / IDENTITY.md / USER.md / TOOLS.md / MEMORY.md` 融入 AI4ALL 的实际运行链路，并把 `HEARTBEAT.md` 从账号级 prompt context 中移除，改成后续全局系统策略文件。

目标状态：

```text
data/user_profiles/<account_id>/
├── AGENTS.md
├── SOUL.md
├── IDENTITY.md
├── USER.md
├── TOOLS.md
├── MEMORY.md
├── user_profile.md
└── memory/
    └── YYYY-MM-DD.md
```

普通用户聊天 prompt 的 `Project Context` 只注入：

```text
AGENTS.md
SOUL.md
IDENTITY.md
USER.md
TOOLS.md
MEMORY.md
```

`HEARTBEAT.md` 不进入普通用户聊天 prompt。用户定时任务、主动触达设置、提醒任务后续单独建模。

## 当前代码状态

当前已经实现目标状态中的账号级 context 注入：

- `app/user_profiles.py`
  - `CONTEXT_FILE_ORDER` 只包含 `AGENTS.md / SOUL.md / IDENTITY.md / USER.md / TOOLS.md / MEMORY.md`
  - `_default_context_templates` 不再生成账号级 `HEARTBEAT.md`
  - `read_agent_context` 不读取也不返回 `HEARTBEAT`
- `app/prompt_builder.py`
  - `_CONTEXT_BLOCK_ORDER` 不包含 `HEARTBEAT`
  - `Project Context` 不注入 `### HEARTBEAT.md`
  - 即使调用方误传 `agent_context={"HEARTBEAT": ...}` 也会忽略
- `app/main.py`
  - 主链路、prompt-preview、debug trace metadata 不再包含账号级 `HEARTBEAT.md`
- tests
  - 已覆盖不创建、不读取、不注入账号级 `HEARTBEAT.md`

历史账号目录里已有的 `HEARTBEAT.md` 不会被自动删除，但当前 runtime 不再读取它。

## 当前 checkpoint

截至 2026-05-18：

- Context files 的基础运行融合已经完成。
- 账号级 `HEARTBEAT.md` 已从创建、读取、prompt 注入、prompt-preview、debug trace metadata 中移除。
- 全局 `HEARTBEAT.md` 占位文件已新增到 `app/prompts/heartbeat.md`，当前不接入普通聊天 runtime。
- 相关测试已覆盖并通过。
- 下一步不是继续 Dreaming、Context 智能压缩或工具体系，而是先做“显式修正与更新机制”。

下一步重点：

- `IDENTITY.md` / `USER.md`：用户显式修正后，更新文件并向用户明确当前记录内容。
- `SOUL.md`：只做内部精简更新，不向用户暴露完整内容；宁缺毋滥。
- 先实现清晰的纯函数/模块边界和测试，再决定是否接入真实聊天触发。

## 实施步骤

### Step 1：移除账号级 HEARTBEAT.md 注入（已完成）

修改 `app/user_profiles.py`：

- 从 `CONTEXT_FILE_ORDER` 移除 `HEARTBEAT.md`
- 从 `_default_context_templates` 移除 `HEARTBEAT.md`
- `context_file_path(account_id, "HEARTBEAT.md")` 应不再被视为账号 context file
- `read_agent_context` metadata 不再返回 `HEARTBEAT.md`

保留策略：

- 不删除磁盘上已存在的 `data/user_profiles/<account_id>/HEARTBEAT.md`
- 只是不再创建、不再读取、不再注入
- 这样避免破坏已有调试文件，也方便需要时人工迁移

修改 `app/prompt_builder.py`：

- 从 `_CONTEXT_BLOCK_ORDER` 移除 `HEARTBEAT`
- 从 `_CONTEXT_BLOCK_LIMITS` 移除 `HEARTBEAT`
- 即使调用方误传 `agent_context={"HEARTBEAT": ...}`，也不注入

修改 `app/main.py`：

- 主链路无需特殊改动，只要 `read_agent_context` 不返回即可
- prompt-preview 和 debug trace metadata 自然不再包含 `HEARTBEAT.md`

### Step 2：补全全局 HEARTBEAT 策略占位（已完成）

先不实现 heartbeat runtime，只建立清晰位置。

已新增：

```text
app/prompts/heartbeat.md
```

用途：

- 记录实例级 heartbeat 的全局策略
- 后续系统健康检查、调度任务、告警策略可以引用
- 不进入普通用户聊天 prompt

第一版内容可以很短：

```markdown
# HEARTBEAT

这是 AI4ALL 实例级 heartbeat 策略文件。
用于系统健康检查、运行状态监督、调度策略约束。
不得注入普通用户聊天 prompt。
用户主动触达、提醒、定时任务应通过独立用户任务模型处理。
```

是否把它接入代码可以分两步：

- 第一阶段只创建文档/模板，不读它
- 第二阶段做实例级 heartbeat 时再接入

### Step 3：明确显式修正规则的代码边界（下一步）

本阶段不急着做完整自然语言解析，但要把边界留出来。

建议新增模块：

```text
app/context_updates.py
```

先定义纯函数和类型，不一定马上在主聊天链路启用：

```python
def apply_identity_update(account_id: str, patch: dict) -> dict:
    """Update IDENTITY.md and return user-visible confirmation summary."""

def apply_user_profile_update(account_id: str, patch: dict) -> dict:
    """Update USER.md and return user-visible confirmation summary."""

def propose_soul_update(account_id: str, instruction: str) -> dict:
    """Create a compact SOUL.md update candidate; do not expose full SOUL.md."""
```

行为规则：

- `SOUL.md`
  - 不向用户回显完整文件
  - 候选更新必须短、稳定、可泛化
  - 宁缺毋滥，不把每次风格反馈都写入长期人格
- `IDENTITY.md`
  - 用户显式修正后，可更新
  - 回复用户时明确当前身份内容
- `USER.md`
  - 用户显式修正后，可更新
  - 回复用户时明确当前记录内容

本阶段建议只先写技术边界和测试，是否接入真实聊天触发可以单独讨论。

### Step 4：更新测试（部分已完成，context_updates 待补）

已完成测试：

- `tests/test_agent_context.py`
  - 新账号只创建 `AGENTS.md / SOUL.md / IDENTITY.md / USER.md / TOOLS.md / MEMORY.md`
  - 不创建 `HEARTBEAT.md`
  - 已存在的 `HEARTBEAT.md` 不删除
- `tests/test_prompt_builder.py`
  - `Project Context` 不包含 `### HEARTBEAT.md`
  - 误传 `HEARTBEAT` 也不会注入
- `tests/test_debug_traces.py`
  - trace system_prompt 不包含 `### HEARTBEAT.md`
  - metadata files 不包含 `HEARTBEAT.md`

待新增测试：

- 新增 `tests/test_context_updates.py`
  - `IDENTITY.md` 更新后返回用户可见确认摘要
  - `USER.md` 更新后返回用户可见确认摘要
  - `SOUL.md` 更新候选不回显全文

### Step 5：迁移现有测试账号文件

不自动删除已有账号目录中的 `HEARTBEAT.md`。

建议手工或脚本迁移：

1. 查看 `data/user_profiles/*/HEARTBEAT.md`
2. 如果里面只有默认内容，可以保留不动，代码不再读取
3. 如果里面有产品级策略，移动到全局 heartbeat 策略文件
4. 如果里面有用户主动触达偏好，后续迁移到用户任务/提醒模型

当前阶段不做批量删除，避免误删人工备注。

## 验收标准

功能验收：

- 新账号 bootstrap 后，不再生成账号级 `HEARTBEAT.md`
- 普通聊天 prompt 不包含 `### HEARTBEAT.md`
- prompt-preview 不包含 `HEARTBEAT.md`
- debug trace metadata 不包含 `HEARTBEAT.md`
- 已存在的账号级 `HEARTBEAT.md` 不会被删除，但不会进入 prompt

测试验收：

- `.venv/bin/pytest` 全量通过
- context files 相关测试覆盖缺失文件、已有文件不覆盖、HEARTBEAT 不注入

产品验收：

- 模型不会在普通聊天中因为 `HEARTBEAT.md` 声称已安排主动触达
- 用户显式修正身份/用户资料时，后续机制能返回清晰确认
- `SOUL.md` 不被直接展示给用户

## 风险与注意事项

- 当前线上/本地已有账号目录可能存在 `HEARTBEAT.md`，移除读取后 prompt 会变短，这是预期变化。
- OpenClaw trace 仍可能包含它自己的 `HEARTBEAT.md`，AI4ALL 不再强行结构完全一致，而是保持语义对齐。
- 如果后续用户级主动触达需要读取 persona/user/memory，应该由独立 heartbeat turn 显式选择上下文，不复用普通聊天 prompt 注入策略。
- `context_file_path` 移除 `HEARTBEAT.md` 后，若旧代码或脚本仍调用它会抛错，需要测试覆盖。

## 推荐执行顺序

1. Step 1/2 已完成；Step 4 中 HEARTBEAT 相关测试已完成。
2. 下一步做 Step 3 的 `context_updates.py` 技术边界。
3. 同步新增 `tests/test_context_updates.py`。
4. 最后根据产品讨论决定是否接入真实用户显式修正流程。
