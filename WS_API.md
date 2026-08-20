# XianYuApis WebSocket 对接文档

本文只说明 XianYuApis 客户端与你的 AI 服务端之间的 WebSocket 交互。

---

## 1. 概述

XianYuApis 客户端会作为 **WebSocket 客户端**连接你的 AI 服务端，完整链路如下：

```text
闲鱼用户消息
    │
    ▼
XianYuApis（本地监听）
    │ 只转发 contentType=1 / 2 的消息
    ▼
AI 服务端（WebSocket）
    │ 返回 reply
    ▼
XianYuApis（调用本地 /api/reply）
    │
    ▼
回复给闲鱼用户
```

---

## 2. 连接信息

- 默认地址：`ws://127.0.0.1:7272`
  - 配置写 `ws://0.0.0.0:7272` 时，客户端会将其规范为 `ws://127.0.0.1:7272`
- 可通过环境变量 `XY_WS_URL` 覆盖地址
- 连接断开后每 5 秒自动重连

---

## 3. contentType 说明

客户端把闲鱼消息精简后统一携带 `contentType` 字段：

| contentType | 含义 | 是否转发给 AI 服务端 |
| --- | --- | --- |
| `1` | 文本消息 | 是 |
| `2` | 图片消息 | 是 |
| `3` | 用户已拍下（待付款） | 否 |
| `4` | 用户已付款（待发货） | 否 |
| `5` | 用户发起退款 | 否 |

说明：闲鱼原始订单卡片消息的 `contentType=26`，客户端会映射为 `3/4/5` 写入本地日志，但不会转发给 AI 服务端。

---

## 4. 客户端 → 服务端消息格式

只发送 `contentType=1`（文本）和 `contentType=2`（图片）的消息，一条消息对应一条 WebSocket 消息。

### 4.1 文本消息

```json
{
  "contentType": 1,
  "cid": "65567028938",
  "senderUserId": "2217957211142",
  "reminderTitle": "卖爆米花的小女孩",
  "text": "55",
  "url": "",
  "width": 0,
  "height": 0,
  "time": "1786958735402",
  "itemId": "1072968954267",
  "clientIp": "61.163.150.192",
  "_appVersion": "7.27.70",
  "_platform": "ios"
}
```

### 4.2 图片消息

```json
{
  "contentType": 2,
  "cid": "65567028938",
  "senderUserId": "2217957211142",
  "reminderTitle": "卖爆米花的小女孩",
  "text": "",
  "url": "https://img.alicdn.com/imgextra/example.jpg",
  "width": 884,
  "height": 1920,
  "time": "1786958735402",
  "itemId": "1072968954267",
  "clientIp": "61.163.150.192",
  "_appVersion": "7.27.70",
  "_platform": "ios"
}
```

### 4.3 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `contentType` | int | 1 文本 / 2 图片 |
| `cid` | string | 会话 ID |
| `senderUserId` | string | 发送方用户 ID（要回复的用户） |
| `reminderTitle` | string | 发送方昵称 |
| `text` | string | 文本内容（图片消息为空） |
| `url` | string | 图片 CDN 地址（文本消息为空） |
| `width` / `height` | int | 图片宽高（文本消息为 0） |
| `time` | string | 消息时间戳（毫秒） |
| `itemId` | string | 商品 ID |
| `clientIp` | string | 用户 IP |
| `_appVersion` | string | 用户客户端版本 |
| `_platform` | string | 用户平台，如 `ios` |

---

## 5. 服务端 → 客户端响应格式

服务端收到消息后，返回如下 JSON：

```json
{
  "code": 0,
  "msg": "ok",
  "contentType": 1,
  "reply": "购买电影票请使用猫眼或淘票票发送座位截图哈~",
  "raw": {
    "contentType": 1,
    "cid": "65567028938",
    "senderUserId": "2217957211142",
    "reminderTitle": "卖爆米花的小女孩",
    "text": "55",
    "url": "",
    "width": 0,
    "height": 0,
    "time": "1786958735402",
    "itemId": "1072968954267",
    "clientIp": "61.163.150.192",
    "_appVersion": "7.27.70",
    "_platform": "ios"
  }
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | int | 业务码，客户端不校验，仅看 `reply` |
| `msg` | string | 提示信息 |
| `contentType` | int | 原始消息类型，1 文本 / 2 图片 |
| `reply` | string 或 array | 需要回复给用户的内容 |
| `raw` | object | 客户端上一条发送的消息原文，用于取 `cid` / `senderUserId` |

### 5.1 reply 为数组

`reply` 支持字符串数组，客户端会**按数组顺序逐条回复**：

```json
{
  "code": 0,
  "msg": "ok",
  "contentType": 1,
  "reply": [
    "第一条回复",
    "第二条回复",
    "第三条回复"
  ],
  "raw": {
    "cid": "65567028938",
    "senderUserId": "2217957211142"
  }
}
```

上面的响应会依次给用户发送 3 条消息。

---

## 6. 本地回复接口

客户端收到 `reply` 后，会调用本地接口：

```text
POST http://127.0.0.1:8000/api/reply
Content-Type: application/json
```

请求体：

```json
{
  "toid": "2217957211142",
  "cid": "65567028938",
  "text": "回复内容"
}
```

- `toid`：从 `raw.senderUserId` 获取
- `cid`：从 `raw.cid` 获取
- `text`：`reply` 数组中的每一条内容

`reply` 为数组时，数组内每条内容都会调用一次 `/api/reply`。

---

## 7. 配置与重连

- 默认连接：`ws://127.0.0.1:7272`
- 环境变量：`XY_WS_URL`，例如 `XY_WS_URL=ws://192.168.1.100:9000`
- 断线自动重连，间隔 5 秒；重连期间不影响闲鱼消息接收和本地接口
