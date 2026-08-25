# XianYuApis 本地接口文档

运行 `python goofish_live.py` 后会同时启动本地 HTTP 服务，默认地址：

```text
http://127.0.0.1:8000
```

可通过环境变量修改监听地址和端口：

- `XY_API_HOST`：默认 `127.0.0.1`
- `XY_API_PORT`：默认 `8000`

所有接口均使用 JSON 格式传输，字符集为 UTF-8。

> Windows cmd 不支持用 `\` 换行，直接粘贴多行 curl 会解析失败，请使用单行命令；PowerShell 可用下方的 `Invoke-RestMethod` 写法。

---

## 1. 健康检查

### GET /health

检查本地服务与 WebSocket 连接状态。

示例：

```bash
curl http://127.0.0.1:8000/health
```

成功响应：

```json
{
  "status": "ok",
  "ws_connected": true
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | string | 固定为 `ok` |
| `ws_connected` | bool | 闲鱼 WebSocket 是否已连接 |

---

## 2. 回复消息

### POST /api/reply

`/api/send` 是本接口的别名，两者等价。

给指定用户回复文字或图片消息。

请求头：

```text
Content-Type: application/json
```

请求参数：

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `toid` | 是 | string | 对方用户 ID，也接受 `send_user_id` / `user_id` |
| `cid` | 否 | string | 会话 ID；不传时自动从最近会话匹配 |
| `item_id` | 否 | string | 找不到会话时，传该字段可自动创建会话 |
| `text` | 否 | string | 文字内容，与 `image_url` 二选一 |
| `image_url` | 否 | string | 图片地址 |
| `width` | 否 | int | 图片宽度 |
| `height` | 否 | int | 图片高度 |
| `message` | 否 | object | 也可直接传消息对象，如 `{"type": "text", "text": "..."}` |

文字消息示例：

```bash
curl -X POST http://127.0.0.1:8000/api/reply \
  -H 'Content-Type: application/json' \
  -d '{
    "toid": "2202640918079",
    "cid": "47812870000",
    "text": "你好，收到！"
  }'
```

不传 `cid` 自动匹配会话：

```bash
curl -X POST http://127.0.0.1:8000/api/send \
  -H 'Content-Type: application/json' \
  -d '{
    "toid": "2202640918079",
    "text": "在的"
  }'
```

发送图片：

```bash
curl -X POST http://127.0.0.1:8000/api/reply \
  -H 'Content-Type: application/json' \
  -d '{
    "toid": "2202640918079",
    "image_url": "https://example.com/a.jpg",
    "width": 800,
    "height": 600
  }'
```

成功响应：

```json
{
  "ok": true,
  "cid": "47812870000",
  "toid": "2202640918079",
  "type": "text"
}
```

错误码：

| HTTP 状态码 | 说明 |
| --- | --- |
| `400` | 缺少必填参数，或消息类型不支持 |
| `404` | 找不到与 `toid` 的会话，且未提供 `cid` / `item_id` |
| `500` | WebSocket 未连接、发送超时或发送失败 |

---

## 3. 订单改价

### POST /api/adjust_price

闲鱼卖家端订单改价，金额单位为分。

请求头：

```text
Content-Type: application/json
```

请求参数：

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `order_id` | 是 | string | 订单 ID，也接受 `orderId` |
| `modify_fee` | 是 | string | 修改后的商品金额（分），也接受 `modifyFee` |
| `new_transport_fee` | 是 | string | 修改后的运费（分），也接受 `newTransportFee` |

示例：

```bash
curl -X POST http://127.0.0.1:8000/api/adjust_price \
  -H 'Content-Type: application/json' \
  -d '{
    "order_id": "5127190491362214211",
    "modify_fee": "1200",
    "new_transport_fee": "0"
  }'
```

也支持 curl 原始字段名：

```json
{
  "orderId": "5127190491362214211",
  "modifyFee": "1200",
  "newTransportFee": "0"
}
```

Windows cmd 单行示例：

```bat
curl -X POST http://127.0.0.1:8000/api/adjust_price -H "Content-Type: application/json" -d "{\"order_id\":\"5127190491362214211\",\"modify_fee\":\"1300\",\"new_transport_fee\":\"0\"}"
```

PowerShell 示例：

```powershell
$body = '{"order_id":"5127190491362214211","modify_fee":"1300","new_transport_fee":"0"}'
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/adjust_price -Method Post -ContentType "application/json" -Body $body
```

成功响应：

```json
{
  "ok": true,
  "result": {
    "ret": ["SUCCESS::调用成功"]
  }
}
```

`result` 内容为闲鱼服务端返回的原始 JSON。

错误码：

| HTTP 状态码 | 说明 |
| --- | --- |
| `400` | 缺少 `order_id`、`modify_fee` 或 `new_transport_fee` |
| `500` | 请求闲鱼接口失败 |

---

## 4. 订单查询

### POST /api/order_detail

按订单号精确查询单个订单详情。

请求参数：

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `orderId` | 是 | string | 订单号，也接受 `order_id` |

Windows cmd 单行示例：

```bat
curl -X POST http://127.0.0.1:8000/api/order_detail -H "Content-Type: application/json" -d "{\"orderId\":\"5127190491362214211\"}"
```

PowerShell 示例：

```powershell
$body = '{"orderId":"5127190491362214211"}'
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/order_detail -Method Post -ContentType "application/json" -Body $body
```

成功响应：

```json
{
  "ok": true,
  "order": {
    "buyerInfoVO": {},
    "commonData": {
      "orderId": "5127190491362214211",
      "orderStatus": "待付款"
    },
    "itemVO": {},
    "priceVO": {}
  }
}
```

---

## 5. 虚拟发货

### POST /api/consign_dummy

调用闲鱼卖家端虚拟发货接口。MTop 返回 `ret` 非 `SUCCESS` 时接口会返回失败。

请求参数：

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `orderId` | 是 | string | 闲鱼订单号，也接受 `order_id` |
| `tradeText` | 否 | string | 发货说明，默认“已出票” |
| `picList` | 否 | array | 物流凭证图片列表，默认空数组 |

```bat
curl -X POST http://127.0.0.1:8000/api/consign_dummy -H "Content-Type: application/json" -d "{\"orderId\":\"5127190491362214211\",\"tradeText\":\"已出票\",\"picList\":[]}"
```

`order` 为单个订单详情对象。

错误码：

| HTTP 状态码 | 说明 |
| --- | --- |
| `400` | 缺少 `orderId` |
| `404` | 未找到该订单 |
| `500` | 请求闲鱼接口失败 |

---

## 6. 卖家取消订单

### POST /api/cancel_order

调用闲鱼卖家端取消订单接口，仅用于已付款但尚未发货的订单。

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `orderId` | 是 | string | 闲鱼订单号，也接受 `order_id` |
| `closeReason` | 否 | string | 取消原因，默认“与买家协商一致” |

```bat
curl -X POST http://127.0.0.1:8000/api/cancel_order -H "Content-Type: application/json" -d "{\"orderId\":\"5127190491362214211\",\"closeReason\":\"与买家协商一致\"}"
```

---

## 通用错误格式

所有接口的错误响应统一为：

```json
{
  "error": "错误原因描述"
}
```
