import asyncio
import base64
import json
import os
import threading
import time
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path

import requests
from loguru import logger
from PIL import Image
import websockets

GATEWAY_LOGIN_URL = 'https://plugin-gateway.yinghuasuan.com/api/v1/client/login'
GATEWAY_XIANYU_STORE_BIND_URL = 'https://plugin-gateway.yinghuasuan.com/api/v1/client/xianyu/store/bind'
DEFAULT_WS_URL = 'wss://plugin-gateway.yinghuasuan.com/ws'
LOGIN_CONFIG_DIR = os.path.join(os.path.expanduser('~'), '.xianyu')
LOGIN_CONFIG_FILE = os.path.join(LOGIN_CONFIG_DIR, 'login.json')


class GatewayLoginError(Exception):
    pass


class GatewayTokenError(GatewayLoginError):
    pass


def _gateway_token_expires_at(token):
    """Read JWT expiry locally only for refresh scheduling; the gateway still verifies it."""
    try:
        payload = str(token or '').split('.')[1]
        payload += '=' * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode('ascii')).decode('utf-8'))
        return int(claims.get('exp') or 0)
    except (IndexError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return 0


def gateway_token_expires_soon(token, leeway=300):
    expires_at = _gateway_token_expires_at(token)
    return bool(expires_at and expires_at <= time.time() + max(0, int(leeway)))


def load_login_config():
    try:
        with open(LOGIN_CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_login_config(username='', password='', device_id=''):
    try:
        os.makedirs(LOGIN_CONFIG_DIR, exist_ok=True)
        data = load_login_config()
        if username:
            data['username'] = username
        if password:
            data['password'] = password
        if device_id:
            data['deviceId'] = device_id
        with open(LOGIN_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_or_create_device_id():
    config = load_login_config()
    device_id = config.get('deviceId') or ''
    if not device_id:
        device_id = 'fish-client-' + uuid.uuid4().hex[:12]
        save_login_config(
            username=config.get('username', ''),
            password=config.get('password', ''),
            device_id=device_id,
        )
    return device_id


def gateway_login(username, password, device_id=None):
    """调用网关登录接口，返回 accessToken / wsUrl / scope 等。"""
    device_id = device_id or get_or_create_device_id()
    payload = {
        'username': username,
        'password': password,
        'deviceId': device_id,
        'clientType': 'store-plugin',
        'businessType': 'xianyu',
        'platform': 'fish',
    }
    try:
        response = requests.post(GATEWAY_LOGIN_URL, json=payload, timeout=15)
        data = response.json()
    except Exception as exc:
        raise GatewayLoginError(f'登录请求失败：{exc}') from exc
    if data.get('code') != 200 or not data.get('data'):
        raise GatewayLoginError(str(data.get('msg') or data))
    save_login_config(username=username, password=password, device_id=device_id)
    return data['data']


def gateway_bind_xianyu_store(access_token, store_id, platform_shop_id):
    try:
        response = requests.post(
            GATEWAY_XIANYU_STORE_BIND_URL,
            json={
                'storeId': int(store_id),
                'platformShopId': str(platform_shop_id or '').strip(),
            },
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=15,
        )
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f'闲鱼店铺绑定请求失败：{exc}') from exc
    if response.status_code == 401 or data.get('code') == 401:
        raise GatewayTokenError(str(data.get('msg') or '登录已过期，请重新登录'))
    if data.get('code') != 200 or not data.get('data'):
        raise RuntimeError(str(data.get('msg') or data))
    return data['data']


class GatewayAuthManager:
    """Refresh one shared gateway login for all store instances in this client."""

    def __init__(self, auth=None):
        self.auth = auth if isinstance(auth, dict) else {}
        self._lock = threading.Lock()

    def refresh(self, stale_token='', force=False):
        with self._lock:
            current_token = str(self.auth.get('accessToken') or '')
            if (stale_token and current_token and current_token != stale_token
                    and not gateway_token_expires_soon(current_token)):
                return dict(self.auth)
            if not force and current_token and not gateway_token_expires_soon(current_token):
                return dict(self.auth)

            config = load_login_config()
            username = str(config.get('username') or '').strip()
            password = str(config.get('password') or '')
            if not username or not password:
                raise GatewayLoginError('未保存子账号或密码，无法自动刷新网关登录态')

            refreshed = gateway_login(
                username,
                password,
                device_id=str(config.get('deviceId') or '') or None,
            )
            self.auth.update(refreshed)
            return dict(self.auth)


class GatewayClient:
    """连接 yhs-plugin-gateway：登录后 bind、心跳、业务消息转发与回复。"""

    BUYER_CHAT_MAX_AGE_SECONDS = 600

    def __init__(self, token, ws_url=None, store_id=None, instance_id='', chrome_logged_in=None,
                 reply_url='http://127.0.0.1:8000/api/reply', stop_event=None, outbox=None,
                 auth_manager=None):
        self.token = token or ''
        self.ws_url = ws_url or os.environ.get('XY_WS_URL') or DEFAULT_WS_URL
        self.store_id = store_id
        self.instance_id = str(instance_id or '')
        self.chrome_logged_in = chrome_logged_in
        self.reply_url = reply_url
        self.api_base_url = reply_url.rsplit('/', 2)[0]
        self.stop_event = stop_event
        self.outbox = outbox
        self.auth_manager = auth_manager
        self.ws = None
        self._heartbeat_task = None
        self._outbox_task = None
        self._outbox_wakeup = asyncio.Event()
        self._pending_event_acks = {}
        self._pending_claims = {}
        self._task_workers = set()
        self.executor_generation = 0
        self._next_auth_refresh_at = 0

    async def run(self):
        reconnect_delay = 5
        while self.stop_event is None or not self.stop_event.is_set():
            retry_delay = reconnect_delay
            try:
                if gateway_token_expires_soon(self.token):
                    await self._refresh_gateway_auth(force=False)
                async with websockets.connect(self.ws_url) as ws:
                    self.ws = ws
                    await self._bind(ws)
                    reconnect_delay = 5
                    self._heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                    self._outbox_task = asyncio.create_task(self._run_outbox(ws))
                    try:
                        async for message in ws:
                            await self._handle_message(message)
                    finally:
                        await self._cancel_task_workers()
                        if self._outbox_task is not None:
                            self._outbox_task.cancel()
                            await asyncio.gather(self._outbox_task, return_exceptions=True)
                            self._outbox_task = None
                        if self._heartbeat_task is not None:
                            self._heartbeat_task.cancel()
                            self._heartbeat_task = None
                        self._fail_pending_event_acks(ConnectionError('插件网关连接已断开'))
            except GatewayLoginError as exc:
                try:
                    await self._refresh_gateway_auth(force=True)
                    logger.info('插件网关登录态已自动刷新，正在重新连接')
                    reconnect_delay = 5
                    retry_delay = 0
                except Exception as refresh_exc:
                    logger.warning(f'插件网关登录态自动刷新失败：{refresh_exc}')
                    logger.warning(f'插件网关连接异常：{exc}；{retry_delay} 秒后重试')
                    reconnect_delay = min(reconnect_delay * 2, 60)
            except Exception as exc:
                logger.warning(f'插件网关连接异常：{exc}；{retry_delay} 秒后重试')
                reconnect_delay = min(reconnect_delay * 2, 60)
            finally:
                self.ws = None
            if self.stop_event is not None and self.stop_event.is_set():
                break
            await asyncio.sleep(retry_delay)

    async def _bind(self, ws):
        bind_id = 'bind_' + uuid.uuid4().hex[:12]
        await ws.send(json.dumps({
            'version': 'plugin.v1',
            'type': 'client.bind',
            'id': bind_id,
            'payload': self._presence_payload({'token': self.token}),
        }))
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if message.get('type') == 'client.bind.ack':
                payload = message.get('payload') or {}
                if payload.get('success'):
                    executor = payload.get('executor') or {}
                    self.executor_generation = int(executor.get('generation') or 0)
                    logger.info('网关绑定成功')
                    if self.outbox is not None:
                        pending_count = await asyncio.to_thread(self.outbox.count, 'pending')
                        if pending_count:
                            logger.info(f'检测到 {pending_count} 条待补发买家事件，正在按顺序上报')
                    return
                raise GatewayLoginError(payload.get('reason') or 'token无效或已过期')

    async def _heartbeat(self, ws):
        while True:
            await asyncio.sleep(15)
            try:
                if gateway_token_expires_soon(self.token) and time.time() >= self._next_auth_refresh_at:
                    try:
                        await self._refresh_gateway_auth(force=False)
                    except Exception as exc:
                        self._next_auth_refresh_at = time.time() + 60
                        logger.warning(f'插件网关登录态提前刷新失败：{exc}；60 秒后重试')
                    else:
                        logger.info('插件网关登录态已提前刷新，后续重连将使用新 token')
                await ws.send(json.dumps({
                    'version': 'plugin.v1',
                    'type': 'client.heartbeat',
                    'id': 'heartbeat_' + uuid.uuid4().hex[:12],
                    'payload': self._presence_payload(),
                }))
            except Exception:
                break

    async def _refresh_gateway_auth(self, force):
        if self.auth_manager is None:
            raise GatewayLoginError('未配置网关自动登录信息')
        stale_token = self.token
        auth = await asyncio.to_thread(self.auth_manager.refresh, stale_token, force)
        token = str(auth.get('accessToken') or '')
        if not token:
            raise GatewayLoginError('刷新网关登录态未返回 accessToken')
        self.token = token
        self.ws_url = str(auth.get('wsUrl') or self.ws_url)
        self._next_auth_refresh_at = 0
        return token != stale_token

    async def send(self, data):
        """Persist a buyer event first; the outbox worker sends it after bind."""
        payload = dict(data or {})
        if self.store_id is not None:
            payload['storeId'] = self.store_id
        message_id = str(payload.get('messageId') or '').strip()
        if not message_id:
            raise ValueError('xianyu.message 缺少稳定 messageId')
        message = {
            'version': 'plugin.v1',
            'type': 'xianyu.message',
            'id': message_id,
            'sentAt': datetime.now().astimezone().isoformat(),
            'payload': payload,
        }
        if self.outbox is None:
            raise RuntimeError('xianyu.message Outbox 未配置')
        await asyncio.to_thread(self.outbox.enqueue, message_id, message)
        if self.ws is None:
            logger.warning(f'插件网关未连接，买家事件已保存等待补发：messageId={message_id}')
        self._outbox_wakeup.set()
        return True

    async def _run_outbox(self, ws):
        while True:
            self._outbox_wakeup.clear()
            try:
                row = await asyncio.to_thread(self.outbox.oldest_pending)
            except Exception as exc:
                logger.warning(f'读取待补发买家事件失败：{exc}')
                await asyncio.sleep(5)
                continue
            if row is None:
                try:
                    await asyncio.wait_for(self._outbox_wakeup.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue
            if self._is_expired_buyer_chat(row):
                message_id = row['message_id']
                await asyncio.to_thread(self.outbox.remove, message_id)
                logger.warning(f'已丢弃超过10分钟的买家聊天补发事件：messageId={message_id}')
                continue
            retry_wait = max(0, row['next_attempt_at'] - datetime.now().timestamp())
            if retry_wait > 0:
                try:
                    await asyncio.wait_for(self._outbox_wakeup.wait(), timeout=retry_wait)
                except asyncio.TimeoutError:
                    pass
                continue
            message_id = row['message_id']
            future = asyncio.get_running_loop().create_future()
            self._pending_event_acks[message_id] = future
            try:
                await ws.send(json.dumps(row['payload'], ensure_ascii=False))
                ack = await asyncio.wait_for(future, timeout=20)
                if ack.get('accepted') is True:
                    await asyncio.to_thread(self.outbox.remove, message_id)
                    logger.info(
                        f'上报网关成功：{self._describe_platform_message(row["payload"].get("payload") or {})}'
                    )
                else:
                    reason = ack.get('reason') or ack.get('message') or '网关拒收'
                    await asyncio.to_thread(self.outbox.mark_blocked, message_id, reason)
                    logger.warning(f'网关拒收买家事件：messageId={message_id} 原因={reason}')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await asyncio.to_thread(self.outbox.mark_failed, message_id, exc)
                logger.warning(f'上报网关失败，消息已保留等待补发：messageId={message_id} {exc}')
            finally:
                self._pending_event_acks.pop(message_id, None)

    @classmethod
    def _is_expired_buyer_chat(cls, row, now=None):
        message = row.get('payload') if isinstance(row, dict) else {}
        payload = message.get('payload') if isinstance(message, dict) else {}
        try:
            content_type = int(payload.get('contentType') or 0)
        except (TypeError, ValueError):
            return False
        if content_type not in (1, 2):
            return False

        event_time = 0.0
        try:
            source_time = float(payload.get('time') or 0)
            event_time = source_time / 1000 if source_time > 100000000000 else source_time
        except (TypeError, ValueError):
            pass
        if event_time <= 0:
            try:
                event_time = datetime.fromisoformat(str(message.get('sentAt') or '').replace('Z', '+00:00')).timestamp()
            except (TypeError, ValueError):
                event_time = float(row.get('create_time') or 0)
        current_time = datetime.now().timestamp() if now is None else float(now)
        return event_time > 0 and current_time - event_time > cls.BUYER_CHAT_MAX_AGE_SECONDS

    def _fail_pending_event_acks(self, exc):
        futures = list(self._pending_event_acks.values())
        self._pending_event_acks.clear()
        for future in futures:
            if not future.done():
                future.set_exception(exc)

    async def _handle_message(self, raw):
        try:
            data = json.loads(raw)
        except Exception:
            return
        msg_type = data.get('type') or ''
        payload = data.get('payload') or {}
        if msg_type == 'server.pong':
            executor = payload.get('executor') or {}
            if executor.get('generation'):
                self.executor_generation = int(executor['generation'])
            return
        if msg_type == 'xianyu.message.ack':
            event_id = str(payload.get('eventId') or '')
            future = self._pending_event_acks.get(event_id)
            if future is not None and not future.done():
                future.set_result(payload)
            return
        if msg_type == 'task.message.claim.ack':
            future = self._pending_claims.pop(str(data.get('requestId') or ''), None)
            if future is not None and not future.done():
                future.set_result(payload)
            return
        if msg_type == 'server.error':
            logger.warning(f'网关错误：{payload.get("message")}')
            return
        if msg_type in {
            'task.xianyu.send_message',
            'task.xianyu.get_order_detail',
            'task.xianyu.sync_goods',
            'task.xianyu.adjust_price',
            'task.xianyu.consign_dummy',
            'task.xianyu.cancel_order',
        }:
            # 任务内部会继续等待 claim 回包；不能占用 WebSocket 收包循环。
            self._start_task_worker(data)
            return

        # 兼容旧服务端 reply 格式：reply 可能在顶层或 payload 里
        reply = data.get('reply') or payload.get('reply')
        if not reply:
            return
        raw_msg = data.get('raw') or payload.get('raw') or payload
        toid = raw_msg.get('senderUserId') or ''
        cid = raw_msg.get('cid') or ''
        if not toid or not cid:
            return
        if isinstance(reply, str):
            replies = [reply]
        elif isinstance(reply, list):
            replies = [str(item) for item in reply if item]
        else:
            replies = []
        for text in replies:
            await asyncio.to_thread(
                self._post_reply,
                {'toid': toid, 'cid': cid, 'text': text},
            )

    def _start_task_worker(self, data):
        worker = asyncio.create_task(self._handle_task(data))
        self._task_workers.add(worker)
        worker.add_done_callback(self._task_workers.discard)

    async def _cancel_task_workers(self):
        workers = list(self._task_workers)
        self._task_workers.clear()
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    def _post_reply(self, payload):
        try:
            requests.post(self.reply_url, json=payload, timeout=10)
        except Exception:
            pass

    async def _handle_task(self, data):
        payload = data.get('payload') or {}
        task_id = str(payload.get('taskId') or data.get('id') or '').strip()
        task_type = str(payload.get('taskType') or data.get('type') or '').strip()
        if not task_id or not task_type:
            return
        generation = int(payload.get('executorGeneration') or 0)
        if generation and self.executor_generation and generation != self.executor_generation:
            logger.warning(f'忽略旧执行实例任务：{task_id}')
            return
        await self._send_task_ack(task_id, task_type, generation)
        try:
            logger.info(f'收到网关任务：{self._describe_task(task_type, payload.get("payload") or {})}')
            task_payload = payload.get('payload') or {}
            result = await self._execute_send_messages(task_id, task_payload, generation) if task_type == 'task.xianyu.send_message' else await asyncio.to_thread(self._execute_task, task_type, task_payload)
            logger.info(f'网关任务执行成功：{self._describe_task(task_type, payload.get("payload") or {})}')
            await self._send_task_result(task_id, task_type, True, 'success', result=result, generation=generation)
        except Exception as exc:
            logger.warning(f'插件任务执行失败：{task_type} {task_id} {exc}')
            await self._send_task_result(
                task_id,
                task_type,
                False,
                'failed',
                error_code=exc.__class__.__name__,
                error_message=str(exc),
                result={},
                generation=generation,
            )

    def _presence_payload(self, extra=None):
        payload = dict(extra or {})
        if self.store_id is not None:
            payload['storeId'] = int(self.store_id)
        if self.instance_id:
            payload['instanceId'] = self.instance_id
        if callable(self.chrome_logged_in):
            payload['chromeLoggedIn'] = bool(self.chrome_logged_in())
        return payload

    async def _send_task_ack(self, task_id, task_type, generation=0):
        if self.ws is None:
            return
        await self.ws.send(json.dumps({
            'version': 'plugin.v1',
            'type': 'task.ack',
            'id': 'ack_' + task_id,
            'sentAt': datetime.now().astimezone().isoformat(),
            'payload': {
                'taskId': task_id,
                'taskType': task_type,
                'executorGeneration': generation,
                'receivedAt': datetime.now().astimezone().isoformat(),
            },
        }, ensure_ascii=False))

    async def _send_task_result(self, task_id, task_type, success, status,
                                error_code=None, error_message=None, result=None, generation=0):
        if self.ws is None:
            return
        await self.ws.send(json.dumps({
            'version': 'plugin.v1',
            'type': 'task.result',
            'id': 'result_' + task_id,
            'sentAt': datetime.now().astimezone().isoformat(),
            'payload': {
                'taskId': task_id,
                'taskType': task_type,
                'attemptId': f'client:{self.instance_id or "unknown"}:{task_id}',
                'executorGeneration': generation,
                'status': status,
                'success': bool(success),
                'errorCode': error_code,
                'errorMessage': error_message,
                'result': result or {},
                'finishedAt': datetime.now().astimezone().isoformat(),
            },
        }, ensure_ascii=False))

    async def _execute_send_messages(self, task_id, payload, generation):
        messages = payload.get('messages')
        if messages is None:
            messages = [self._legacy_message(payload)]
        if not isinstance(messages, list) or not messages:
            raise ValueError('payload.messages不能为空')
        results = []
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                raise ValueError('payload.messages格式错误')
            message_key = str(message.get('messageKey') or f'message_{index + 1}')
            claim = await self._claim_task_message(task_id, message_key, generation)
            if not claim.get('accepted'):
                raise RuntimeError(str(claim.get('reason') or '消息领取失败'))
            if claim.get('skip'):
                results.append({'messageKey': message_key, 'status': 'skipped', 'attemptId': claim.get('attemptId')})
                continue
            try:
                data = await asyncio.to_thread(self._send_message_payload, dict(payload, **message))
                data['strict_cid'] = True
                result = await asyncio.to_thread(self._local_post, '/api/reply', data)
                await self._send_task_message_result(task_id, message_key, claim, generation, True, result=result)
                results.append({'messageKey': message_key, 'status': 'succeeded', 'attemptId': claim.get('attemptId')})
            except Exception as exc:
                await self._send_task_message_result(task_id, message_key, claim, generation, False, error_message=str(exc))
                raise
        return {'messages': results}

    @staticmethod
    def _legacy_message(payload):
        if payload.get('imageUrl') or payload.get('image_url'):
            return {'messageKey': 'legacy_image', 'type': 'image', 'imageUrl': payload.get('imageUrl') or payload.get('image_url'), 'width': payload.get('width', 0), 'height': payload.get('height', 0)}
        return {'messageKey': 'legacy_text', 'type': 'text', 'text': payload.get('text') or payload.get('content') or ''}

    async def _claim_task_message(self, task_id, message_key, generation):
        if self.ws is None:
            raise RuntimeError('插件网关未连接')
        request_id = 'claim_' + uuid.uuid4().hex[:16]
        future = asyncio.get_running_loop().create_future()
        self._pending_claims[request_id] = future
        await self.ws.send(json.dumps({'version': 'plugin.v1', 'type': 'task.message.claim', 'id': request_id, 'payload': {'taskId': task_id, 'messageKey': message_key, 'executorGeneration': generation}}, ensure_ascii=False))
        try:
            return await asyncio.wait_for(future, timeout=20)
        finally:
            self._pending_claims.pop(request_id, None)

    async def _send_task_message_result(self, task_id, message_key, claim, generation, success, result=None, error_message=''):
        if self.ws is None:
            return
        await self.ws.send(json.dumps({'version': 'plugin.v1', 'type': 'task.message.result', 'id': 'message_result_' + uuid.uuid4().hex[:16], 'payload': {'taskId': task_id, 'messageKey': message_key, 'attemptId': claim.get('attemptId'), 'claimToken': claim.get('claimToken'), 'executorGeneration': generation, 'success': bool(success), 'result': result or {}, 'errorMessage': error_message}}, ensure_ascii=False))

    def _execute_task(self, task_type, payload):
        if task_type == 'task.xianyu.send_message':
            data = self._send_message_payload(payload)
            result = self._local_post('/api/reply', data)
            text = data.get('text') or data.get('image_url') or ''
            logger.info(f'发送消息给买家：toid={data.get("toid", "")} 内容={self._short_text(text)}')
            return result
        if task_type == 'task.xianyu.get_order_detail':
            order_id = payload.get('orderId') or payload.get('order_id')
            if not order_id:
                raise ValueError('payload.orderId不能为空')
            result = self._local_post('/api/order_detail', {'orderId': str(order_id)})
            logger.info(f'查询订单详情完成：订单={order_id}')
            return result
        if task_type == 'task.xianyu.sync_goods':
            page_size = min(50, max(1, int(payload.get('pageSize') or payload.get('page_size') or 20)))
            result = self._local_post('/api/goods', {'pageSize': page_size})
            goods = result.get('result') if isinstance(result, dict) else {}
            logger.info(f'闲鱼商品同步完成：共{int((goods or {}).get("total") or 0)}项')
            return result
        if task_type == 'task.xianyu.adjust_price':
            order_id = payload.get('orderId') or payload.get('order_id')
            modify_fee = payload.get('modifyFee') or payload.get('modify_fee')
            new_transport_fee = payload.get('newTransportFee')
            if new_transport_fee is None:
                new_transport_fee = payload.get('new_transport_fee', 0)
            if order_id is None or modify_fee is None:
                raise ValueError('payload.orderId和payload.modifyFee不能为空')
            result = self._local_post('/api/adjust_price', {
                'orderId': str(order_id),
                'modifyFee': str(modify_fee),
                'newTransportFee': str(new_transport_fee),
            })
            self._ensure_mtop_success(result, '闲鱼改价')
            logger.info(f'订单改价完成：订单={order_id} 金额分={modify_fee}')
            return result
        if task_type == 'task.xianyu.consign_dummy':
            order_id = payload.get('orderId') or payload.get('order_id')
            if not order_id:
                raise ValueError('payload.orderId不能为空')
            result = self._local_post('/api/consign_dummy', {
                'orderId': str(order_id),
                'tradeText': str(payload.get('tradeText') or payload.get('trade_text') or '已出票'),
                'picList': payload.get('picList') if payload.get('picList') is not None else payload.get('pic_list', []),
            })
            self._ensure_mtop_success(result, '闲鱼虚拟发货')
            logger.info(f'闲鱼虚拟发货完成：订单={order_id}')
            return result
        if task_type == 'task.xianyu.cancel_order':
            order_id = payload.get('orderId') or payload.get('order_id')
            if not order_id:
                raise ValueError('payload.orderId不能为空')
            result = self._local_post('/api/cancel_order', {
                'orderId': str(order_id),
                'closeReason': str(payload.get('closeReason') or payload.get('close_reason') or '与买家协商一致'),
            })
            self._ensure_mtop_success(result, '闲鱼取消订单')
            logger.info(f'闲鱼取消订单完成：订单={order_id}')
            return result
        raise ValueError(f'不支持的任务类型：{task_type}')

    @staticmethod
    def _ensure_mtop_success(result, action):
        """MTop 请求即使 HTTP 200，也会把业务失败写入 ret。"""
        response = result.get('result') if isinstance(result, dict) else None
        if not isinstance(response, dict):
            raise RuntimeError(f'{action}失败：接口返回异常')

        ret = response.get('ret')
        ret_items = ret if isinstance(ret, list) else [ret] if ret else []
        if any(str(item).upper().startswith('SUCCESS') for item in ret_items):
            return

        data = response.get('data') if isinstance(response.get('data'), dict) else {}
        message = data.get('errMsg') or data.get('errCode') or next(
            (str(item) for item in ret_items if str(item).strip()),
            '',
        )
        raise RuntimeError(f'{action}失败：{message or "闲鱼未确认改价成功"}')

    def _send_message_payload(self, payload):
        toid = payload.get('toid') or payload.get('buyerId') or payload.get('senderUserId')
        if not toid:
            raise ValueError('payload.toid不能为空')
        data = {
            'toid': str(toid),
            'cid': str(payload.get('cid') or ''),
            'item_id': str(payload.get('itemId') or payload.get('item_id') or ''),
        }
        image_url = payload.get('imageUrl') or payload.get('image_url')
        if image_url:
            width, height = self._image_size(
                str(image_url),
                int(payload.get('width') or 0),
                int(payload.get('height') or 0),
            )
            data.update({
                'image_url': str(image_url),
                'width': width,
                'height': height,
            })
            return data
        text = payload.get('text')
        if text is None:
            text = payload.get('content')
        if text is None:
            raise ValueError('payload.text不能为空')
        data['text'] = str(text)
        return data

    @staticmethod
    def _image_size(image_url, width=0, height=0):
        if width > 0 and height > 0:
            return width, height
        try:
            response = requests.get(image_url, timeout=10)
            response.raise_for_status()
            with Image.open(BytesIO(response.content)) as image:
                image_width, image_height = image.size
                if image_width > 0 and image_height > 0:
                    return image_width, image_height
        except Exception as exc:
            logger.warning(f'获取图片尺寸失败，将使用默认尺寸：{exc}')
        return width or 512, height or 512

    def _local_post(self, path, payload):
        response = requests.post(self.api_base_url + path, json=payload, timeout=30)
        try:
            body = response.json()
        except Exception:
            body = {'raw': response.text}
        if response.status_code >= 400:
            raise RuntimeError(str(body.get('error') or body))
        return body

    @staticmethod
    def _short_text(value, limit=80):
        text = str(value or '').replace('\n', ' ').strip()
        return text if len(text) <= limit else text[:limit] + '...'

    def _describe_platform_message(self, payload):
        content_type = int(payload.get('contentType') or 0)
        buyer = payload.get('reminderTitle') or payload.get('buyerNick') or payload.get('senderUserId') or ''
        if content_type == 1:
            return f'文字 买家={buyer} 内容={self._short_text(payload.get("text"))}'
        if content_type == 2:
            return f'图片 买家={buyer} 地址={self._short_text(payload.get("url"), 60)}'
        if content_type == 3:
            return f'创建订单 买家={buyer} 订单={payload.get("orderId", "")}'
        if content_type == 4:
            return f'付款订单 买家={buyer} 订单={payload.get("orderId", "")}'
        if content_type == 5:
            return f'退款订单 买家={buyer} 订单={payload.get("orderId", "")}'
        return f'类型={content_type} 买家={buyer}'

    def _describe_task(self, task_type, payload):
        if task_type == 'task.xianyu.send_message':
            messages = payload.get('messages') or [payload]
            return f'发送消息 toid={payload.get("toid") or payload.get("buyerId") or ""} 共{len(messages)}项'
        if task_type == 'task.xianyu.get_order_detail':
            return f'查询订单详情 订单={payload.get("orderId") or payload.get("order_id") or ""}'
        if task_type == 'task.xianyu.sync_goods':
            return f'同步闲鱼商品 每页={payload.get("pageSize") or payload.get("page_size") or 20}'
        if task_type == 'task.xianyu.adjust_price':
            return f'修改订单价格 订单={payload.get("orderId") or payload.get("order_id") or ""} 金额分={payload.get("modifyFee") or payload.get("modify_fee") or ""}'
        if task_type == 'task.xianyu.consign_dummy':
            return f'虚拟发货 订单={payload.get("orderId") or payload.get("order_id") or ""}'
        if task_type == 'task.xianyu.cancel_order':
            return f'取消订单 订单={payload.get("orderId") or payload.get("order_id") or ""}'
        return task_type
