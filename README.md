# 🐟 XianYuApis — “闲鱼”第三方API集成库，AI客服智能体底座 

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![Node.js](https://img.shields.io/badge/node.js-18%2B-green)](https://nodejs.org/)
[![License](https://img.shields.io/badge/license-MIT-orange)](LICENSE)

> **在 AI 大模型爆发的时代，每一个闲鱼卖家都值得拥有一个 7×24 小时不下线的智能客服。**
> 本项目封装了闲鱼平台完整的消息通信能力，为开发者构建 AI 客服智能体提供可靠、稳定的底层 API 支撑。

**⚠️ 严禁用于发布不良信息、违法内容！如有侵权请联系作者删除。**

---

## 为什么需要这个项目？

```
用户私信 ──► [XianYuApis] ──► 你的 AI Agent（LLM / RAG / 规则引擎）──► 自动回复
                ▲                                                          │
                └──────────────── 发送消息 / 图片 ◄────────────────────────┘
```

闲鱼官方没有开放 IM 消息接口。想要接入 GPT、Claude、本地大模型来做智能客服，首先需要能**稳定收发消息**。XianYuApis 解决的正是这个前置问题：

- 逆向还原了闲鱼 WebSocket 私信协议（sign 签名 + base64 + Protobuf）
- 封装全部 HTTP 接口（sign 参数已解密）
- 提供统一的消息收发抽象层，开发者只需关注业务逻辑

**你负责接 AI 大脑，我们负责打通闲鱼的神经。**

---

## 已实现功能

| 模块 | 功能 | 状态 |
|------|------|------|
| HTTP API | 闲鱼所有 HTTP 接口（sign 签名已解密） | ✅ |
| WebSocket | 私信实时收发（sign + base64 + Protobuf 协议） | ✅ |
| 消息类型 | 文字、图片消息 | ✅ |
| 会话管理 | 获取全部历史聊天记录 | ✅ |
| 主动发送 | 主动向指定用户发消息 | ✅ |
| Token 维持 | 自动刷新登录态，常驻进程不掉线 | ✅ |
| 获取聊天记录 | 获取与指定用户的历史消息记录 | ✅ |
| 商品信息 | 获取商品详情 | ✅ |
| 媒体上传 | 上传图片并发送 | ✅ |
| 登录 | Chrome 登录自动保存 Cookie，失效自动重登 | ✅ |


---

## 成品案例 在本项目基础上继续构建的Agent项目

- [XianyuAutoAgent](https://github.com/shaxiu/XianyuAutoAgent) — 基于本项目构建的闲鱼 AI 全自动客服智能体
- [xianyu-auto-reply](https://github.com/zhinianboke/xianyu-auto-reply) -  基于本项目构建的闲鱼自动回复系统
- [xianyu-auto-reply-fix](https://github.com/GuDong2003/xianyu-auto-reply-fix) -  基于本项目构建的闲鱼闲鱼管理系统
- [xianyu-auto-reply](https://github.com/zhinianboke-new/xianyu-auto-reply) -  基于本项目构建的闲鱼 AI 全自动客服智能体
- [xianyu-auto-reply](https://github.com/HJYHJYHJY/xianyu-auto-reply) -  基于本项目构建的闲鱼闲鱼自动回复系统
- [xianyu-super-butler](https://github.com/23Star/xianyu-super-butler) -  基于本项目构建的闲鱼闲鱼超级管家
- [XianyuAutoAgent](https://github.com/qOeOp/XianyuAutoAgent) -  基于本项目构建的闲鱼智能闲鱼客服机器人系统



> 欢迎提交你基于本项目构建的 AI 应用，PR 随时欢迎！

---

## 快速开始

### 环境要求

- Python 3.9+
- Node.js 18+（用于执行签名算法 JS）

### 安装依赖

```bash
pip install -r requirements.txt
```

### 首次登录（自动获取并保存 Cookie）

首次运行会检查本地 `cookies.json`：

- 存在且有效：直接使用本地 Cookie 启动
- 不存在或已失效：自动打开本机 Google Chrome 访问 [seller.goofish.com](https://seller.goofish.com/)，手动登录后程序会自动读取 Cookie 并保存到 `cookies.json`

程序默认每 600 秒做一次登录态心跳，失效时会自动重新打开 Chrome 登录，并重连 WebSocket，不需要手动改代码。


### 直接运行

桌面版（推荐）：打开后会显示启动窗口，点击“一键启动”后程序才开始运行，窗口内会实时显示运行日志（监听 IP 和端口已内置）：

```bash
python gui.py
```

命令行版：

```bash
python goofish_live.py
```

命令行版默认不输出终端日志，需要查看时加 `--debug`（也支持 `-v` / `--verbose`）：

```bash
python goofish_live.py --debug
```

运行后除了连接 WebSocket 接收/回复消息，还会同时启动一个只监听本地的 HTTP 接口，默认地址 `127.0.0.1:8000`。可通过环境变量修改：

- `XY_API_HOST`：默认 `127.0.0.1`
- `XY_API_PORT`：默认 `8000`
- `XY_COOKIE_FILE`：Cookie 保存文件，默认 `cookies.json`
- `XY_COOKIE_STR`：可选，直接传入 Cookie 字符串，跳过文件读取和 Chrome 登录
- `XY_LOGIN_TIMEOUT`：等待手动登录超时秒数，默认 `300`
- `XY_HEARTBEAT_INTERVAL`：登录态心跳间隔秒数，默认 `600`
- `XY_CHROME_PATH`：可选，指定 Google Chrome 可执行文件路径
- `XY_CHROME_PROFILE`：可选，指定 Chrome 登录专用用户目录
- `XY_SKIP_CHROME_CHECK`：设为 `1` 可跳过启动时的 Chrome 安装检测（仅建议已有有效 Cookie 时使用）
- `XY_NODE_BIN`：可选，指定 Node.js 可执行文件路径
- `XY_LOG_DIR`：聊天原始 JSON 日志目录，默认 `log`
- `XY_WS_URL`：插件网关 WebSocket 地址，默认使用登录接口返回的 `wsUrl`，兜底为 `wss://plugin-gateway.yinghuasuan.com/ws`

收到的每条聊天消息会按天写入 `log/chat_YYYY-MM-DD.jsonl`（每行一条原始 JSON），方便排查和分析聊天记录。

程序会先调用插件网关登录接口，登录成功后连接网关 WebSocket：把收到的文本、图片、订单卡片包装成 `plugin.v1` 的 `xianyu.message` 上报。业务回复、查订单、改价由网关下发 `task.xianyu.*` 任务，程序执行本地 `/api/reply`、`/api/order_detail`、`/api/adjust_price` 后回传 `task.result`。

完整 WS 对接说明见 [WS_API.md](WS_API.md)。

### 本地 HTTP 接口

完整接口文档见 [API.md](API.md)。

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

给指定用户回复文字消息：

```bash
curl -X POST http://127.0.0.1:8000/api/reply \
  -H 'Content-Type: application/json' \
  -d '{"toid": "2202640918079", "cid": "47812870000", "text": "你好，收到！"}'
```

`/api/send` 是 `/api/reply` 的别名，接口参数：

- `toid`：必填，对方用户 ID（也接受 `send_user_id` / `user_id`）
- `cid`：可选，会话 ID。不传时会从最近会话中自动匹配；找不到且传了 `item_id` 时会自动创建会话
- `item_id`：可选，自动创建会话时使用的商品 ID
- `text`：文字内容
- `image_url` / `width` / `height`：发送图片
- `message`：也可直接传消息对象，如 `{"type": "text", "text": "..."}` 或 `{"type": "image", "image_url": "..."}`

发送图片示例：

```bash
curl -X POST http://127.0.0.1:8000/api/send \
  -H 'Content-Type: application/json' \
  -d '{"toid": "2202640918079", "image_url": "https://example.com/a.jpg", "width": 800, "height": 600}'
```

订单改价（金额单位为分）：

```bash
curl -X POST http://127.0.0.1:8000/api/adjust_price \
  -H 'Content-Type: application/json' \
  -d '{"order_id": "5127190491362214211", "modify_fee": "1200", "new_transport_fee": "0"}'
```

也接受 `orderId` / `modifyFee` / `newTransportFee` 命名字段。

Python 直接调用：

```python
from goofish_apis import XianyuApis
from utils.goofish_utils import generate_device_id, trans_cookies

cookies = trans_cookies('your_login_cookie_string_here')
xianyu = XianyuApis(cookies, generate_device_id(cookies['unb']))

result = xianyu.adjust_order_price(
    order_id='5127190491362214211',
    modify_fee='1200',        # 分
    new_transport_fee='0',    # 分
)
print(result)
```

---

## 打包为 exe / macOS 可执行程序

使用 PyInstaller 打包，**必须在目标系统上构建**：Windows 上打 `.exe`，macOS 上打 macOS 可执行文件。

- Windows：双击或运行 `build_windows.bat`
- macOS：运行 `bash build_macos.sh`

构建产物在 `dist/` 目录：

- Windows：`dist\XianYuApis\XianYuApis.exe`（onedir 目录，启动更快）
- macOS：`dist/XianYuApis`

Windows 打包改为 onedir 模式，不再每次启动解压几十 MB 文件，打开速度快很多；分发时把整个 `dist\XianYuApis` 文件夹压缩后给用户即可。

双击 Windows 的 `dist\XianYuApis\XianYuApis.exe` 会打开桌面启动器窗口，而不是命令行：

1. 填写 `XY_API_HOST`（监听 IP）和 `XY_API_PORT`（监听端口）
2. 点击“一键启动”，程序才开始登录并连接 WebSocket / 启动本地接口
3. 窗口内的日志区域会实时显示登录、心跳、收消息和接口运行情况

需要命令行方式运行服务时，仍可使用 `python goofish_live.py`。

程序运行时的 JS 解密/签名依赖 Node.js。构建脚本会自动下载对应平台的 Node.js（Windows 下载 `node.exe`，macOS 按芯片架构下载 `node`）到 `node_bin/` 并打包进产物，构建时需要联网；如果不想联网，也可以手动把对应平台的 Node 可执行文件放到 `node_bin/` 后再构建。

### 常见问题

- `pip install -r requirements.txt` 报 `execjs` 找不到：旧版 requirements 误用了 `execjs` 包名，现已移除；JS 解密改为直接用 Node.js 执行，不再依赖 PyExecJS
- 双击 exe 闪退：先用命令行运行 `dist\XianYuApis\XianYuApis.exe` 查看报错；常见原因是目标机器缺少 Node.js，或者打包前没有把 `node.exe` 放进 `node_bin/`。新版入口在异常退出时会打印错误并等待按回车，便于定位

---

## 项目结构

```
XianYuApis/
├── goofish_live.py      # 主入口：闲鱼 WebSocket 监听、本地回复、插件网关接入
├── goofish_apis.py      # HTTP API 封装（登录、刷新 Token、商品详情、上传媒体）
├── local_api.py         # 本地 HTTP 接口（健康检查、给指定用户回复消息）
├── cookie_auth.py       # Chrome 登录自动获取/保存 Cookie
├── gui.py               # 桌面启动器（一键启动、配置监听地址、实时日志）
├── message/
│   ├── types.py         # 消息类型定义（TextContent / ImageContent / AudioContent）
├── utils/
│   ├── goofish_utils.py # 工具函数（sign 签名、Cookie 处理、消息解密）
│   └── sign.py          # mtop sign 生成（纯 Python，可复用）
├── xianyu.spec          # PyInstaller 打包配置
├── build_windows.bat    # Windows exe 打包脚本
├── build_macos.sh       # macOS 打包脚本
├── static/
│   └── goofish_js_*.js  # 逆向 JS（sign 签名核心算法）
├── requirements.txt
└── Dockerfile
```

---

## 接入 AI 智能体

在 `goofish_live.py` 的 `handle_message` 方法中替换回复逻辑即可：

```python
async def handle_message(self, message, websocket):
    # ... 解析 send_user_id, cid, send_message ...

    # 原始 echo 回复（示例）
    # reply = f'{send_user_name} 说了: {send_message}'

    # 接入 AI 大模型（示例）
    reply = await your_ai_agent(send_message)          # GPT / Claude / Qwen / 本地模型

    await self.send_msg(websocket, cid, send_user_id, make_text(reply))
```

---

## 注意事项

- `goofish_live.py` 是消息收发主入口，所有 AI 回复逻辑在此扩展
- `goofish_apis.py` 包含 HTTP 接口模板，可按需添加其他接口

---

## 额外说明

1. 感谢 Star ⭐ 和 Follow，项目会持续更新
2. 作者联系方式在主页，有问题随时联系
3. 欢迎 PR 和 Issue，也欢迎关注作者其他项目
4. 如果此项目对您有帮助，欢迎请作者喝一杯奶茶 ~~

<div align="center">
  <img src="https://github.com/cv-cat/Spider_XHS/blob/master/author/wx_pay.png" width="380px" alt="微信赞赏码">
  <img src="https://github.com/cv-cat/Spider_XHS/blob/master/author/zfb_pay.jpg" width="380px" alt="支付宝收款码">
</div>

---

## Star 趋势

<a href="https://cvcat.site/star-history/svg?repos=cv-cat/XianYuApis&type=Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://cvcat.site/star-history/svg?repos=cv-cat/XianYuApis&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://cvcat.site/star-history/svg?repos=cv-cat/XianYuApis&type=Date" />
    <img alt="Star History Chart" src="https://cvcat.site/star-history/svg?repos=cv-cat/XianYuApis&type=Date" />
  </picture>
</a>


## 🍔 交流群

如果你对爬虫和 AI Agent 感兴趣，请加作者主页 wx 通过邀请加入群聊

ps: 请加群，人满或者过期 issue | wx 提醒

| group-1 | group-2 | group-3 |
|:--:|:--:|:--:|
| <img width="280" alt="group1" src="https://cvcat.site/assets/group1.jpg" /> | <img width="280" alt="group2" src="https://cvcat.site/assets/group2.jpg" /> | <img width="280" alt="group3" src="https://cvcat.site/assets/group3.jpg" /> |
