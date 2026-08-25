# 闲鱼客户端插件网关 WebSocket 对接文档

本文说明 `gf-auto` 客户端和 `yhs-plugin-gateway` 的当前对接方式。

## 1. 总体链路

```text
闲鱼用户消息
    │
    ▼
gf-auto 本地监听
    │ 上报 contentType=1 / 2 / 3 / 4 / 5 / 6
    ▼
yhs-plugin-gateway
    │ 转发 yhs-bus，按 bus 任务下发回复、查订单、改价
    ▼
gf-auto 本地 /api/reply、/api/order_detail、/api/adjust_price
    │
    ▼
回复给闲鱼用户
```

当前阶段已经进入正式业务联调：文本/图片/拍下/付款/退款都会上报网关，网关转发 `yhs-bus`，业务动作通过 `task.xianyu.*` 下发给客户端执行。

## 2. 登录

登录地址固定为：

```text
POST https://plugin-gateway.yinghuasuan.com/api/v1/client/login
```

请求示例：

```json
{
  "username": "pub子账号",
  "password": "密码",
  "deviceId": "fish-client-xxxx",
  "clientType": "store-plugin",
  "businessType": "xianyu",
  "platform": "fish"
}
```

说明：

- `businessType` 固定传 `xianyu`。
- `platform` 固定传 `fish`。
- `deviceId` 会保存到本机 `~/.xianyu/login.json`，同一台电脑尽量保持不变。
- 登录成功后使用响应里的 `data.accessToken` 和 `data.wsUrl`。

## 3. WebSocket 连接和绑定

优先连接登录接口返回的 `data.wsUrl`，兜底地址：

```text
wss://plugin-gateway.yinghuasuan.com/ws
```

连接成功后先发送绑定消息：

```json
{
  "version": "plugin.v1",
  "type": "client.bind",
  "id": "bind_xxx",
  "payload": {
    "token": "登录接口返回的 accessToken"
  }
}
```

绑定成功：

```json
{
  "version": "plugin.v1",
  "type": "client.bind.ack",
  "payload": {
    "success": true,
    "clientId": "7f00000108fc00000001",
    "serverTime": "2026-08-19T10:00:00+08:00"
  }
}
```

绑定失败时，客户端需要重新登录获取 token。

## 4. 心跳

客户端每 25 秒发送一次：

```json
{
  "version": "plugin.v1",
  "type": "client.heartbeat",
  "id": "heartbeat_xxx",
  "payload": {}
}
```

网关返回：

```json
{
  "version": "plugin.v1",
  "type": "server.pong",
  "payload": {
    "clientId": "7f00000108fc00000001"
  }
}
```

## 5. 上报闲鱼消息

客户端把闲鱼消息精简后统一放到 `payload`，外层使用网关信封：

```json
{
  "version": "plugin.v1",
  "type": "xianyu.message",
  "id": "msg_xxx",
  "sentAt": "2026-08-19T10:03:00+08:00",
  "payload": {
    "storeId": 1001,
    "contentType": 1,
    "cid": "65567028938",
    "senderUserId": "2217957211142",
    "reminderTitle": "卖爆米花的小女孩",
    "text": "你好",
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

图片消息把 `contentType` 改为 `2`，并填写 `url/width/height`。

订单卡片消息使用同一个 `xianyu.message` 外壳，`contentType` 分别为：

- `3`：用户已拍下，待付款。
- `4`：用户已付款，待发货。
- `5`：用户发起退款。
- `6`：用户关闭未付款订单。

订单消息需要尽量带上 `orderId/cid/senderUserId/reminderTitle/itemId/time`，其中 `orderId` 是后续改价、查详情和建单的关键字段。

## 6. contentType 说明

| contentType | 含义 | 当前是否上报网关 |
| --- | --- | --- |
| `1` | 文本消息 | 是 |
| `2` | 图片消息 | 是 |
| `3` | 用户已拍下，待付款 | 是 |
| `4` | 用户已付款，待发货 | 是 |
| `5` | 用户发起退款 | 是 |
| `6` | 用户关闭未付款订单 | 是 |

闲鱼原始订单卡片消息的 `contentType=26`，客户端会映射为 `3/4/5` 后上报网关。订单关闭系统通知通常为原始 `contentType=14/28`，客户端映射为 `6`；通知未带订单号时，仅使用同会话最近收到的订单号作为兜底。

## 7. 网关 ACK

`xianyu.message` 上报后，网关只返回接收确认：

```json
{
  "version": "plugin.v1",
  "type": "xianyu.message.ack",
  "payload": {
    "accepted": true,
    "eventType": "xianyu.message",
    "forwardBus": true,
    "idempotencyKey": "xianyu:fish:1:1001:xianyu.message:msg_xxx"
  }
}
```

业务回复不是 ACK 里返回，而是由网关后续下发 `task.xianyu.*` 任务。`xianyu.test` 等非正式测试事件仍可能返回 `xianyu.echo`。

## 8. 网关任务

网关会下发三类闲鱼任务。客户端收到后先回 `task.ack`，执行完成后回 `task.result`。

### 8.1 发送消息

任务类型：`task.xianyu.send_message`

```json
{
  "version": "plugin.v1",
  "type": "task.xianyu.send_message",
  "id": "task_xxx",
  "payload": {
    "taskId": "task_xxx",
    "taskType": "task.xianyu.send_message",
    "payload": {
      "toid": "2217957211142",
      "cid": "65567028938",
      "itemId": "1072968954267",
      "text": "回复内容"
    }
  }
}
```

客户端执行本地：

```text
POST http://127.0.0.1:8000/api/reply
```

### 8.2 查询订单详情

任务类型：`task.xianyu.get_order_detail`

客户端执行本地：

```text
POST http://127.0.0.1:8000/api/order_detail
```

请求核心字段：`orderId`。

### 8.3 修改订单价格

任务类型：`task.xianyu.adjust_price`

客户端执行本地：

```text
POST http://127.0.0.1:8000/api/adjust_price
```

请求核心字段：

- `orderId`：闲鱼订单号。
- `modifyFee`：改价后的金额，单位分。
- `newTransportFee`：运费，单位分，当前传 `0`。

### 8.4 虚拟发货

任务类型：`task.xianyu.consign_dummy`

客户端执行本地：

```text
POST http://127.0.0.1:8000/api/consign_dummy
```

请求核心字段：

- `orderId`：闲鱼订单号。
- `tradeText`：发货说明，默认“已出票”。
- `picList`：物流凭证图片列表，当前业务传空数组。

### 8.5 task.ack

```json
{
  "version": "plugin.v1",
  "type": "task.ack",
  "payload": {
    "taskId": "task_xxx",
    "taskType": "task.xianyu.send_message",
    "receivedAt": "2026-08-19T10:03:02+08:00"
  }
}
```

### 8.5 task.result

```json
{
  "version": "plugin.v1",
  "type": "task.result",
  "payload": {
    "taskId": "task_xxx",
    "taskType": "task.xianyu.send_message",
    "status": "success",
    "success": true,
    "errorCode": null,
    "errorMessage": null,
    "result": {
      "ok": true
    },
    "finishedAt": "2026-08-19T10:03:03+08:00"
  }
}
```

## 9. 配置与重连

- `XY_WS_URL` 可覆盖网关 WS 地址，一般不需要配置，优先使用登录接口返回的 `wsUrl`。
- 连接断开后会自动重连。
- token 失效或绑定失败时，需要重新登录。
