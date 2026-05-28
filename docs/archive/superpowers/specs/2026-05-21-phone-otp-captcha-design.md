# 手机号注册 SMS OTP + 阿里云验证码 设计文档

**日期：** 2026-05-21  
**范围：** Web 注册流程（面向用户）  
**状态：** 后端已完成并安全加固（2026-05-21）；前端验证码配置仍需运行时配置收口

---

## 1. 背景与目标

当前 `POST /web/register` 不对手机号做任何验证，任何脚本可直接提交任意手机号完成注册，存在两类风险：

1. **短信轰炸**：后续接入短信功能后，恶意脚本可批量触发短信发送
2. **身份不可信**：无法确认注册者真实持有该手机号

本次改动目标：
- 注册前通过阿里云验证码 2.0 拦截自动化脚本
- 通过短信 OTP 确认用户真实持有手机号
- 对 `/web/register` 端点加上 OTP token 校验

---

## 2. 用户流程

```
[1] 用户输入手机号
[2] 点击「获取验证码」→ 触发阿里云验证码弹窗
[3] 用户完成验证码（滑块/拼图）
[4] 前端将 captchaVerifyParam 透传给后端 → POST /web/sms/send-otp
[5] 后端校验验证码 + 手机号频率 → 发送 6 位 OTP 短信
[6] 用户输入收到的验证码 → POST /web/sms/verify-otp
[7] 后端验证 OTP → 返回一次性 verified_token
[8] 前端调用 POST /web/register-and-binding-intent（携带 otp_token）
[9] 后端校验 token → 创建或复用 platform_user 和默认 AI4ALL Account → 生成微信登录二维码
```

---

## 3. API 设计

### 3.1 `POST /web/sms/send-otp`（新增）

**Request：**
```json
{
  "phone": "13800000000",
  "captcha_verify_param": "<阿里云验证码回调的原始字符串，禁止修改>"
}
```

**处理逻辑：**
1. 校验手机号格式（复用 `_normalize_phone`）
2. 调用阿里云验证码 `VerifyIntelligentCaptcha`，`verify_result` 为 false 则返回 400
3. 查询该手机号过去 1 小时内已发送条数，超过 `ALIYUN_SMS_MAX_PER_PHONE_PER_HOUR`（默认 5）则返回 429
4. 将该手机号现有未过期的 OTP 记录作废（`expires_at` 设为过去时间）
5. 生成 6 位随机数字 OTP，写入 `phone_verifications` 表（10 分钟有效期）
6. 调用阿里云短信 `SendSms` 发送 OTP
7. 若短信发送失败，立即作废该手机号当前 OTP 记录，并返回 500

**Response（成功）：**
```json
{ "status": "ok" }
```

**Response（失败示例）：**
```json
{ "detail": "验证码校验未通过" }          // 400
{ "detail": "发送频率过高，请稍后重试" }   // 429
{ "detail": "短信发送失败，请稍后重试" }   // 500
```

---

### 3.2 `POST /web/sms/verify-otp`（新增）

**Request：**
```json
{
  "phone": "13800000000",
  "code": "123456"
}
```

**处理逻辑：**
1. 查找该手机号最新一条未验证、未过期的记录
2. 若不存在 → 400（"验证码不存在或已过期"）
3. 错误尝试次数 `verify_attempts >= 5` → 400（"尝试次数过多，请重新获取验证码"），当前 OTP 不再允许继续验证
4. `code` 不匹配 → `verify_attempts + 1`，返回 400（"验证码错误"）
5. 匹配 → 设置 `verified_at`，生成 UUID 写入 `verified_token`，`token_expires_at = now + 10min`

**Response（成功）：**
```json
{
  "status": "ok",
  "verified_token": "550e8400-e29b-41d4-a716-446655440000"
}
```

---

### 3.3 `POST /web/register`（修改）

新增必填字段 `otp_token`：

```json
{
  "phone": "13800000000",
  "display_name": "Alice",
  "otp_token": "550e8400-e29b-41d4-a716-446655440000"
}
```

**新增校验逻辑（在原有逻辑之前）：**
1. 调用 `consume_valid_verification_token(otp_token, phone)`
2. 单条 SQL 原子更新：`verified_token` 匹配、`phone` 匹配、`token_consumed_at IS NULL`、`token_expires_at > now`
3. `UPDATE` 影响行数为 0 → 400（"注册凭证无效或已过期"）
4. 消耗成功后继续原有注册逻辑，创建或复用 platform user

---

### 3.4 `POST /web/register-and-binding-intent`（新增）

面向当前简化版 onboarding 页面。OTP 验证后，前端直接调用该接口完成注册、默认 AI4ALL Account 准备和二维码生成，不再展示“创建智能体”步骤。

**Request：**
```json
{
  "phone": "13800000000",
  "otp_token": "550e8400-e29b-41d4-a716-446655440000",
  "channel": "openclaw-weixin"
}
```

**处理逻辑：**
1. 原子消耗 `otp_token` 并创建或复用 `platform_user`
2. 调用 `get_or_create_default_ai4all_account_for_user`，复用已有 active owner binding；没有账号时创建默认 `AI4ALL 助手`
3. 创建 `binding_intent`
4. 调用 OpenClaw Gateway `web.login.start` 获取 `qr_data_url`
5. 返回 `platform_user`、`account`、`subscription` 和 `binding_intent`

---

## 4. 数据库

### 新增表 `phone_verifications`

```sql
CREATE TABLE IF NOT EXISTS phone_verifications (
    id TEXT PRIMARY KEY,
    phone TEXT NOT NULL,
    code TEXT NOT NULL,
    verify_attempts INTEGER NOT NULL DEFAULT 0,
    verified_at TEXT,
    verified_token TEXT,
    token_expires_at TEXT,
    token_consumed_at TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_phone_verifications_phone_created
    ON phone_verifications(phone, created_at);
```

### 字段说明

| 字段 | 说明 |
|------|------|
| `id` | `phv_<uuid hex>` |
| `phone` | 规范化后的手机号 |
| `code` | 6 位数字 OTP |
| `expires_at` | OTP 过期时间（created_at + 10min） |
| `verified_at` | OTP 验证通过时间，NULL 表示未验证 |
| `verified_token` | 注册用一次性 UUID，NULL 表示未生成 |
| `token_expires_at` | token 过期时间（verified_at + 10min） |
| `token_consumed_at` | 注册消耗时间，NULL 表示未使用 |
| `verify_attempts` | 错误尝试次数，≥5 时拒绝验证 |

---

## 5. 新增模块

### `app/sms.py`

封装阿里云短信发送，暴露 `send_otp(phone, code)` 函数：
- 单例 Client（`alibabacloud_dysmsapi20170525`）
- `template_param = json.dumps({"code": code})`
- 使用 `secrets.randbelow(1_000_000)` 生成密码学安全 OTP
- mock 模式：`APP_ENV in ("local", "test")` 且 `ALIYUN_ACCESS_KEY_ID` 为空时，仅打印 log，不实际发送
- 生产保护：非 local/test 环境下缺少凭据，直接抛 `RuntimeError`

### `app/captcha.py`

封装阿里云验证码校验，暴露 `verify_captcha(captcha_verify_param) -> bool` 函数：
- 单例 Client（`alibabacloud_captcha20230305`）
- 调用 `VerifyIntelligentCaptcha`，返回 `response.body.result.verify_result`
- mock 模式：`APP_ENV in ("local", "test")` 且 `ALIYUN_CAPTCHA_SCENE_ID` 为空时，直接返回 `True`
- 生产保护：非 local/test 环境下缺少 scene_id，直接抛 `RuntimeError`

---

## 6. 配置新增（`app/config.py` + `.env.example`）

```
# Aliyun SMS
ALIYUN_ACCESS_KEY_ID=
ALIYUN_ACCESS_KEY_SECRET=
ALIYUN_SMS_SIGN_NAME=
ALIYUN_SMS_TEMPLATE_CODE=
ALIYUN_SMS_MAX_PER_PHONE_PER_HOUR=5

# Aliyun Captcha 2.0
ALIYUN_CAPTCHA_SCENE_ID=
ALIYUN_CAPTCHA_PREFIX=

# OTP TTL（分钟）
OTP_EXPIRES_MINUTES=10
OTP_TOKEN_EXPIRES_MINUTES=10
```

---

## 7. 前端 `onboarding.html` 改动

### 验证码形态：一点即过

控制台创建场景时选择**「一点即过」**。用户点击「获取验证码」按钮后，阿里云在后台完成环境评估，低风险直接通过，高风险才降级为二次挑战。前端代码和后端接口与其他形态完全相同，无需特殊处理。

### 引入阿里云验证码 JS（在 `<head>` 中）

目标接入方式应把公开的 `prefix` 放在 SDK 加载前的 `window.AliyunCaptchaConfig` 中，`SceneId` 放在 `initAliyunCaptcha` 调用中。当前仓库里的静态 `onboarding.html` 已能串起 OTP 子流程，但 `SceneId` / `prefix` 仍是手工写入页面，后续需要改为后端公开配置接口或构建期注入。

```html
<script>
  window.AliyunCaptchaConfig = {
    region: "cn",
    prefix: "XXXXXX",   // 控制台概览页「实例基本信息」中获取
  };
</script>
<!-- 必须动态加载，禁止本地部署 -->
<script src="https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"></script>
```

### `initAliyunCaptcha` 初始化（页面加载后调用一次）

```javascript
var captchaInstance;

window.initAliyunCaptcha({
  SceneId: "XXXXXXXX",       // 控制台场景列表中获取
  mode: "popup",             // 弹出式，一点即过使用 popup
  element: "#captcha-element",
  button: "#btn-send-otp",   // 「获取验证码」按钮，点击后触发验证
  captchaVerifyCallback: captchaVerifyCallback,
  onBizResultCallback: onBizResultCallback,
  getInstance: function(instance) { captchaInstance = instance; },
  slideStyle: { width: 300, height: 40 },  // 一点即过按钮框体尺寸
  language: "cn",
});
```

> **注意**：`initAliyunCaptcha` 只调用一次，整个页面共享同一实例。
> 验证码 JS 加载完成到触发验证请求之间需间隔 **大于 2 秒**。

### Step 1 子流程

Step 1 card 内顺序展开两段 UI；OTP 验证通过后直接注册并生成二维码。

**段 A — 手机号 + 获取验证码按钮**
- 输入框：手机号
- 按钮 `#btn-send-otp`「获取验证码」：点击 → 触发阿里云验证码 popup
- 验证码容器 `#captcha-element`（隐藏，由 SDK 渲染）

**段 B — OTP 输入（`onBizResultCallback` 成功后展开）**
- 输入框：6 位验证码
- 按钮「验证并注册」：先调 `POST /web/sms/verify-otp`，成功后调 `POST /web/register-and-binding-intent`
- 倒计时文案（60s 后可重发，重发再次触发 `captchaInstance.show()`）

### 阿里云验证码回调

```javascript
// SDK 完成验证后调用，captchaVerifyParam 直接透传给后端，禁止修改
async function captchaVerifyCallback(captchaVerifyParam) {
  try {
    const result = await webFetch('/web/sms/send-otp', {
      method: 'POST',
      body: JSON.stringify({
        phone: document.getElementById('phone').value,
        captcha_verify_param: captchaVerifyParam,
      }),
    });
    return { captchaResult: true, bizResult: true };
  } catch (e) {
    return { captchaResult: true, bizResult: false };  // 验证码通过，但业务失败
  }
}

// 业务结果回调：captchaResult && bizResult 均 true 时触发
function onBizResultCallback(bizResult) {
  if (bizResult) {
    showOtpInput();  // 展开段 B
  } else {
    setStatus('st-send-otp', '发送失败，请稍后重试', true);
  }
}
```

---

## 8. 安全要点汇总

| 威胁 | 对策 |
|------|------|
| 自动化脚本批量发短信 | 阿里云验证码拦截在前 |
| 单号短信轰炸 | 每小时限额（默认 5 条） |
| OTP 暴力枚举 | 错误 5 次后锁定当前 OTP 校验，需重新发送 |
| OTP 可预测 | `secrets.randbelow(1_000_000)` 密码学安全随机 |
| verified_token 重放 | `consume_valid_verification_token` 单条件原子 UPDATE，消耗即作废 |
| verified_token 盗用 | 10 分钟 TTL + phone 绑定校验 |
| 手机号格式注入 | `^1[3-9]\d{9}$` 正则验证，拒绝非大陆手机号 |
| SMS 发送失败残留记录 | 发送失败立即调 `invalidate_verifications_for_phone` 清理，返回 500 |
| 生产环境缺失凭据 | 非 local/test 环境下缺少 Aliyun 凭据直接抛 `RuntimeError` |
| 本地开发 | `APP_ENV=local` 且凭据为空时自动 mock，OTP 打印到服务日志 |

---

## 9. 新增依赖

```
alibabacloud_dysmsapi20170525
alibabacloud_captcha20230305
alibabacloud_tea_openapi
```

---

## 10. 不在本次范围内

- 注册后的登录态管理（session / JWT）
- 已注册用户的手机号变更
- 短信送达回执处理
- 多语言短信模板
- 前端验证码 `SceneId` / `prefix` 的多环境运行时配置注入
- OTP 明文存储改为 HMAC/哈希存储
