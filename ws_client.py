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
                logger.warning(f'AI 服务端连接异常：{exc}')
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
