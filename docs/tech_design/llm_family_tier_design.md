# LLM family×tier 选型技术设计

更新时间：2026-07-12

> 事实源：`app/llm_providers.py`、`app/llm.py`、`app/db/llm_config.py`、`app/routers/admin_llm.py`、`.env.example`。
> 本文描述当前生效的 LLM 选型模型。**旧的 `LLM_MODEL` / `LLM_DEFAULT_PROVIDER_ID` / `settings.llm_model` 已删除，不再使用**——凡文档仍引用这些名字的都应改为本文口径。

## 1. 为什么是两层

早期只有一个「默认 model / 默认 provider id」，换厂商要同时改多处，且分不清「主对话要强模型、后台任务用快模型」。重构后把选型拆成两个正交维度：

- **family（厂商家族）**：`deepseek` | `openai` | `anthropic`。一次切换 family，主对话和所有后台任务一起换厂商。
- **tier（档位）**：`pro`（综合强，主对话）| `flash`（快/省，后台任务）。

调用点不写死具体模型，只声明「我是什么任务」，由 `tier_for_task(task)` 映射到档位，再由 `resolve_provider_for_tier(tier, family, override)` 解析出具体 provider。这样换模型 = 改矩阵，换厂商 = 改一个 family，调整某类任务的档位 = 改一条 task→tier 路由。

## 2. task → tier 路由

`app/llm_providers.py::tier_for_task(task_kind)`。默认表 `_TASK_TIER_DEFAULTS`：主对话走 pro，其余后台任务走 flash。

| task kind 常量 | 默认 tier |
|---|---|
| `main_reply` | **pro** |
| `onboarding_extraction` | flash |
| `moderation` | flash |
| `rolling_summary` | flash |
| `dreaming` | flash |
| `user_meta` | flash |
| `relationship_state` | flash |
| `proactive_recall` | flash |
| `web_completion` | flash |

覆盖方式：环境变量 `LLM_TASK_TIERS`（JSON），逐 task 覆盖，如 `{"moderation":"pro"}`。未知 task kind 会告警并回落 flash。调用点应使用常量（`TASK_MAIN_REPLY` 等），不要拼裸字符串。

## 3. provider 矩阵（family × tier cell）

`list_llm_providers()` 返回全部可选 provider，每个 provider 是矩阵里的一个 (family, tier) cell。

### 3.1 内置矩阵

`_builtin_provider_items()` 内置四个 cell，即使不配 `LLM_PROVIDERS_JSON` 也可在后台看到：

| id | family | tier | protocol | model 来源 |
|---|---|---|---|---|
| `deepseek` | deepseek | flash | openai_chat | `deepseek-v4-flash`（`_DEFAULT_DEEPSEEK_MODEL`） |
| `deepseek-v4-pro` | deepseek | pro | openai_chat | `deepseek-v4-pro` |
| `chatgpt` | openai | pro | openai_responses | `LLM_OPENAI_MODEL` |
| `claude` | anthropic | pro | anthropic_messages | `LLM_ANTHROPIC_MODEL` |

deepseek 内置 pro+flash 两档；openai/anthropic 只内置 pro 一档，flash 需用 `LLM_PROVIDERS_JSON` 补（每条必须显式带 `family`+`tier`）。

### 3.2 JSON 扩展 / 覆盖

`LLM_PROVIDERS_JSON`（list 或 `{"providers":[...]}`）按 `id` 覆盖内置项、或新增 cell。约束：

- 每条必须显式声明 `family` + `tier`（省略默认 `family=""`、`tier="pro"`）；**覆盖内置 id 时也要写全**，不做隐式继承，避免「改了 model 忘了 tier」的错配。
- 禁止内联 `api_key`，只能用 `api_key_env` 引用（`LLM_API_KEY` / `LLM_OPENAI_API_KEY` / `LLM_ANTHROPIC_API_KEY` 等，见 `_KEY_FIELD_ALIASES`）。
- `protocol` 仅支持 `openai_chat` / `openai_responses` / `anthropic_messages`。非法 JSON 时运行时回落到内置矩阵（`_safe_list_providers` 告警不崩）。

## 4. 解析顺序（resolve_provider_for_tier）

给定一个 tier，解析实际 provider 的优先级：

1. **该 tier 的运行时 override provider id**（后台设置，可跨 family）——命中直接返回。
2. **active family × tier** 精确命中。
3. 兜底：该 family 的 pro → 任意同 tier → 列表首个。

active family 来源优先级：显式传入 > 运行时绑定 `active_family` > `LLM_ACTIVE_FAMILY` env > `_DEFAULT_FAMILY`（deepseek）。

`app/llm.py` 是唯一读运行时绑定的地方：它从 `get_llm_runtime_bindings()` 取 `active_family` 和该 tier 的 `override_provider_id`，传给 `resolve_provider_for_tier`；`llm_providers.py` 本身无 DB 依赖。

## 5. 运行时切换（无需重启）

存储：`app/db/llm_config.py`，复用 KV 表 `llm_runtime_config`（迁移 `_migration_0002_llm_runtime_config`），三个 key：`active_family` / `pro_provider_id` / `flash_provider_id`。

| 操作 | 函数 | 语义 |
|---|---|---|
| 读绑定 | `get_llm_runtime_bindings()` | 返回三者（未设为 None，回落 settings 默认） |
| 切 active family | `set_active_family(family)` | 写 active_family，**并清掉两个 tier override**，让两档都跟随新 family |
| 覆盖某档 provider | `set_tier_provider_override(tier, provider_id)` | 把某档钉到指定 provider（可跨 family） |
| 清某档 override | `clear_tier_provider_override(tier)` | 该档回落 active family 的对应档 |
| 清全部 | `clear_llm_runtime_bindings()` | 回落 settings 默认 |

后台入口 `app/routers/admin_llm.py`（admin/staff）：

- `GET /admin/llm/providers` — 列矩阵 + 当前解析结果
- `PATCH /admin/llm/active-family` — 切 family
- `PATCH /admin/llm/tier-override/{tier}` — 钉某档 provider
- `DELETE /admin/llm/tier-override/{tier}` — 清某档 override
- `POST /admin/llm/providers/{provider_id}/probe` — 探活某 provider

## 6. 相关环境变量（`.env.example` 为准）

| 变量 | 作用 |
|---|---|
| `LLM_ACTIVE_FAMILY` | 默认生效家族（deepseek/openai/anthropic），后台可运行时覆盖 |
| `LLM_TASK_TIERS` | 可选，逐 task 覆盖 tier 的 JSON |
| `LLM_BASE_URL` / `LLM_API_KEY` | deepseek 家族 base/key（family=deepseek 的 pro/flash 共用） |
| `LLM_OPENAI_BASE_URL` / `LLM_OPENAI_MODEL` / `LLM_OPENAI_API_KEY` | openai 家族 |
| `LLM_ANTHROPIC_BASE_URL` / `LLM_ANTHROPIC_MODEL` / `LLM_ANTHROPIC_API_KEY` | anthropic 家族 |
| `LLM_PROVIDERS_JSON` | 可选，声明/覆盖 family×tier 矩阵条目（每条带 family+tier） |

> 已废弃、代码中不再读取：`LLM_MODEL`、`LLM_DEFAULT_PROVIDER_ID`、`settings.llm_model`。

## 7. 计费相关（现状与缺口）

计费按 token 记（`record_chat_usage_charge`），审计列 `llm_model` 记录本次实际解析出的模型串（`get_active_llm_model(tier_for_task(...))`）。**按模型定价倍率（`model_price_multiplier_micros`）当前恒传 1.0x、`model_price_rules` 表未落地**——family×tier 目前只决定「用哪个模型」，不决定「不同模型不同计费倍率」。定价收口跟踪见 [`STATUS.md`](../STATUS.md) §3 与 [entitlement_growth_design](entitlement_growth_design.md)。
