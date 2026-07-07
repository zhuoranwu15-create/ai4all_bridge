# turn_service 主链路与编排模块：优化空间梳理

> 临时文档。背景：主干上有并行改动待合并，本次先只做代码走读 + 问题梳理，**不改代码**。
> 范围：`app/turn_service.py`（`handle_openclaw_turn` 四阶段主链路）+ 直接参与编排的
> `app/session_lifecycle.py`、`app/user_profiles.py`（`read_agent_context`）、
> `app/agent_self_state.py`、`app/context_window.py`、`app/memory_writer.py`、
> `app/tool_evidence_replay.py`、`app/mission_state.py`。按优先级排列，均已逐行核实源码。

日期：2026-07-05。

---

## 0. 整体架构评价（先说好的部分，避免只挑刺）

`handle_openclaw_turn` 已经拆成 `_prepare_turn`（解析+守卫）→ `_persist_and_screen_inbound`
（入站持久化+筛查）→ `_resolve_turn_reply`（解析回复）→ `_finalize_turn`（终结）四阶段，
每阶段产物用 `_TurnSetup` / `_InboundResult` / `_ReplyResult` 三个 dataclass 显式传递，
职责边界清楚、命中守卫可在任意阶段提前返回。这个结构本身**不建议改动**——下面的问题都是
在这个骨架内部找到的局部冗余，不是架构性缺陷。

---

## 1. 【P0，高优先级】`read_agent_context()` 每轮被重复调用 2-3 次，其中 1-2 次结果完全未使用

### 现状

`agent_context`（合并 AGENTS/TOOLS/SOUL/IDENTITY/USER/MEMORY/MISSION 七个文件的读取结果）
在一次非 onboarding 完成态的普通轮次里，实际被 `read_agent_context()` 读取：

1. `turn_service.py:1345-1348`（`_resolve_turn_reply` 内）：
   ```python
   agent_context = read_agent_context(
       account_id,
       display_name=account.get("display_name"),
   )
   ```
2. 若命中 onboarding 预抽取且有写入（`turn_service.py:1360-1370`），再读一次：
   ```python
   if onboarding_pre_written:
       agent_context = read_agent_context(
           account_id,
           display_name=account.get("display_name"),
       )
   ```
3. `turn_service.py:487-490`（`build_turn_llm_input` 内部，独立读取，与上面两次互不知情）：
   ```python
   agent_context = read_agent_context(
       account_id,
       display_name=account.get("display_name"),
   )
   ```

**用 grep 核实了整个文件里 `agent_context` 的每一处引用**（`turn_service.py` 全文只有这
7 处：77/79 是 import，487/520/569/593/645 都在 `build_turn_llm_input` 函数体内，1345/1367
在 `_resolve_turn_reply` 内）。也就是说：**第 1、2 步里读出来的 `agent_context` 局部变量，
在 `_resolve_turn_reply` 剩余的代码里没有被读取过一次**——它没有被传进 `build_turn_llm_input`
（该函数没有接收 `agent_context` 的参数），第 3 步是完全独立的一次重新读取。第 1、2 步纯粹是
"读了但扔掉"。

### 代价：不是空转，是真实 DB 往返

`read_agent_context()`（`app/user_profiles.py:556-587`）内部：

- 先调 `ensure_system_context_files()`（磁盘存在性检查，AGENTS.md/TOOLS.md，2 次文件系统调用）；
- 再调 `ensure_agent_context_files()`（`user_profiles.py:529-553`）：对 SOUL/IDENTITY/USER/
  MEMORY/MISSION 5 个文件逐个调 `profile_storage.read_file()` 检查是否已存在——**5 次 DB 读**；
- 再遍历 7 个 `CONTEXT_FILE_ORDER` 文件取正文：AGENTS/TOOLS 走磁盘（2 次文件读），SOUL/
  IDENTITY/USER/MEMORY/MISSION 走 `profile_storage.read_file()`——**又 5 次 DB 读**。

即每调用一次 `read_agent_context()` ≈ **10 次 `profile_storage.read_file()` DB 往返**
（`app/profile_storage.py:19-24`，每次都是独立的 `with _tx(conn) as tx:` 事务，没有传入
共享 `conn`，也没有做批量查询）。而 `profile_storage` 表 `account_profile_files` 是双后端的
——按 CLAUDE.md，**生产环境（aliyun1+aliyun2）自 2026-06-21 起已全量切到 PG**，aliyun2 是直连
中心 PG，意味着这 10 次读在生产环境是 10 次真实的跨机网络往返，不是本地 SQLite 文件系统调用。

普通轮次（无 onboarding 写入）：3 次 `read_agent_context()` 调用中有 2 次纯浪费，即
**每轮白白多付出约 20 次 DB 往返**；命中 onboarding 预抽取写入的轮次会更高。

另外，`_prepare_turn`（`turn_service.py:891`）里已经单独调用过一次 `ensure_agent_context_files
(account_id, ...)`，这次调用本身也会被后续每一次 `read_agent_context()` 内部再检查一遍——
四层里有相当一部分是同一件"文件是否存在"的检查被反复问了四次。

### 建议

- `_resolve_turn_reply` 里第 1345/1367 行读出来的 `agent_context`，如果确实只是为了在
  onboarding 分支判断"有没有新写入"，可以直接检查 `onboarding_pre_written` 返回值本身
  （它已经是 `apply_extracted_onboarding_info` 的返回结果），不需要为此重新读一遍全量
  context。
- 如果这次读取的结果本意是要传给 `build_turn_llm_input` 复用（避免它自己再读一次），
  给 `build_turn_llm_input` 加一个可选的 `agent_context: Optional[AgentContext] = None`
  参数，传了就用、不传按现状自己读——这是最小改动、向后兼容的修法。
- 不建议现在就动 `_prepare_turn:891` 那次 `ensure_agent_context_files` 调用，它和
  `read_agent_context` 内部的检查目的略有不同（早期确保文件存在，供 onboarding/画像等
  其它逻辑用），先解决上面两处更明确的重复读，再看要不要动这层。

这条改动范围小（`turn_service.py` 一个函数 + `build_turn_llm_input` 签名加一个可选参数），
不改变任何对外行为，风险低，收益是每轮减少约 2/3 的 `read_agent_context` 开销。

---

## 2. 【P1，需要确认后再排期】跨业务日的 Dreaming 摘要在请求同步路径里跑

### 现状

`_prepare_turn`（`turn_service.py:871-878`）同步调用
`get_or_create_account_active_session_with_dreaming(...)`。当账号跨越业务日边界
（`session_lifecycle.py:34-44` 的 `_close_reason_for` 判定 `daily_dreaming`）时，会走到
`_rotate_session_with_dreaming()` → `_fallback_close_summary()` → `run_dreaming(...,
allow_fallback=True)`（`session_lifecycle.py:54-66`），而 `run_dreaming()`
（`app/dreaming.py:726-`）内部 `_call_dreaming_llm(...)`（第 834 行）是一次**真实的、同步
的 LLM 摘要调用**，成功与否都要等它返回（失败时因为 `allow_fallback=True` 会走确定性兜底，
但"调用+等待失败"这段时间本身也计入延迟）。

也就是说：**每个活跃账号当天第一条消息**，如果没有被 proactive/dreaming scheduler
提前扫描并轮转掉这个 session，就会在这一条消息的响应路径里，先跑完一次 LLM 摘要调用，
用户才能收到回复。这不是每轮都发生，但对命中的用户是一次实打实的额外 LLM 往返延迟，
叠加在原本的对话生成 LLM 调用之前。

CLAUDE.md 提到"主动调度器推荐作为独立进程运行……Dreaming scheduler 可通过 FastAPI
in-process 开关或 admin run-once 调试"——如果调度器确实稳定地在业务日边界之前把所有
活跃 session 都提前轮转掉了，这个同步路径在生产里基本不会被触发，风险主要在：调度器
未运行/延迟/漏扫的账号，或者用户消息恰好卡在调度器扫描间隙。

### 建议（不确定，需要先确认再决定要不要动）

1. 先确认生产环境 dreaming scheduler 的实际运行状态和扫描频率，评估"当天第一条消息
   撞上同步 dreaming"的实际发生率有多高——如果调度器覆盖得足够好，这条优先级可以降低，
   不必特意为一个低频路径增加复杂度。
2. 如果确认有实际影响，可选方向（都需要单独评估，不在本文档展开设计）：
   - 给 `_call_dreaming_llm` 加显式超时，超时即走 `allow_fallback` 的确定性摘要，避免
     LLM 侧偶发慢响应放大到用户可感知的延迟；
   - 或者把"当天第一条消息"的处理改成先用旧 carryover/无摘要快速给一个回复，摘要生成
     挪到 after-turn 异步链路（类似现有的 `write_memory`/`maybe_update_relationship_state_
     after_turn` 模式）——但这个改法涉及 session 状态机语义变化，需要单独评估对
     `carryover_summary`/`business_day` 一致性的影响，不是一个"改一行"级别的修复。

---

## 3. 【P2，低优先级】`_build_tooling_envelope` 无论是否 debug 都构建完整 schema 列表

### 现状

`_build_tooling_envelope`（`turn_service.py:335-405`）每轮都会遍历 `iter_specs()`
（全部已注册工具），为每个工具构建 `available`/`disabled` 两个 debug 用列表，其中
`available` 列表里每一项都带着该工具的完整 JSON schema（`turn_service.py:373-380`）。

这个结果最终进入 `debug_metadata["tooling"]`，而 `debug_metadata` 只有在
`debug_trace_enabled`（`_is_debug_trace_account`，一个很小的账号白名单）为真时才会
通过 `insert_debug_trace` 落库（`turn_service.py:1607-1623`）。也就是说，**对绝大多数
不在白名单里的账号，这份带完整 schema 的列表每轮都在构建，但从不被使用**。

### 影响与建议

`iter_specs()` 本身是模块级常量（`app/tools/registry.py:159` 的 `_SPECS` 在 import
时构建一次），这里的开销是纯 Python 层面的字典拷贝/列表构建，**没有 DB/网络 I/O**，
量级上远小于第 1 条的问题。可以考虑：只有 `debug_trace_enabled` 为真时才把完整
`schema` 字段塞进 `available` 列表，其余情况只保留 `name`/`group`/`reason`。收益有限，
优先级排在最后，可以和第 1 条一起顺手做，也可以先不做。

---

## 4. 观察到但不建议现在动的部分

- **每轮多个独立小事务**：`_persist_and_screen_inbound` 和 `_finalize_turn` 各自开了
  独立的 `with db_connect() as conn:` 块（入站插入+计数一个事务，出站插入+turn_count
  另一个事务），中间还夹着 `process_referral_message_for_account`、
  `screen_inbound_message_sync`、`enqueue_message_for_moderation` 等各自独立的调用。
  这是四阶段流水线的自然结果（各阶段职责边界清楚，命中守卫可提前返回），合并事务会
  牺牲这个清晰度换取有限的网络往返节省，**不建议现在动**。
- **`upsert_channel_binding` 每轮无条件 UPSERT `last_seen_at`**（`app/db/accounts.py:92-`）：
  即使这一轮 identity 完全没变化，也会写一次。这是"最后活跃时间"这个字段本身的语义
  要求（需要每轮更新），不是 bug，不建议为了省一次写而牺牲这个可观测性。
  `_prepare_turn` 里还带着一次别名去重查询（`_channel_account_id_aliases`），同样是
  数据一致性需要的必要开销。
- **TDAI 200ms 同步超时召回**（`_resolve_turn_reply:1300-1341`）：注释里已经写明是
  "同步调用，严格 200ms 超时"的有意设计，不是遗漏的性能问题，不在本次梳理范围内。
- **`resolve_account_mission` 未被重复调用**：`build_turn_llm_input` 里查一次
  （`turn_service.py:546`），结果通过 `resolved_mission` 参数直接传给
  `build_agent_self_state_block`（`agent_self_state.py:111-112` 的 `_UNRESOLVED` 哨兵
  机制），避免了同一账号同一轮查两次 `account_mission`——这是一个已经做对的例子，
  第 1 条的修复思路（"读一次、传下去，不要各自重读"）在这里已经有先例。

---

## 5. 建议（按性价比排序）

1. **修 `read_agent_context()` 的重复调用**（第 1 条）：确定性高、改动集中在
   `turn_service.py` 一个函数 + `build_turn_llm_input` 加一个可选参数，不改变任何
   对外行为和 prompt 内容，收益是每轮减少约 2/3 的 profile 相关 DB 往返（生产环境是
   跨机 PG，收益更明显）。
2. **确认 dreaming scheduler 在生产的实际覆盖率**，再决定是否需要处理"当天第一条
   消息撞上同步 dreaming"这条路径（第 2 条）。这条如果影响面小可以先搁置，如果影响
   面大需要单独立项设计（涉及 session 状态机语义，不是小改动）。
3. `_build_tooling_envelope` 的 schema 按需构建（第 3 条），顺手做，不单独立项。

---

## 6. 开放问题

1. 第 1 条里，`_resolve_turn_reply` 重新读 `agent_context` 的原始意图是什么——是历史
   遗留（重构后忘记删）还是有什么本文档没看出来的隐含依赖？需要确认后再定具体修法
   （直接删 vs 传参复用）。
2. 生产环境 dreaming scheduler 的部署形态（哪些节点在跑、扫描间隔多长）——这决定第 2
   条的优先级要不要提前。
3. 是否需要在 `/debug/accounts/{id}/prompt-preview` 或类似入口加一个"本轮 DB 往返次数"
   的计数指标，方便以后验证类似的重复调用问题，而不是每次都靠人工读代码数。
