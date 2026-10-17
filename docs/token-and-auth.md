# Token 获取方式与授权边界（实测）

> 问题：用户在小程序/App 登录、或下单前后，能不能自动拿到麦当劳 MCP Token？
> 结论：**不能。麦当劳未提供任何 OAuth 授权码流程，也没有任何"替用户自动获取 Token"的官方接口。**
> 实测时间：2026-10-17

---

## 一、实测证据

| 探测项 | 结果 | 判读 |
|---|---|---|
| `POST https://mcp.mcd.cn`（不带 Authorization） | **403** + `{"code":"0001","msg":"系统错误"}`，响应头**无 `WWW-Authenticate`** | 按 MCP 规范，支持 OAuth 时应返回 **401 + `WWW-Authenticate: Bearer resource_metadata=...`**。返回 403 且无该头 → **不支持 OAuth 发现** |
| `mcp.mcd.cn/.well-known/oauth-protected-resource` | **404** `{"error_msg":"404 Route Not Found"}` | 无受保护资源元数据 |
| `mcp.mcd.cn/.well-known/oauth-authorization-server` | **404** | 无授权服务器元数据 |
| `open.mcd.cn/.well-known/oauth-authorization-server` | **200，但 `content-type: text/html`，3661 字节** | **假 200** |
| `open.mcd.cn/api/oauth/token` | 同上，3661 字节 HTML | **假 200** |
| `open.mcd.cn/definitely-not-a-real-path-xyz`（对照） | 同上，**也是 3661 字节 HTML** | 与上两行**完全一致** → 证明是**前端路由通配回退**，不是真端点 |
| 官方 `M-China/mcd-mcp-server` README 关键词计数 | `OAuth` 0、`oauth` 0、`授权码` 0、`authorization code` 0；`Bearer` 2、`Token` 20、`激活` 1 | 官方只描述**手动领取**流程 |

官方 README 的原文流程：

> 1. 申请 MCP Token
> 2. 点击**激活**按钮，申请 MCP Token
> 3. 第五步：MCP Token 申请成功，可以**一键复制**
> 4. 复制下面的配置，替换 `YOUR_MCP_TOKEN` 为实际 MCP Token

错误码表也印证：

> `401 | MCP Token 无效、已过期或未提供`

**结论：Token 是一个由用户本人在 `open.mcd.cn` 控制台激活后手动复制的静态 Bearer 凭证，
没有授权码换取、没有 refresh token、没有 scope 协商。**

---

## 二、必须先分清的两套东西

很多人会把它们混为一谈，但它们是**互不相通**的：

| | 麦当劳 App / 小程序账号 | 麦当劳 MCP Token |
|---|---|---|
| 是什么 | 用户的**消费身份** | 开发者/应用**调用 MCP 的凭证** |
| 在哪获得 | McDonald's App、微信小程序登录 | `open.mcd.cn/mcp` → 手机号登录 → 控制台 → 激活 |
| 能做什么 | 点餐、看订单、用券 | 调用 35 个 MCP Tool |
| 两者关系 | **App 登录不会产生 MCP Token** | MCP Token 也不代表用户身份 |

所以"用户登录 App 时顺便拿到 MCP Token"这件事，**在数据关系上就不成立**——App 里根本没有这个东西。

---

## 三、三条绝对不能做的事

| 做法 | 为什么不行 |
|---|---|
| **截取用户 App / 小程序的登录态或凭据** | 属于**未授权获取他人账号凭证**。违反《网络安全法》《个人信息保护法》，也违反麦当劳《使用条款》 |
| **引导用户把账号密码交给第三方换取 Token** | 同上，且是典型钓鱼形态 |
| **把 MCP Token 打包进客户端分发** | 官方 README 明确要求「**请妥善保管 MCP Token，避免泄露给他人**」；打包分发等于公开泄露 |

> 第 3 条特别提醒：把 Token 写进小程序/App 的前端包或配置里，**任何人反编译都能拿到**。
> 这是最常见的"图省事"错误，也是官方条款直接禁止的行为。

---

## 四、四条合法路径

### 路径 1：离线快照（已实现，零 Token）

`scripts/sample_data/` 内置真实 MCP 返回的快照（120 项餐品、活动日历、营养、门店）。
未设置 Token 时脚本自动回落，并在终端与页面明确标注「离线快照 · 抓取于 …」。

**适用**：评委、路人、同学——clone 下来直接能跑，不需要任何凭据。

### 路径 2：静态看板（已上线，零 Token）

GitHub Pages 上的三份页面本来就是静态 HTML，**打开即用，完全不需要 Token**。

- 首页 https://asdunlilj.github.io/mcd-break-window/
- 课间推荐 `preview-client.html`
- 订单追踪 `order-status.html`

**适用**：演示、拉星、给门店经理看效果。

### 路径 3：引导用户自助领取（推荐给真实用户）

不代取，只把入口和步骤做好：

```
打开 https://open.mcd.cn/mcp → 手机号登录 → 控制台 → 激活 → 一键复制
→ 设为环境变量 MCD_MCP_TOKEN
```

CLI 在缺 Token 时已经直接打印这段指引（见 `scripts/offline.py` 的 `offline_banner()`）。

**适用**：愿意用实时数据的同学/店长本人。**这是官方唯一认可的路径。**

### 路径 4：服务端代理（B 端，含【待确认】风险）

由部署方持有 Token，在服务端调用 MCP，对终端用户做鉴权与限流。

- **技术上比"把 Token 打进客户端"安全得多**（Token 不出服务端）
- **但**：一个 Token 服务多个终端用户，是否在授权范围内，**官方文档没有写**
  → 【待确认】需向麦当劳开放平台确认
- 且官方明确提示"避免泄露给他人"，公开部署一个带真 Token 的公共服务有违约风险
- **只能用于**：小范围、知情同意、内部试用（例如本校本店试点）

---

## 五、对 "让没有 Token 的人也能用" 的落地建议

| 场景 | 该用什么 | Token 需求 |
|---|---|---|
| 评委 / 路人看效果 | 路径 2（Pages 静态页） | 无 |
| 同学 clone 下来跑 | 路径 1（离线快照） | 无 |
| 同学要用实时数据 | 路径 3（自助领取） | 每人自己申请 |
| 校内小范围试点 | 路径 4（服务端代理，先向官方确认） | 部署方持有 1 个 |

---

## 六、一句话总结

> **官方没有 OAuth，Token 只能用户本人在控制台手动领取。**
> 想"替用户拿到 Token"这件事，技术上没有接口，合规上也不允许。
> 能做的是三件：**给没有 Token 的人一条离线可用的路（已做）、给看效果的人一个在线链接（已做）、给要用实时数据的人一条清晰的领取指引（已做）。**
