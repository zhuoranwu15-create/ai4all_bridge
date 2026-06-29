# 主动破冰小流量试用 Runbook v1.1

> 目标：以最低风险验证主动破冰全链路（话术选取 → 发送 → impression 记录），
> 保护其他用户不受影响。

---

## 功能现状

| 项目 | 状态 |
|---|---|
| DB schema | 已上线（`icebreaker_scripts` + `icebreaker_impressions` 表） |
| 100 条话术 seed | 已导入生产（99 条 enabled） |
| 破冰核心逻辑 | `app/proactive/icebreaker.py` 已合并 |
| Scheduler Step 5 | 代码已合并，**默认不自动触发**（需 scheduler 进程开启） |

---

## 前置检查（每次试用前执行）

```bash
# 1. 确认话术库已导入
sqlite3 data/ai4all.sqlite3 \
  "SELECT count(*) as total, sum(enabled) as enabled FROM icebreaker_scripts;"
# 期望：total=100, enabled=99

# 2. 确认测试账号存在
sqlite3 data/ai4all.sqlite3 \
  "SELECT account_id, channel FROM proactive_sessions WHERE account_id='aid_491823504' LIMIT 1;"

# 3. 确认测试账号今日尚未发送（quota_date = 当天日期）
sqlite3 data/ai4all.sqlite3 \
  "SELECT count(*) FROM outbound_messages
   WHERE account_id='aid_491823504'
     AND product_category='proactive_icebreaker'
     AND quota_date=date('now','+8 hours')
     AND status IN ('pending','sending','sent');"
# 期望：0

# 4. 确认 scheduler 进程未开启（Phase A 阶段只手动触发）
ps aux | grep run_proactive_scheduler | grep -v grep
# 期望：无输出

# 5. 确认路由存在（可选，脚本会自动跳过无路由账号）
sqlite3 data/ai4all.sqlite3 \
  "SELECT channel, channel_account_id FROM proactive_sessions
   WHERE account_id='aid_491823504' LIMIT 1;"
```

---

## Phase A：手动触发单账号（只测 aid_491823504）

**本阶段只对 `aid_491823504` 一个账号测试，不启动 scheduler，不影响其他用户。**

### 步骤

```bash
# Step 1：进入项目目录
cd /path/to/ai4all_bridge

# Step 2：启动 Python REPL（使用项目 venv）
.venv/bin/python3
# 或 arm64 节点
arch -arm64 .venv/bin/python3

# Step 3：在 REPL 中手动触发
from app.proactive.icebreaker import dispatch_icebreaker
result = dispatch_icebreaker("aid_491823504")
print(result)
```

**预期结果（正常路径）：**
```python
{
  'account_id': 'aid_491823504',
  'status': 'sent',          # 或 'cancelled'（policy 拦截，属正常）
  'script_id': '破冰XXX',
  'outbound_id': 12345,       # 实际 DB 行 id
  'outbound_status': 'sent',
  'impression_id': 67890
}
```

**预期结果（无路由，跳过）：**
```python
{'account_id': 'aid_491823504', 'status': 'no_op', 'reason': 'missing_channel_route'}
```

**预期结果（无候选话术）：**
```python
{'account_id': 'aid_491823504', 'status': 'no_op', 'reason': 'no_candidate_scripts'}
```

### 发送后验证

```bash
# 确认 impression 写入
sqlite3 data/ai4all.sqlite3 \
  "SELECT id, script_id, status, created_at
   FROM icebreaker_impressions
   WHERE account_id='aid_491823504'
   ORDER BY created_at DESC LIMIT 3;"

# 确认 outbound 写入（status=sent 或 cancelled）
sqlite3 data/ai4all.sqlite3 \
  "SELECT id, status, text, created_at
   FROM outbound_messages
   WHERE account_id='aid_491823504'
     AND product_category='proactive_icebreaker'
   ORDER BY created_at DESC LIMIT 3;"

# 确认今日配额已占用（同日再手动触发会走 no_op）
sqlite3 data/ai4all.sqlite3 \
  "SELECT count(*) FROM outbound_messages
   WHERE account_id='aid_491823504'
     AND product_category='proactive_icebreaker'
     AND quota_date=date('now','+8 hours')
     AND status IN ('pending','sending','sent');"
# 期望：1（如果 status=sent）或 0（如果 cancelled，允许同日重试）
```

---

## 日常观察 SQL

```sql
-- 今日破冰发送汇总（cancelled 分析必须用 LEFT JOIN，因 outbound_id 可为 NULL）
SELECT
  ii.account_id,
  ii.script_id,
  ii.status         AS impression_status,
  om.status         AS outbound_status,
  ii.created_at
FROM icebreaker_impressions ii
LEFT JOIN outbound_messages om ON om.id = ii.outbound_message_id
WHERE date(ii.created_at) = date('now', '+8 hours')
ORDER BY ii.created_at DESC;

-- 近 7 天破冰发送趋势
SELECT
  date(ii.created_at) AS day,
  ii.status,
  count(*) AS cnt
FROM icebreaker_impressions ii
WHERE ii.created_at >= datetime('now', '+8 hours', '-7 days')
GROUP BY day, ii.status
ORDER BY day DESC;

-- 话术曝光分布（top 10）
SELECT script_id, count(*) AS impressions
FROM icebreaker_impressions
WHERE status='sent'
GROUP BY script_id
ORDER BY impressions DESC
LIMIT 10;

-- 当日 cancelled 明细（LEFT JOIN 查 policy 原因）
SELECT
  ii.account_id,
  ii.script_id,
  om.policy_reason,
  ii.created_at
FROM icebreaker_impressions ii
LEFT JOIN outbound_messages om ON om.id = ii.outbound_message_id
WHERE date(ii.created_at) = date('now', '+8 hours')
  AND ii.status = 'cancelled'
ORDER BY ii.created_at DESC;
```

---

## 暂停条件

出现以下任意情况，立即暂停并检查：

- 非测试账号出现 `product_category='proactive_icebreaker'` 的 outbound（说明 scheduler 意外启动）
- `impression.status='error'` 数量异常增加
- 同一账号同日 impression 超过 1 条（配额逻辑失效）
- 收到用户投诉/负面反馈

---

## 回滚

Phase A（手动触发）无需回滚，停止手动调用即可。

Phase B（scheduler 开启后）回滚方法：

```bash
# 停止 scheduler 进程
kill <scheduler_pid>

# 或设置环境变量后重启服务
PROACTIVE_SCHEDULER_ENABLED=false
```

数据回滚（如需清除测试数据）：

```bash
sqlite3 data/ai4all.sqlite3 \
  "DELETE FROM icebreaker_impressions WHERE account_id='aid_491823504';
   DELETE FROM outbound_messages
     WHERE account_id='aid_491823504'
       AND product_category='proactive_icebreaker';"
```

---

## Phase B 启动条件

满足以下全部条件后，才可开启 scheduler 自动派发：

- [ ] Phase A 在 `aid_491823504` 上连续成功 ≥ 3 次（含不同天）
- [ ] 确认话术内容无异常（无 offensive/spam 反馈）
- [ ] 确认 impression 记录正常（无重复、状态正确）
- [ ] 确认 cancelled 分析 SQL 可正常返回 policy_reason
- [ ] 与产品/运营确认扩量范围和时间窗口

Phase B 启动方式：
```bash
# 在 .env 或启动脚本中设置（只允许单 worker 部署）
PROACTIVE_SCHEDULER_ENABLED=true

# 或直接启动 scheduler 进程
.venv/bin/python3 scripts/run_proactive_scheduler.py
```
