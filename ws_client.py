import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import requests
from loguru import logger
import websockets

GATEWAY_LOGIN_URL = 'https://plugin-gateway.yinghuasuan.com/api/v1/client/login'
DEFAULT_WS_URL = 'wss://plugin-gateway.yinghuasuan.com/ws'
LOGIN_CONFIG_DIR = os.path.join(os.path.expanduser('~'), '.xianyu')
LOGIN_CONFIG_FILE = os.path.join(LOGIN_CONFIG_DIR, 'login.json')


class GatewayLoginError(Exception):
    pass


def load_login_config():
    try:
        with open(LOGIN_CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_login_config(username='', device_id=''):
    try:
        os.makedirs(LOGIN_CONFIG_DIR, exist_ok=True)
        data = load_login_config()
        if username:
            data['username'] = username
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
        save_login_config(username=config.get('username', ''), device_id=device_id)
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
    if data.get('code') != 0 or not data.get('data'):
        raise GatewayLoginError(str(data.get('msg') or data))
    save_login_config(username=username, device_id=device_id)
    return data['data']


class GatewayClient:
    """连接 yhs-plugin-gateway：登录后 bind、心跳、业务消息转发与回复。"""

    def __init__(self, token, ws_url=None, store_id=None,
                 reply_url='http://127.0.0.1:8000/api/reply', stop_event=None):
        self.token = token or ''
        self.ws_url = ws_url or os.environ.get('XY_WS_URL') or DEFAULT_WS_URL
        self.store_id = store_id
        self.reply_url = reply_url
        self.api_base_url = reply_url.rsplit('/', 2)[0]
        self.stop_event = stop_event
        self.ws = None
        self._heartbeat_task = None

    async def run(self):
        while self.stop_event is None or not self.stop_event.is_set():
            try:
                async with websockets.connect(self.ws_url) as ws:
                    self.ws = ws
                    await self._bind(ws)
                    self._heartbeat_task = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for message in ws:
                            await self._handle_message(message)
                    finally:
                        if self._heartbeat_task is not None:
                            self._heartbeat_task.cancel()
                            self._heartbeat_task = None
            except Exception as exc:
                logger.warning(f'插件网关连接异常：{exc}')
            finally:
                self.ws = None
            if self.stop_event is not None and self.stop_event.is_set():
                break
            await asyncio.sleep(5)

    async def _bind(self, ws):
        bind_id = 'bind_' + uuid.uuid4().hex[:12]
        await ws.send(json.dumps({
            'version': 'plugin.v1',
            'type': 'client.bind',
            'id': bind_id,
            'payload': {'token': self.token},
        }))
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if message.get('type') == 'client.bind.ack':
                payload = message.get('payload') or {}
                if payload.get('success'):
                    logger.info('网关绑定成功')
                    return
                raise GatewayLoginError(payload.get('reason') or 'token无效或已过期')

    async def _heartbeat(self, ws):
        while True:
            await asyncio.sleep(25)
            try:
                await ws.send(json.dumps({
                    'version': 'plugin.v1',
                    'type': 'client.heartbeat',
                    'id': 'heartbeat_' + uuid.uuid4().hex[:12],
                    'payload': {},
                }))
            except Exception:
                break

    async def send(self, data):
        """把本地精简消息包装成 xianyu.message 发给网关。"""
        if self.ws is None:
            return
        payload = dict(data or {})
        if self.store_id is not None:
            payload['storeId'] = self.store_id
        message = {
            'version': 'plugin.v1',
            'type': 'xianyu.message',
            'id': 'msg_' + uuid.uuid4().hex[:16],
            'sentAt': datetime.now().astimezone().isoformat(),
            'payload': payload,
        }
        try:
            await self.ws.send(json.dumps(message, ensure_ascii=False))
        except Exception:
            pass

    async def _handle_message(self, raw):
        try:
            data = json.loads(raw)
        except Exception:
            return
        msg_type = data.get('type') or ''
        payload = data.get('payload') or {}
        if msg_type == 'server.pong':
            return
        if msg_type == 'server.error':
            logger.warning(f'网关错误：{payload.get("message")}')
            return
        if msg_type in {
            'task.xianyu.send_message',
            'task.xianyu.get_order_detail',
            'task.xianyu.adjust_price',
        }:
            await self._handle_task(data)
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
        await self._send_task_ack(task_id, task_type)
        try:
            result = await asyncio.to_thread(self._execute_task, task_type, payload.get('payload') or {})
            await self._send_task_result(task_id, task_type, True, 'success', result=result)
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
            )

    async def _send_task_ack(self, task_id, task_type):
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
                'receivedAt': datetime.now().astimezone().isoformat(),
            },
        }, ensure_ascii=False))

    async def _send_task_result(self, task_id, task_type, success, status,
                                error_code=None, error_message=None, result=None):
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
                'status': status,
                'success': bool(success),
                'errorCode': error_code,
                'errorMessage': error_message,
                'result': result or {},
                'finishedAt': datetime.now().astimezone().isoformat(),
            },
        }, ensure_ascii=False))

    def _execute_task(self, task_type, payload):
        if task_type == 'task.xianyu.send_message':
            return self._local_post('/api/reply', self._send_message_payload(payload))
        if task_type == 'task.xianyu.get_order_detail':
            order_id = payload.get('orderId') or payload.get('order_id')
            if not order_id:
                raise ValueError('payload.orderId不能为空')
            return self._local_post('/api/order_detail', {'orderId': str(order_id)})
        if task_type == 'task.xianyu.adjust_price':
            order_id = payload.get('orderId') or payload.get('order_id')
            modify_fee = payload.get('modifyFee') or payload.get('modify_fee')
            new_transport_fee = payload.get('newTransportFee')
            if new_transport_fee is None:
                new_transport_fee = payload.get('new_transport_fee', 0)
            if order_id is None or modify_fee is None:
                raise ValueError('payload.orderId和payload.modifyFee不能为空')
            return self._local_post('/api/adjust_price', {
                'orderId': str(order_id),
                'modifyFee': str(modify_fee),
                'newTransportFee': str(new_transport_fee),
            })
        raise ValueError(f'不支持的任务类型：{task_type}')

    @staticmethod
    def _send_message_payload(payload):
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
            data.update({
                'image_url': str(image_url),
                'width': int(payload.get('width') or 0),
                'height': int(payload.get('height') or 0),
            })
            return data
        text = payload.get('text')
        if text is None:
            text = payload.get('content')
        if text is None:
            raise ValueError('payload.text不能为空')
        data['text'] = str(text)
        return data

    def _local_post(self, path, payload):
        response = requests.post(self.api_base_url + path, json=payload, timeout=30)
        try:
            body = response.json()
        except Exception:
            body = {'raw': response.text}
        if response.status_code >= 400:
            raise RuntimeError(str(body.get('error') or body))
        return body
