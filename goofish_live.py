import base64
import json
import asyncio
import threading
import time
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from loguru import logger
import websockets
from app_paths import default_cookie_file, default_log_dir
from goofish_apis import XianyuApis, UA

from utils.goofish_utils import generate_mid, generate_uuid, trans_cookies, generate_device_id, decrypt, \
    get_session_cookies_str
from message import Message, make_text, make_image
from local_api import LocalApiServer
from ws_client import GatewayClient, GatewayLoginError, gateway_login

try:
    from cookie_auth import CookieStore, check_chrome_installed, fetch_cookies_via_chrome
except ImportError:
    from cookie_auth import CookieStore, find_chrome_binary, fetch_cookies_via_chrome

    def check_chrome_installed():
        path = os.environ.get('XY_CHROME_PATH') or find_chrome_binary()
        if path:
            return True, path, ''
        return False, None, ('未检测到 Google Chrome，请先安装浏览器后再运行。'
                             '下载地址: https://www.google.com/chrome/')


def _first_field(obj, *names):
    if not isinstance(obj, dict):
        return None
    for name in names:
        value = obj.get(name)
        if value is not None:
            return value
    return None


def _ws_connect(uri, headers=None):
    """兼容 websockets 13 与 14+ 的不同请求头参数名。"""
    major = int(str(getattr(websockets, '__version__', '0')).split('.')[0])
    if major >= 14:
        return websockets.connect(uri, additional_headers=headers)
    return websockets.connect(uri, extra_headers=headers)


class XianyuLive:
    def __init__(self, cookies_str=None, cookie_file=None, login_timeout=None,
                 heartbeat_interval=None, gateway_auth=None, account_changed_callback=None,
                 store_id=None, instance_id='', instance_name='', chrome_profile_dir=None,
                 local_api_port=8000, log_dir=None):
        self.base_url = 'wss://wss-goofish.dingtalk.com/'
        self.cookies_str = cookies_str
        self.cookie_file = cookie_file or os.environ.get('XY_COOKIE_FILE') or default_cookie_file()
        self.login_timeout = login_timeout or int(os.environ.get('XY_LOGIN_TIMEOUT', '300'))
        self.heartbeat_interval = heartbeat_interval or int(os.environ.get('XY_HEARTBEAT_INTERVAL', '600'))
        self.cookie_store = CookieStore(self.cookie_file)
        self.gateway_auth = gateway_auth or {}
        self.account_changed_callback = account_changed_callback
        self.store_id = int(store_id or 0)
        self.instance_id = str(instance_id or '')
        self.instance_name = str(instance_name or '')
        self.chrome_profile_dir = str(chrome_profile_dir or '')
        self.local_api_port = int(local_api_port or 8000)
        self.log_dir = log_dir or os.environ.get('XY_LOG_DIR') or default_log_dir()
        if cookies_str:
            self.cookies = trans_cookies(cookies_str)
            self.user_agent = UA
            self.access_token = ''
            saved_device_id = ''
        else:
            store_data = self.cookie_store.load() or {}
            self.cookies = store_data.get('cookies') or {}
            self.user_agent = store_data.get('user_agent') or UA
            self.access_token = store_data.get('access_token') or ''
            saved_device_id = store_data.get('device_id') or ''
        self.myid = self.cookies.get('unb', '')
        self.device_id = saved_device_id or generate_device_id(self.myid)
        self._device_id_from_store = bool(saved_device_id)
        self.xianyu = XianyuApis(self.cookies, self.device_id, user_agent=self.user_agent)
        self._token_failures = 0
        self.ws = None
        self.loop = None
        self._pending = {}
        self._reconnect_event = threading.Event()
        self._stop_event = threading.Event()
        self._relogin_lock = threading.Lock()
        self._seen_structures = set()
        self._seen_protocol_frames = set()
        self._seen_parse_failures = set()
        self._seen_status_events = set()
        self._last_sync_log_at = 0.0
        self._sync_ack_running = False
        self.ws_client = None
        self._ws_task = None
        self.api_server = None
        self.api_thread = None

    def save_cookies(self):
        self.cookie_store.save(self.cookies, self.user_agent, self.access_token, self.device_id)

    def current_chrome_account(self):
        return {
            'nick': unquote(str(self.cookies.get('tracknick') or '')).strip(),
            'userId': str(self.cookies.get('unb') or '').strip(),
        }

    def _notify_chrome_account(self):
        if not callable(self.account_changed_callback):
            return
        try:
            self.account_changed_callback(self.current_chrome_account())
        except Exception:
            pass

    def _save_raw_message(self, message):
        """把聊天消息的原始 JSON 按天写入 log/chat_YYYY-MM-DD.jsonl。"""
        try:
            log_dir = Path(self.log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            file_path = log_dir / f'chat_{time.strftime("%Y-%m-%d")}.jsonl'
            line = json.dumps(message, ensure_ascii=False)
            with open(file_path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
            logger.info(f'聊天日志已写入 {file_path}')
        except Exception as exc:
            logger.warning(f'写入聊天日志失败: {exc}')

    def _save_unparsed_message(self, raw):
        """保留无法解析的本地原始包，避免在界面暴露用户消息正文。"""
        try:
            log_dir = Path(self.log_dir)
            log_dir.mkdir(parents=True, exist_ok=True)
            file_path = log_dir / f'unparsed_{time.strftime("%Y-%m-%d")}.jsonl'
            with open(file_path, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps({'received_at': int(time.time()), 'raw': raw}, ensure_ascii=False) + '\n')
        except Exception as exc:
            logger.warning(f'保存未解析闲鱼消息失败: {exc}')

    def _log_protocol_frame_once(self, message):
        lwp = str(message.get('lwp') or '')
        code = str(message.get('code') or '')
        body = message.get('body')
        shape = f'{lwp}|{code}|{type(body).__name__}'
        if shape in self._seen_protocol_frames:
            return
        self._seen_protocol_frames.add(shape)
        logger.info(f'闲鱼 WS 收到协议帧：lwp={lwp or "-"} code={code or "-"}')

    def _log_parse_failure_once(self, raw, reason=''):
        raw_type = type(raw).__name__
        raw_length = len(raw) if isinstance(raw, (str, bytes, list, dict)) else 0
        detail = str(reason or '未知原因').replace('\n', ' ').strip()[:160]
        shape = f'{raw_type}:{raw_length}:{detail}'
        if shape in self._seen_parse_failures:
            return
        self._seen_parse_failures.add(shape)
        logger.warning(
            f'闲鱼消息解析失败：类型={raw_type} 长度={raw_length} 原因={detail}，原始包已保存到本店日志'
        )

    def _log_status_event_once(self, data):
        first = data.get('1') if isinstance(data, dict) else None
        shape = f'{type(data).__name__}:{type(first).__name__}'
        if shape in self._seen_status_events:
            return
        self._seen_status_events.add(shape)
        logger.info('闲鱼收到状态类推送，非买家消息')

    @staticmethod
    def _is_chat_message(message):
        """只保留真正的私信：'1' 必须是对象，且带消息内容（10）或内容体（6）。"""
        if not isinstance(message, dict):
            return False
        first = message.get('1')
        if not isinstance(first, dict):
            return False
        return '10' in first or '6' in first

    @staticmethod
    def _is_status_event(message):
        """会话/状态类推送：'1' 是数组或字符串时，不作为聊天记录写入。"""
        if not isinstance(message, dict):
            return True
        return isinstance(message.get('1'), (list, str))

    def _is_self_message(self, message):
        """闲鱼会把店铺发出的消息回显到同步流，不能再当作买家消息上报。"""
        first = message.get('1') if isinstance(message, dict) else {}
        reminder = first.get('10') if isinstance(first, dict) else {}
        sender_id = str((reminder or {}).get('senderUserId') or '').split('@')[0]
        current_id = str(self.myid or '').split('@')[0]
        return bool(sender_id and current_id and sender_id == current_id)

    def _log_message_structure(self, data):
        """首次遇到某种消息结构时打印一次，便于定位真实聊天消息的字段。"""
        try:
            if not isinstance(data, dict):
                shape = f'non-dict:{type(data).__name__}'
            else:
                first = data.get('1')
                if isinstance(first, list):
                    shape = '1-list'
                elif isinstance(first, dict):
                    keys = ','.join(sorted(str(k) for k in first.keys())[:12])
                    shape = f'1-dict:{keys}'
                else:
                    shape = f'1-other:{type(first).__name__}'
            if shape not in self._seen_structures:
                self._seen_structures.add(shape)
                top_keys = list(data.keys()) if isinstance(data, dict) else []
                logger.info(f'收到新结构消息: {shape}, 顶层keys: {top_keys}')
        except Exception:
            pass

    def _parse_sync_data(self, raw):
        self._last_parse_error = ''
        if raw is None:
            self._last_parse_error = '消息为空'
            return None
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(raw)
        except Exception as json_exc:
            try:
                return json.loads(decrypt(raw))
            except Exception as decrypt_exc:
                self._last_parse_error = (
                    f'Node 解码失败：{type(decrypt_exc).__name__}: {str(decrypt_exc)[:120]}'
                )
        if isinstance(raw, str):
            try:
                padding = '=' * (-len(raw) % 4)
                return json.loads(base64.b64decode(raw + padding).decode('utf-8'))
            except Exception as base64_exc:
                if not self._last_parse_error:
                    self._last_parse_error = (
                        f'JSON/Base64 解析失败：{type(json_exc).__name__}/{type(base64_exc).__name__}'
                    )
        return None

    @staticmethod
    def _simplify_chat_message(message):
        """精简聊天消息：1 文本 / 2 图片 / 3 已拍下 / 4 已付款 / 5 退款。"""
        first = message.get('1') or {}
        content = (first.get('6') or {}).get('3') or {}
        reminder = first.get('10') or {}
        try:
            content_type = int(content.get('4') or 0)
        except (TypeError, ValueError):
            content_type = 0

        cid = str(first.get('2') or '').split('@')[0]
        sender_user_id = str(reminder.get('senderUserId') or '')
        reminder_title = str(reminder.get('reminderTitle') or '')
        time_str = str(first.get('5') or '')
        reminder_url = str(reminder.get('reminderUrl') or '')
        item_id = ''
        try:
            query = parse_qs(urlparse(reminder_url).query)
            item_id = (query.get('itemId') or [''])[0]
        except Exception:
            pass

        inner_data = None
        inner = content.get('5')
        if inner:
            try:
                inner_data = json.loads(inner) if isinstance(inner, str) else inner
            except Exception:
                inner_data = None

        # 订单卡片消息：原 contentType=26，映射为 3 已拍下 / 4 已付款 / 5 退款
        if content_type == 26:
            order_id = ''
            try:
                ext_json = reminder.get('extJson')
                if isinstance(ext_json, str):
                    ext_json = json.loads(ext_json)
                update_key = str((ext_json or {}).get('updateKey') or '')
                parts = update_key.split(':')
                if len(parts) >= 2 and parts[1]:
                    order_id = parts[1]
            except Exception:
                pass
            if not order_id and inner_data:
                try:
                    main = ((inner_data.get('dxCard') or {}).get('item') or {}).get('main') or {}
                    targets = [str(main.get('targetUrl') or '')]
                    button = (main.get('exContent') or {}).get('button') or {}
                    if button.get('targetUrl'):
                        targets.append(str(button.get('targetUrl') or ''))
                    for target in targets:
                        target_query = parse_qs(urlparse(target).query)
                        for key in ('bizOrderId', 'orderId', 'id'):
                            if target_query.get(key):
                                order_id = target_query[key][0]
                                break
                        if order_id:
                            break
                except Exception:
                    pass

            order_type = 3
            card_text = str(reminder.get('reminderContent') or '')
            if '已付款' in card_text or '等待你发货' in card_text:
                order_type = 4
            elif '退款' in card_text:
                order_type = 5
            return {
                'contentType': order_type,
                'cid': cid,
                'senderUserId': sender_user_id,
                'reminderTitle': reminder_title,
                'orderId': order_id,
                'time': time_str,
                'itemId': item_id,
            }

        text = ''
        url = ''
        width = 0
        height = 0
        if inner_data:
            if content_type == 2:
                pics = ((inner_data.get('image') or {}).get('pics')) or []
                if pics:
                    url = str(pics[0].get('url') or '')
                    try:
                        width = int(pics[0].get('width') or 0)
                    except (TypeError, ValueError):
                        width = 0
                    try:
                        height = int(pics[0].get('height') or 0)
                    except (TypeError, ValueError):
                        height = 0
            elif content_type == 1:
                text = str(((inner_data.get('text') or {}).get('text')) or '')

        return {
            'contentType': content_type,
            'cid': cid,
            'senderUserId': sender_user_id,
            'reminderTitle': reminder_title,
            'text': text,
            'url': url,
            'width': width,
            'height': height,
            'time': time_str,
            'itemId': item_id,
            'clientIp': str(reminder.get('clientIp') or ''),
            '_appVersion': str(reminder.get('_appVersion') or ''),
            '_platform': str(reminder.get('_platform') or ''),
        }

    def check_login(self):
        """检查当前 cookie 是否有效，同时起到刷新登录态的作用。"""
        if not self.cookies or not self.cookies.get('_m_h5_tk'):
            return False
        try:
            result = self.xianyu.refresh_token()
        except Exception as exc:
            logger.warning(f'登录态检查失败: {exc}')
            return False
        ret = result.get('ret') or []
        ret_text = ' '.join(str(item) for item in ret)
        if 'RGV587_ERROR' in ret_text or 'SM::' in ret_text:
            logger.warning('刷新登录态被风控拦截，登录态仍视为有效')
            return True
        return any('SUCCESS' in str(item) for item in ret)

    def ensure_login(self, force=False):
        """优先使用本地保存的 cookie，失效时打开 Chrome 手动登录并自动保存。"""
        if not force and self.cookies and self.access_token and self._device_id_from_store and self.check_login():
            logger.info('使用本地 cookie 登录成功')
            self._notify_chrome_account()
            return True
        installed, _, chrome_hint = check_chrome_installed()
        if not installed:
            logger.error(chrome_hint)
            return False
        logger.info('需要登录闲鱼卖家中心，正在打开 Chrome')
        try:
            cookies_str, user_agent, access_token, device_id = fetch_cookies_via_chrome(
                timeout=self.login_timeout,
                profile_dir=self.chrome_profile_dir or None,
            )
        except Exception as exc:
            logger.exception(f'Chrome 登录失败: {exc}')
            return False
        self.cookies = trans_cookies(cookies_str)
        self.user_agent = user_agent or self.user_agent
        self.access_token = access_token
        self.device_id = device_id or self.device_id
        self._device_id_from_store = True
        required = ('unb', '_m_h5_tk', 'cookie2')
        missing = [name for name in required if not self.cookies.get(name)]
        if missing:
            logger.error(f'获取 cookie 不完整，缺少: {missing}')
            return False
        self.save_cookies()
        self.myid = self.cookies['unb']
        self.xianyu = XianyuApis(self.cookies, self.device_id, user_agent=self.user_agent)
        logger.info(f"登录成功: {self.cookies.get('tracknick')}")
        self._notify_chrome_account()
        return True

    def relogin(self):
        """登录态失效时重新走 Chrome 登录流程，成功后触发 WebSocket 重连。"""
        with self._relogin_lock:
            logger.warning('登录态已失效，开始重新登录')
            if not self.ensure_login(force=True):
                logger.error('重新登录失败，等待下次心跳重试')
                return False
            logger.info('重新登录成功，触发 WebSocket 重连')
            self._reconnect_event.set()
            return True

    async def list_all_conversations(self, cid):
        headers = {
            "Cookie": get_session_cookies_str(self.xianyu.session),
            "Host": "wss-goofish.dingtalk.com",
            "Connection": "Upgrade",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "Origin": "https://www.goofish.com",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        async with _ws_connect(self.base_url, headers) as websocket:
            await self.init(websocket)
            send_mid = generate_mid()
            msg = {
                "lwp": "/r/MessageManager/listUserMessages",
                "headers": {
                    "mid": send_mid
                },
                "body": [
                    f"{cid}@goofish",
                    False,
                    9007199254740991,
                    20,
                    False
                ]
            }
            user_message_models = []
            async for message in websocket:
                try:
                    message = json.loads(message)
                    ack = {
                        "code": 200,
                        "headers": {
                            "mid": message["headers"]["mid"] if "mid" in message["headers"] else generate_mid(),
                            "sid": message["headers"]["sid"] if "sid" in message["headers"] else '',
                        }
                    }
                    if 'app-key' in message["headers"]:
                        ack["headers"]["app-key"] = message["headers"]["app-key"]
                    if 'ua' in message["headers"]:
                        ack["headers"]["ua"] = message["headers"]["ua"]
                    if 'dt' in message["headers"]:
                        ack["headers"]["dt"] = message["headers"]["dt"]
                    await websocket.send(json.dumps(ack))
                except Exception as e:
                    pass
                try:
                    if 'lwp' in message and message['lwp'] == "/s/vulcan":
                        await websocket.send(json.dumps(msg))
                    recv_mid = message["headers"]["mid"] if "mid" in message["headers"] else ''
                    if recv_mid == send_mid:
                        logger.info(f"user history message: {message}")
                        has_more = message["body"]["hasMore"] == 1
                        next_cursor = message["body"]["nextCursor"]
                        for user_message in message["body"]["userMessageModels"]:
                            send_user_name = user_message["message"]["extension"]["reminderTitle"]
                            send_user_id = user_message["message"]["extension"]["senderUserId"]
                            send_message_base64 = user_message["message"]["content"]["custom"]["data"]
                            send_message_json = json.loads(base64.b64decode(send_message_base64).decode('utf-8'))
                            user_message_models.insert(0, {
                                "send_user_id": send_user_id,
                                "send_user_name": send_user_name,
                                "message": send_message_json
                            })
                        if has_more:
                            logger.info(f"has more history messages, next cursor: {next_cursor}")
                            send_mid = generate_mid()
                            msg["headers"]["mid"] = send_mid
                            msg["body"][2] = next_cursor
                            await websocket.send(json.dumps(msg))
                        else:
                            return user_message_models
                except Exception as e:
                    return user_message_models

    async def create_chat(self, ws, toid, item_id='891198795482'):
        msg = {
            "lwp": "/r/SingleChatConversation/create",
            "headers": {
                "mid": generate_mid()
            },
            "body": [
                {
                    "pairFirst": f"{toid}@goofish",
                    "pairSecond": f"{self.myid}@goofish",
                    "bizType": "1",
                    "extension": {
                        "itemId": item_id
                    },
                    "ctx": {
                        "appVersion": "1.0",
                        "platform": "web"
                    }
                }
            ]
        }
        await ws.send(json.dumps(msg))

    async def send_msg(self, ws, cid, toid, message: Message):
        msg_type = message["type"]
        msg = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {
                "mid": generate_mid()
            },
            "body": [
                {
                    "uuid": generate_uuid(),
                    "cid": f"{cid}@goofish",
                    "conversationType": 1,
                    "content": {
                        "contentType": 101,
                        "custom": {
                            "type": None,
                            "data": None
                        }
                    },
                    "redPointPolicy": 0,
                    "extension": {
                        "extJson": "{}"
                    },
                    "ctx": {
                        "appVersion": "1.0",
                        "platform": "web"
                    },
                    "mtags": {},
                    "msgReadStatusSetting": 1
                },
                {
                    "actualReceivers": [
                        f"{toid}@goofish",
                        f"{self.myid}@goofish"
                    ]
                }
            ]
        }
        if msg_type == "text":
            payload = {
                "contentType": 1,
                "text": {
                    "text": message["text"]
                }
            }
            text_base64 = str(base64.b64encode(json.dumps(payload).encode('utf-8')), 'utf-8')
            msg["body"][0]["content"]["custom"]["type"] = 1
            msg["body"][0]["content"]["custom"]["data"] = text_base64
        elif msg_type == "image":
            payload = {
                "contentType": 2,
                "image": {
                    "pics": [
                        {
                            "type": 0,
                            "url": message["image_url"],
                            "width": message["width"],
                            "height": message["height"]
                        }
                    ]
                }
            }
            image_base64 = str(base64.b64encode(json.dumps(payload).encode('utf-8')), 'utf-8')
            msg["body"][0]["content"]["custom"]["type"] = 2
            msg["body"][0]["content"]["custom"]["data"] = image_base64
        elif msg_type == "audio":
            # TODO: handle audio message
            logger.error(f"不支持的消息类型: {msg_type}")
            return
        else:
            logger.error(f"不支持的消息类型: {msg_type}")
            return
        await ws.send(json.dumps(msg))

    async def _request(self, ws, lwp, body, timeout=10.0):
        """发送一次 RPC，并等待携带相同 mid 的服务端响应。"""
        mid = generate_mid()
        loop = self.loop or asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[mid] = future
        msg = {
            "lwp": lwp,
            "headers": {"mid": mid},
            "body": body
        }
        try:
            await ws.send(json.dumps(msg))
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(mid, None)

    async def _find_conversation_id(self, ws, toid, max_count=200):
        """从最近会话里找到与 toid 的单聊 cid；找不到返回 None。"""
        target = str(toid).split('@')[0]
        # 兼容不同服务端对 timestamp 的解释：0 表示全量起点，当前时间表示最新窗口
        for timestamp in (0, int(time.time() * 1000)):
            response = await self._request(ws, '/r/Conversation/listNewest', [timestamp, max_count], timeout=10.0)
            body = response.get('body', [])
            if isinstance(body, dict):
                body = _first_field(body, 'userConvModels', 'userConvs', '1') or []
            for conv in body or []:
                single = _first_field(conv, 'singleChatUserConversation', '2')
                chat = _first_field(single, 'singleChatConversation', '1')
                cid = _first_field(chat, 'cid', '1')
                pair_first = _first_field(chat, 'pairFirst', '2') or ''
                pair_second = _first_field(chat, 'pairSecond', '3') or ''
                if str(pair_first).split('@')[0] == target or str(pair_second).split('@')[0] == target:
                    return str(cid).split('@')[0] if cid else None
        return None

    async def _create_chat_and_wait(self, ws, toid, item_id):
        """创建单聊会话，等待响应并返回 cid；失败返回 None。"""
        response = await self._request(ws, '/r/SingleChatConversation/create', [
            {
                "pairFirst": f"{toid}@goofish",
                "pairSecond": f"{self.myid}@goofish",
                "bizType": "1",
                "extension": {
                    "itemId": str(item_id)
                },
                "ctx": {
                    "appVersion": "1.0",
                    "platform": "web"
                }
            }
        ], timeout=10.0)
        single = response.get('body')
        if not isinstance(single, dict):
            return None
        single = _first_field(single, 'singleChatUserConversation', '2') or single
        chat = _first_field(single, 'singleChatConversation', '1') or single
        cid = _first_field(chat, 'cid', '1')
        return str(cid).split('@')[0] if cid else None

    @staticmethod
    def _build_message(payload):
        message = payload.get('message')
        if isinstance(message, dict):
            msg_type = message.get('type')
            if msg_type == 'text':
                return make_text(str(message.get('text') or ''))
            if msg_type == 'image':
                return make_image(
                    str(message.get('image_url') or ''),
                    int(message.get('width') or 0),
                    int(message.get('height') or 0),
                )
            raise ValueError(f'不支持的 message.type: {msg_type}')
        if payload.get('text') is not None:
            return make_text(str(payload['text']))
        if payload.get('image_url'):
            return make_image(
                str(payload['image_url']),
                int(payload.get('width') or 0),
                int(payload.get('height') or 0),
            )
        raise ValueError('请提供 text、image_url 或 message 对象')

    async def _send_reply(self, payload):
        if self.ws is None:
            raise RuntimeError('WebSocket 未连接')
        toid = str(payload.get('toid') or payload.get('send_user_id') or payload.get('user_id') or '').strip().split('@')[0]
        if not toid:
            raise ValueError('缺少 toid')
        cid = str(payload.get('cid') or '').strip().split('@')[0]
        if not cid:
            cid = await self._find_conversation_id(self.ws, toid)
        if not cid:
            item_id = payload.get('item_id')
            if item_id:
                cid = await self._create_chat_and_wait(self.ws, toid, item_id)
            else:
                raise LookupError(f'未找到与用户 {toid} 的会话，请提供 cid，或提供 item_id 自动创建会话')
        if not cid:
            raise RuntimeError('创建会话失败，无法定位 cid')
        message = self._build_message(payload)
        await self.send_msg(self.ws, cid, toid, message)
        return {'ok': True, 'cid': cid, 'toid': toid, 'type': message['type']}

    def send_reply(self, payload):
        """本地 HTTP 接口调用的同步入口，把请求投递到主事件循环执行。"""
        if self.loop is None or self.ws is None:
            raise RuntimeError('WebSocket 未连接')
        future = asyncio.run_coroutine_threadsafe(self._send_reply(payload), self.loop)
        return future.result(timeout=30)

    def adjust_order_price(self, payload):
        """本地 HTTP 接口调用的订单改价入口。"""
        def pick(*names):
            for name in names:
                if payload.get(name) is not None:
                    return payload[name]
            return None

        order_id = pick('order_id', 'orderId')
        modify_fee = pick('modify_fee', 'modifyFee')
        new_transport_fee = pick('new_transport_fee', 'newTransportFee')
        if order_id is None or modify_fee is None or new_transport_fee is None:
            raise ValueError('缺少 order_id、modify_fee 或 new_transport_fee')
        return self.xianyu.adjust_order_price(str(order_id), str(modify_fee), str(new_transport_fee))

    def get_order_detail(self, payload):
        """本地 HTTP 接口调用的订单查询入口，返回单个订单详情。"""
        order_id = payload.get('orderId')
        if order_id is None:
            order_id = payload.get('order_id')
        if order_id is None:
            raise ValueError('缺少 orderId')
        result = self.xianyu.get_order_detail(str(order_id))
        ret = result.get('ret') or []
        if ret and not any('SUCCESS' in str(item) for item in ret):
            raise RuntimeError(f'查询订单失败: {ret}')
        items = ((result.get('data') or {}).get('module') or {}).get('items') or []
        target = str(order_id)
        order = next(
            (item for item in items if str((item.get('commonData') or {}).get('orderId')) == target),
            None,
        )
        if order is None and len(items) == 1:
            order = items[0]
        if order is None:
            raise LookupError(f'未找到订单 {target}')
        return order

    def start_local_api(self, host='127.0.0.1', port=8000):
        if self.api_server is not None:
            return self.api_server
        self.api_server = LocalApiServer((host, port), self)
        self.api_thread = threading.Thread(
            target=self.api_server.serve_forever,
            name='xianyu-local-api',
            daemon=True,
        )
        self.api_thread.start()
        logger.info(f'本地接口已启动: http://{host}:{port}')
        return self.api_server

    def stop_local_api(self):
        if self.api_server is not None:
            self.api_server.shutdown()
            self.api_server.server_close()
            self.api_server = None

    async def init(self, ws):
        token = self.access_token or ''
        if not token:
            data = self.xianyu.get_token()
            token = data['data']['accessToken'] if 'data' in data and 'accessToken' in data['data'] else ''
            self.access_token = token
        if not token:
            logger.error(f'获取token失败: {json.dumps(data, ensure_ascii=False)}')
            raise RuntimeError('获取token失败')
        msg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": "444e9908a51d1cb236a27862abc769c9",
                "token": token,
                "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5",
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self.device_id,
                "mid": generate_mid()
            }
        }
        await ws.send(json.dumps(msg))
        logger.info('init')

    async def _sync_ack_flow(self, websocket):
        """按 SDK 流程：/s/sync 后先 getState 再 ackDiff，保持长连接。"""
        if self._sync_ack_running:
            return
        self._sync_ack_running = True
        try:
            state_resp = await self._request(websocket, '/r/SyncStatus/getState', [{'topic': 'sync'}], timeout=5)
            body = state_resp.get('body')
            if body:
                await self._request(websocket, '/r/SyncStatus/ackDiff', [body], timeout=5)
                logger.info(f'sync ack 完成: {body}')
        except Exception as exc:
            logger.warning(f'sync ack 失败: {exc}')
        finally:
            self._sync_ack_running = False

    async def heart_beat(self, ws):
        while True:
            msg = {
                "lwp": "/!",
                "headers": {
                    "mid": generate_mid()
                 }
            }
            await ws.send(json.dumps(msg))
            await asyncio.sleep(15)

    def user_alive(self):
        while not self._stop_event.is_set():
            if self._stop_event.wait(self.heartbeat_interval):
                break
            if not self.check_login():
                self.relogin()

    async def main(self):
        threading.Thread(target=self.user_alive, daemon=True, name='xianyu-heartbeat').start()
        self.loop = asyncio.get_running_loop()
        host = os.environ.get('XY_API_HOST', '127.0.0.1')
        port = self.local_api_port
        try:
            self.start_local_api(host, port)
        except OSError as exc:
            logger.error(f'本地接口启动失败: {exc}')

        auth = self.gateway_auth or {}
        token = auth.get('accessToken') or os.environ.get('XY_GATEWAY_TOKEN') or ''
        ws_url = auth.get('wsUrl') or os.environ.get('XY_WS_URL') or None
        self.ws_client = GatewayClient(
            token=token,
            ws_url=ws_url,
            store_id=self.store_id or None,
            reply_url=f'http://{host}:{port}/api/reply',
            stop_event=self._stop_event,
        )
        self._ws_task = asyncio.create_task(self.ws_client.run())

        while not self._stop_event.is_set():
            headers = {
                "Cookie": get_session_cookies_str(self.xianyu.session),
                "Host": "wss-goofish.dingtalk.com",
                "Connection": "Upgrade",
                "Pragma": "no-cache",
                "Cache-Control": "no-cache",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                "Origin": "https://www.goofish.com",
                "Accept-Encoding": "gzip, deflate, br, zstd",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            try:
                async with _ws_connect(self.base_url, headers) as websocket:
                    self.ws = websocket
                    logger.info('WebSocket 已连接，开始注册')
                    await self.init(websocket)
                    self._token_failures = 0
                    heartbeat_task = asyncio.create_task(self.heart_beat(websocket))
                    try:
                        async for message in websocket:
                            # logger.info(f"message: {message}")
                            message = json.loads(message)
                            msg_lwp = message.get('lwp', '')
                            msg_code = message.get('code')
                            if msg_lwp or (msg_code is not None and msg_code != 200):
                                body_summary = str(message.get('body'))[:300]
                                logger.info(f'WS 服务端消息: lwp={msg_lwp} code={msg_code} body={body_summary}')
                            if msg_lwp == '/s/sync':
                                asyncio.create_task(self._sync_ack_flow(websocket))
                            ack = {
                                "code": 200,
                                "headers": {
                                    "mid": message["headers"]["mid"] if "mid" in message["headers"] else generate_mid(),
                                    "sid": message["headers"]["sid"] if "sid" in message["headers"] else '',
                                }
                            }
                            if 'app-key' in message["headers"]:
                                ack["headers"]["app-key"] = message["headers"]["app-key"]
                            if 'ua' in message["headers"]:
                                ack["headers"]["ua"] = message["headers"]["ua"]
                            if 'dt' in message["headers"]:
                                ack["headers"]["dt"] = message["headers"]["dt"]
                            await websocket.send(json.dumps(ack))

                            if self._reconnect_event.is_set():
                                logger.info('收到重连信号，正在重连')
                                break

                            if self._stop_event.is_set():
                                logger.info('收到停止信号，正在退出')
                                break

                            recv_mid = message.get('headers', {}).get('mid', '')
                            future = self._pending.get(recv_mid) if recv_mid else None
                            if future is not None and not future.done():
                                future.set_result(message)
                                continue

                            await self.handle_message(message, websocket)
                    finally:
                        heartbeat_task.cancel()
                        try:
                            await heartbeat_task
                        except BaseException:
                            pass
            except Exception as exc:
                logger.error(f'WebSocket 连接/初始化失败: {exc}')
                if isinstance(exc, RuntimeError) and 'token' in str(exc):
                    self._token_failures += 1
                    if self._token_failures >= 3:
                        logger.warning(f'连续 {self._token_failures} 次获取 token 失败，300 秒后再试')
                        await asyncio.sleep(300)
                    elif not self.relogin():
                        logger.warning('重新登录失败，60 秒后重试')
                        await asyncio.sleep(60)
            finally:
                self.ws = None
                self._reconnect_event.clear()
            if self._stop_event.is_set():
                break
            await asyncio.sleep(3)

        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except BaseException:
                pass

    async def handle_message(self, message, websocket):
        try:
            package = message["body"]["syncPushPackage"]["data"]
        except Exception:
            self._log_protocol_frame_once(message)
            return
        if not isinstance(package, list):
            logger.warning('闲鱼同步推送格式异常，未得到消息列表')
            return

        if time.time() - self._last_sync_log_at >= 30:
            self._last_sync_log_at = time.time()
            logger.info(f'闲鱼同步推送到达：{len(package)} 条')

        for item in package:
            raw = item.get("data") if isinstance(item, dict) else item
            parsed = self._parse_sync_data(raw)
            if parsed is None:
                self._save_unparsed_message(raw)
                self._log_parse_failure_once(raw, self._last_parse_error)
                continue
            self._log_message_structure(parsed)
            if self._is_status_event(parsed):
                self._log_status_event_once(parsed)
                continue
            if self._is_chat_message(parsed):
                if self._is_self_message(parsed):
                    continue
                simplified = self._simplify_chat_message(parsed)
                self._save_raw_message(simplified)
                logger.info(f'收到买家消息：{self._describe_simplified_message(simplified)}')
                if simplified.get('contentType') in (1, 2, 3, 4, 5) and self.ws_client is not None:
                    await self.ws_client.send(simplified)
            else:
                self._save_raw_message(parsed)
                logger.info('闲鱼收到未识别消息，已保留结构供排查')
            if not self._is_chat_message(parsed):
                continue

            try:
                send_user_name = parsed["1"]["10"]["reminderTitle"]
                send_user_id = parsed["1"]["10"]["senderUserId"]
                send_message = parsed["1"]["10"]["reminderContent"]
                logger.info(f"user: {send_user_name}, 发送给我的信息 message: {send_message}")

                cid = parsed["1"]["2"]
                cid = cid.split('@')[0]

                # 回复文字
                # reply = f'Hello, {send_user_name}! I am a robot. I am not available now. I will reply to you later.'
                # reply = f'{send_user_name} 说了: {send_message}'
                # await self.send_msg(websocket, cid, send_user_id, make_text(reply))

                # 回复图片
                # res_json = self.xianyu.upload_media(r"D:\Desktop\1.png")
                # image_object = res_json["object"]
                # width, height = map(int, image_object["pix"].split('x'))
                # await self.send_msg(websocket, cid, send_user_id, make_image(image_object["url"], width, height))
            except Exception:
                pass

    @staticmethod
    def _short_log_text(value, limit=80):
        text = str(value or '').replace('\n', ' ').strip()
        return text if len(text) <= limit else text[:limit] + '...'

    def _describe_simplified_message(self, data):
        content_type = int(data.get('contentType') or 0)
        buyer = data.get('reminderTitle') or data.get('senderUserId') or ''
        if content_type == 1:
            return f'文字 买家={buyer} 内容={self._short_log_text(data.get("text"))}'
        if content_type == 2:
            return f'图片 买家={buyer} 地址={self._short_log_text(data.get("url"), 60)}'
        if content_type == 3:
            return f'创建订单 买家={buyer} 订单={data.get("orderId", "")}'
        if content_type == 4:
            return f'付款订单 买家={buyer} 订单={data.get("orderId", "")}'
        if content_type == 5:
            return f'退款订单 买家={buyer} 订单={data.get("orderId", "")}'
        return f'类型={content_type} 买家={buyer}'


def configure_terminal_logging(enabled):
    """终端日志总开关：默认关闭，传入 --debug 时打开。"""
    logger.remove()
    if enabled:
        logger.add(
            sys.stderr,
            level='INFO',
            format='{time:HH:mm:ss} | {level} | {message}',
        )


def _resolve_cli_gateway_auth():
    """命令行启动时解析网关登录信息：token > 账号密码环境变量 > 手动输入。"""
    token = os.environ.get('XY_GATEWAY_TOKEN')
    if token:
        return {
            'accessToken': token,
            'wsUrl': os.environ.get('XY_WS_URL') or '',
            'scope': {'storeIds': []},
        }
    username = os.environ.get('XY_GATEWAY_USERNAME') or ''
    password = os.environ.get('XY_GATEWAY_PASSWORD') or ''
    if not username:
        username = input('网关账号: ').strip()
    if not password:
        import getpass
        password = getpass.getpass('网关密码: ')
    if not username or not password:
        logger.error('缺少网关账号或密码')
        raise SystemExit(1)
    try:
        return gateway_login(username, password)
    except GatewayLoginError as exc:
        logger.error(f'网关登录失败：{exc}')
        raise SystemExit(1)


def main_entry():
    debug = any(arg in ('--debug', '-v', '--verbose') for arg in sys.argv[1:])
    configure_terminal_logging(debug)
    try:
        if not os.environ.get('XY_SKIP_CHROME_CHECK'):
            installed, _, chrome_hint = check_chrome_installed()
            if not installed:
                logger.error(chrome_hint)
                raise SystemExit(1)
        xianyu_live = XianyuLive(
            cookies_str=os.environ.get('XY_COOKIE_STR'),
            cookie_file=os.environ.get('XY_COOKIE_FILE', 'cookies.json'),
            gateway_auth=_resolve_cli_gateway_auth(),
        )
        if not xianyu_live.ensure_login():
            logger.error('登录失败，程序退出')
            raise SystemExit(1)
        asyncio.run(xianyu_live.main())
    except SystemExit:
        raise
    except Exception:
        logger.exception('程序异常退出')
        raise
    finally:
        if getattr(sys, 'frozen', False) and os.name == 'nt':
            try:
                input('按回车键退出...')
            except EOFError:
                pass


if __name__ == '__main__':
    main_entry()
