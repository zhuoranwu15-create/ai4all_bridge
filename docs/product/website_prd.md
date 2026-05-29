# 朝夕相伴 官网 PRD

> 上级文档：[onboarding_prd.md](onboarding_prd.md)  
> 状态：开发中  
> 更新：2026-05-29

---

## 一、产品目标

为朝夕相伴提供一个用户可独立访问的官网，承载两个核心功能：

1. **注册 / 登录**：手机号 OTP 验证 + 微信扫码绑定
2. **用户中心**：查看账号信息、贝壳余额、微信绑定状态

官网是微信聊天之外唯一能显示账号信息、贝壳余额和后续充值入口的地方。

**域名**：ai4company.top（开发期本地 `http://localhost:8180/ui/home.html`）

---

## 二、品牌规范

品牌名「**朝夕相伴**」：
- **朝夕**：品牌标识，字重 800，主色 `#E8824A`
- **相伴**：修饰词，字重 300，颜色 `var(--text-sub)`，间距略开

主色系：

| Token | 值 | 用途 |
|---|---|---|
| `--brand` | `#E8824A` | 主按钮、强调色 |
| `--brand-light` | `#FAE8D8` | 次级按钮背景 |
| `--brand-dark` | `#C4622E` | hover 状态 |
| `--bg` | `#FFFAF7` | 页面底色（暖白）|

---

## 三、页面清单

| 文件 | URL | 说明 |
|---|---|---|
| `app/static/home.html` | `/ui/home.html` | 首页 + 注册 / 登录 |
| `app/static/dashboard.html` | `/ui/dashboard.html` | 用户中心 |
| `app/static/site.css` | `/ui/site.css` | 官网公共样式（与 admin 隔离）|

---

## 四、功能详细设计

### 4.1 首页（home.html）

#### Hero 区
- 品牌 logo + slogan（「你的微信专属 AI 陪伴」）
- 品牌配图位（占位符，物料就绪后替换）
- 三行副文案：手机号验证 · 微信扫码接入 · 开箱即用

#### 注册 / 登录卡片
步骤指示器（2 步）+ 以下流程：

```
用户输入手机号
  → 阿里云验证码 SDK（popup 模式）
  → 通过 → POST /web/sms/send-otp
  → 发送成功 → 展开 OTP 输入框

用户输入 6 位验证码
  → POST /web/sms/verify-otp → verified_token
  → POST /web/login（消费 token，创建 session）

    ├── has_active_binding = true
    │     → 保存 session_token → 跳转 dashboard.html
    │
    └── has_active_binding = false
          → 展示 binding_intent 二维码
          → 轮询 GET /web/binding-intents/{id}（3s）
          → status=completed → 跳转 dashboard.html
```

#### 会话复用
页面加载时检查 `localStorage.chaochao_session`：
- 存在 → 调用 `GET /web/me` 验证
  - 有效 → 直接跳转 dashboard.html
  - 无效 / 过期 → 清除，展示注册表单

---

### 4.2 用户中心（dashboard.html）

**鉴权**：页面加载时检查 session，无效则跳转 home.html。

#### 账号信息卡
- 手机号（脱敏展示保留后端实际值）
- Account ID（mono 字体）
- 套餐（当前为 free）
- 注册时间

#### 贝壳余额卡
- 余额展示（当前硬编码 0，待 wallet 模块接入）
- 「充值」按钮（disabled，标注「敬请期待」）

#### 微信绑定卡
- 已绑定状态：绿色徽章 + 微信账号标识 + 最近活跃时间
- 未绑定状态：橙色徽章提示
- 「重新绑定」按钮：调用 `POST /web/binding-intents` → 展示新 QR → 轮询

#### 退出登录
- 清除 `localStorage.chaochao_session` → 跳转 home.html

---

## 五、新增后端接口

### 5.1 POST /web/login

替代首页调用 register-and-binding-intent 的完整登录入口：

- 输入：`{verified_token, phone}`
- 消费 verified_token（一次性）
- 创建或复用 platform_user + 默认 account
- 创建 7 天 session_token
- 若无活跃 channel_binding，自动创建 binding_intent（返回 QR）
- 返回：`{session_token, platform_user, account, subscription, has_active_binding, binding_intent}`

### 5.2 GET /web/me

- 鉴权：`Authorization: Bearer <session_token>`
- 返回当前 platform_user、account、subscription
- session 过期 → 401

### 5.3 GET /web/me/bindings

- 鉴权同上
- 返回该 account 下所有 channel_bindings

### 5.4 修改 POST /web/register-and-binding-intent

- 新增返回字段：`session_token`（7 天有效）
- 原有字段不变，向后兼容

---

## 六、数据库变更

新增表 `platform_user_sessions`：

```sql
CREATE TABLE IF NOT EXISTS platform_user_sessions (
    id TEXT PRIMARY KEY,             -- sess_{uuid}
    platform_user_id TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,      -- secrets.token_urlsafe(32)
    expires_at TEXT NOT NULL,        -- datetime ISO，7 天后过期
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(platform_user_id) REFERENCES platform_users(id)
);
```

---

## 七、V1 范围边界

**包含**
- 手机号 OTP 注册 / 登录
- 微信扫码绑定（含重新绑定）
- Session 持久化（7 天）
- 账号信息展示
- 贝壳余额展示（固定为 0）

**不包含（后续版本）**
- 贝壳充值 / 支付
- 邀请码拉新
- 通知偏好设置
- 历史消息查看
- 多微信账号绑定管理
- 解绑功能

---

## 八、验收标准

1. 新用户完整流程：手机号 → OTP → 二维码 → 扫码 → 跳转用户中心
2. 已绑定用户：手机号 → OTP → 直接跳转用户中心（无需再扫码）
3. 刷新用户中心不需要重新登录（session 7 天有效）
4. 退出后再进入 home.html 需重新验证
5. 用户中心显示正确手机号、account ID、plan、绑定状态
6. 在 ai4company.top 域名下可正常访问
