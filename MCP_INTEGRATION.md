# MCP 接入说明（MCP_INTEGRATION.md）

本项目**真实使用**麦当劳中国 MCP（Model Context Protocol）服务。
本文件说明：实际使用的 MCP Server、Tool、调用流程与业务价值。

---

## 1. 使用的 MCP Server

| 项 | 值 |
|---|---|
| Server 名称 | `mcd-mcp` |
| 提供方 | 麦当劳中国（M-China） |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 认证方式 | 请求头 `Authorization: Bearer <MCP_TOKEN>` |
| 协议版本 | `2024-11-05` |
| 服务端标识 | `serverInfo: {"name":"mcd-mcp","version":"1.0.0"}` |
| 可用 Tool 数 | **35** |
| 限流 | 每 Token 每分钟 ≤ 600 次请求，超出返回 `429` |
| Token 获取 | https://open.mcd.cn/mcp → 手机号登录 → 控制台 → 激活 |
| 官方文档 | https://github.com/M-China/mcd-mcp-server |

### 接入方式

**方式 A：WorkBuddy 自定义连接器**

WorkBuddy → 左侧栏【专家·技能·连接器】→【连接器】→ 右上角【自定义连接器】→ 填入配置 → 保存 → 启用。
启用后工具以 `mcp__mcd-mcp__<tool-name>` 形式暴露给智能体。

**方式 B：标准 MCP 协议直连（本项目采用）**

`scripts/mcd_client.py` 为**零依赖**（仅 Python 标准库）的 MCP 客户端实现，
完整走通 `initialize` → `notifications/initialized` → `tools/list` → `tools/call`。

采用方式 B 的原因：本项目需要把预估模型与 MCP 调用封装在一起，作为可独立运行的
Skill 交付，不依赖宿主环境是否已配置连接器。

配置文件见 [`mcp-config.example.json`](./mcp-config.example.json)，**仅含环境变量占位符**。

---

## 2. 实际使用的 Tool

服务端共开放 35 个 Tool（经 `tools/list` 实测确认）。本项目实际调用的 9 个如下：

| Tool | 在本项目中的作用 | 调用时机 |
|---|---|---|
| `now-time-info` | 取服务器时间（GMT+08:00），作为时段系数判定的**唯一时间基准**，避免本机时区/时钟偏差 | 每次预估前 |
| `query-nearby-stores` | 按城市+位置关键词定位校内门店，获得 `storeCode` | 首次配置 / 换店 |
| `query-meals` | 拉取门店在售餐品与分类（`categories[].meals[].code` + `data.meals[code].name/currentPrice`） | 每次推荐前 |
| `query-meal-detail` | 拆解套餐组成，细化出餐复杂度 | 估算组合餐时 |
| `calculate-price` | 下单前核价（金额单位为**分**，展示需除 100） | 确认订单前 |
| `query-store-coupons` | 查询门店可用券，作为推荐的价格修正项 | 推荐打分时 |
| `campaign-calendar` | 判断当日是否有促销/上新活动 → `K_campaign` | 每次预估前 |
| `list-nutrition-foods` | 160 条餐品营养数据 → 推荐中的营养维度打分 | 缓存后复用 |
| `query-order` | **轮询订单状态，实测「下单→配餐完成」真实耗时 → 回填自校准** | 下单后 |

服务端完整 35 个 Tool 清单及其业务域分组见 `docs/mcp-tools.md`。

---

## 3. 调用流程

### 3.1 主链路：课间窗口快取推荐（`cli.py advise`）

```
① now-time-info                → 服务器时间（时段基准）
② campaign-calendar            → 当日是否命中促销/上新 → K_campaign
③ query-meals(storeCode,        → 门店在售餐品与分类
              orderType=1,
              beType=1)
④ 本地：list-nutrition-foods 缓存 → 营养维度打分输入
⑤ 本地：PrepModel.estimate()   → 逐餐品预估等待时长与置信区间
⑥ 本地：recommend()            → 在课间预算内排序，输出 Top3 与避坑提示
```

> 说明：⑤⑥ 为本地建模，因为 **MCP 不提供出餐时长信号**（已对 35 个 Tool 全量核查，唯一的
> `expectDeliveryTime` 属于外送场景的预计送达时间）。这是本项目的核心技术工作。

### 3.2 自校准链路（`cli.py watch`）

```
① create-order / 用户自行下单  → 得到 orderId
② query-order(orderId) 轮询
     订单状态：1 待支付 / 2 配餐中 / 3 配送中 / 4 已完成 / 5 已取消 / 10 餐厅确认配餐中
③ 命中 2/10/3/4 中的首个时刻  → 得到「下单 → 配餐完成」的耗时
④ calibration_guard 五道防线   → 判定该样本是否可信（见下）
⑤ PrepModel.calibrate()        → 信任加权 + 指数滑动修正门店系数，写回 state.json
```

**⚠️ 重要限制与应对**：`orderStatus` 是**门店员工手动操作的状态机，不是传感器**。
门店在压单时会批量把订单点成「已完成」以清空操作屏，导致该时刻早于餐品真实交付时刻，
且偏差幅度与高峰负载正相关（内生性污染）。

为此本项目实施了五道纵深防线：

| 防线 | 作用 |
|---|---|
| R1 物理下界 | 低于物理不可能时长的样本直接丢弃 |
| R2 状态跳变 | 首次观测即「已完成」且间隔过短的样本丢弃 |
| R3 批量清屏 | 同门店 90 秒内样本聚集 → 判定为操作屏批量点击，整批丢弃 |
| R4 负载单调性 | 实测等待与负载显著负相关 → **冻结自动校准** |
| R5 保守单向 | 判定口径强制 `K_store ≥ 1.0`，**污染无法使判定变乐观** |

完整推演见 `docs/data-integrity.md`，可复现验证见 `tests/test_guard.py`。

该闭环全部基于官方 MCP 能力，无自建采集、无爬虫。
另提供 `source="user"` 通道（用户确认真实取餐时刻），可信度高于状态机口径。

### 3.3 实测调用样例

```json
{
  "success": true, "code": 200, "message": "请求成功",
  "datetime": "2026-10-16 19:20:45",
  "traceId": "df0657c36abaac2926b76df50cf1b0ad",
  "data": {
    "timestamp": 1792149645705,
    "formatted": "2026-10-16 19:20:45",
    "timezone": "GMT+08:00",
    "dayOfWeek": "FRIDAY"
  }
}
```

`query-nearby-stores` 在验证门店的真实返回：

```json
{
  "storeCode": "3450415",
  "storeName": "麦当劳德阳罗江区四川工业科技学院校内商业街餐厅",
  "address": "四川工业科技学院校内商业街",
  "distance": 337,
  "businessStartTime": "09:00",
  "businessEndTime": "23:00",
  "reservation": true
}
```

---

## 4. 业务价值

### 4.1 对学生：把"不确定"变成"可决策"

原本学生面对的是完全未知的等待时长，只能二选一：

- 到店再点 → 可能等 8 分钟，课间只有 10 分钟 → 迟到
- 提前点好 → 可能 5 分钟就做好，放 10 分钟 → 口感变差

本项目把等待时长显性化，并给出「课间是否来得及」的明确判断与替代餐品建议，
让学生**在下单前**就能做出不会后悔的选择。

### 4.2 对门店：用推荐做削峰填谷

推荐排序天然把客流引向**出餐快**的餐品（饮品、小食、卷类），减少同一时段对
慢工序餐品（安格斯厚牛堡 4.2 分钟基准）的集中冲击，平缓课间瞬时峰值。

门店系数与负载系数会随真实耗时自我修正，**用得越久越准**。

### 4.3 对 MCP 生态：补齐一个官方未覆盖的能力层

麦当劳 MCP 提供了完整的**交易与信息**能力，但不提供**时间预期**能力。
本项目在官方能力之上构建了可解释、可校准的时间预估层，是一个纯粹的增量价值，
不改变任何官方业务语义。

---

## 5. 合规与安全说明

- 配置文件仅使用环境变量占位符（`${MCD_MCP_TOKEN}`），**仓库中不包含任何真实 Token、密钥或账号凭证**
- 所有数据均通过麦当劳官方 MCP 接口按需获取，**未做额外的数据采集、存储或二次分发**
- 本机仅缓存两类数据：`nutrition.txt`（公开营养数据）与 `state.json`（脱敏后的耗时样本，不含订单号、手机号等个人标识）
- 本项目为参赛作品，由参赛者独立开发，**非麦当劳官方产品**
- 餐品信息、价格及供应状态以麦当劳官方渠道的实时结果为准
- 项目输出仅供参考，不构成医疗、营养或其他专业建议
