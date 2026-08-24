import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlencode

import requests
from loguru import logger
import websockets

from utils.goofish_utils import generate_device_id

LOGIN_URL = 'https://seller.goofish.com/'
# 登录态与 IM token 的必要条件。tracknick 只用于展示昵称，_tb_token_ 也并非所有
# seller.goofish.com 登录态都会写入；实际 token 校验成功才是最终判定。
REQUIRED_COOKIES = {'unb', '_m_h5_tk', 'cookie2'}


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def find_chrome_binary():
    candidates = []
    if sys.platform == 'darwin':
        candidates = [
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            str(Path.home() / 'Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        ]
    elif sys.platform.startswith('win'):
        roots = [
            os.environ.get('PROGRAMFILES'),
            os.environ.get('PROGRAMFILES(X86)'),
            os.environ.get('LOCALAPPDATA'),
        ]
        for root in roots:
            if root:
                candidates.append(str(Path(root) / 'Google' / 'Chrome' / 'Application' / 'chrome.exe'))
    else:
        candidates = ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser']
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    for name in candidates:
        found = shutil.which(name)
        if found:
            return found
    return None


def check_chrome_installed():
    """返回 (是否可用, chrome 路径, 安装提示)。"""
    path = os.environ.get('XY_CHROME_PATH') or find_chrome_binary()
    if path:
        return True, path, ''
    hint = ('未检测到 Google Chrome，请先安装浏览器后再运行。'
            '下载地址: https://www.google.com/chrome/')
    return False, None, hint


def _is_xianyu_cookie(cookie):
    domain = cookie.get('domain') or ''
    return 'goofish.com' in domain or 'mmstat.com' in domain


def _cookies_to_string(cookies):
    return '; '.join(f"{c['name']}={c['value']}" for c in cookies)


def _debugger_page_url(port):
    """优先连接闲鱼卖家中心页面，避免误连 Chrome 的空白新标签页。"""
    try:
        targets = requests.get(f'http://127.0.0.1:{port}/json', timeout=2).json()
    except Exception:
        return ''

    pages = [target for target in targets if target.get('type') == 'page' and target.get('webSocketDebuggerUrl')]
    if not pages:
        return ''
    pages.sort(key=lambda target: (
        'seller.goofish.com' not in str(target.get('url') or ''),
        'goofish.com' not in str(target.get('url') or ''),
    ))
    return str(pages[0].get('webSocketDebuggerUrl') or '')


def _run_in_new_loop(coro):
    """在独立线程里运行协程，避免与主事件循环冲突。"""
    result = {}
    error = {}

    def target():
        try:
            result['value'] = asyncio.run(coro)
        except Exception as exc:
            error['exc'] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error['exc']
    return result['value']


async def _poll_login_cookies(ws_url, deadline):
    async with websockets.connect(ws_url, max_size=None) as ws:
        request_id = 0

        async def call(method, params=None):
            nonlocal request_id
            request_id += 1
            await ws.send(json.dumps({'id': request_id, 'method': method, 'params': params or {}}))
            while True:
                message = json.loads(await ws.recv())
                if message.get('id') == request_id:
                    return message.get('result', {}) or {}

        await call('Network.enable')

        async def get_user_agent():
            try:
                eval_result = await call('Runtime.evaluate', {'expression': 'navigator.userAgent'})
                return str((eval_result.get('result') or {}).get('value') or '')
            except Exception:
                return ''

        async def get_sdk_device_id():
            """读取页面里 IM SDK 实际使用的设备号 im_sdk_did-im_sdk_tab_id。"""
            expression = (
                "(function(){ try { "
                "var d = sessionStorage.getItem('im_sdk_did') || localStorage.getItem('im_sdk_did') || ''; "
                "if (!d) { for (var i = 0; i < localStorage.length; i++) { "
                "var k = localStorage.key(i); "
                "if (k && k.indexOf('im_sdk_did') > -1) { d = localStorage.getItem(k); break; } } } "
                "var t = sessionStorage.getItem('im_sdk_tab_id') || ''; "
                "return d ? d + (t ? '-' + t : '') : ''; "
                "} catch(e) { return ''; } })()"
            )
            result = await call('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
            return str((result.get('result') or {}).get('value') or '')

        def build_token_request(cookie_dict, device_id):
            token = (cookie_dict.get('_m_h5_tk') or '').split('_')[0]
            t = str(int(time.time() * 1000))
            data_val = f'{{"appKey":"444e9908a51d1cb236a27862abc769c9","deviceId":"{device_id}"}}'
            sign = hashlib.md5(f'{token}&{t}&34839810&{data_val}'.encode('utf-8')).hexdigest()
            params = {
                'jsv': '2.7.2',
                'appKey': '34839810',
                't': t,
                'sign': sign,
                'v': '1.0',
                'type': 'originaljson',
                'accountSite': 'xianyu',
                'dataType': 'json',
                'timeout': '20000',
                'api': 'mtop.taobao.idlemessage.pc.login.token',
                'sessionOption': 'AutoLoginOnly',
                'spm_cnt': 'a21ybx.im.0.0',
                'spm_pre': 'a21ybx.item.want.1.14ad3da6ALVq3n',
                'log_id': '14ad3da6ALVq3n',
            }
            url = 'https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/?' + urlencode(params)
            body = 'data=' + quote(data_val, safe='')
            return url, body

        async def fetch_token(cookie_dict, device_id):
            url, body = build_token_request(cookie_dict, device_id)
            expression = (
                '(async () => { try { const res = await fetch(' + json.dumps(url) + ', '
                '{method:"POST", headers:{"Content-Type":"application/x-www-form-urlencoded"}, '
                'body:' + json.dumps(body) + ', credentials:"include"}); '
                'return await res.text(); } catch (e) { return "FETCH_ERROR:" + e.message; } })()'
            )
            result = await call('Runtime.evaluate', {
                'expression': expression,
                'awaitPromise': True,
                'returnByValue': True,
            })
            text = str((result.get('result') or {}).get('value') or '')
            if text.startswith('FETCH_ERROR:'):
                return None
            try:
                data = json.loads(text)
            except Exception:
                return None
            ret = data.get('ret') or []
            if any('SUCCESS' in str(item) for item in ret):
                token = (data.get('data') or {}).get('accessToken')
                if token:
                    return token
            return None

        settled = False
        warned = False
        while time.time() < deadline:
            result = await call('Network.getAllCookies')
            cookies = result.get('cookies') or []
            if not cookies:
                result = await call('Storage.getCookies')
                cookies = result.get('cookies') or []
            names = {c.get('name') for c in cookies}
            if REQUIRED_COOKIES.issubset(names):
                if not settled:
                    # 等 2 秒让页面把完整 cookie（cookie2/_tb_token_ 等）写齐再取
                    settled = True
                    await asyncio.sleep(2)
                    continue
                filtered = [c for c in cookies if _is_xianyu_cookie(c)]
                if filtered:
                    cookie_dict = {c.get('name'): c.get('value') for c in filtered}
                    device_id = await get_sdk_device_id() or generate_device_id(cookie_dict.get('unb', ''))
                    access_token = await fetch_token(cookie_dict, device_id)
                    if access_token:
                        user_agent = await get_user_agent()
                        logger.info(f'使用设备号: {device_id}')
                        return _cookies_to_string(filtered), user_agent, access_token, device_id
                    if not warned:
                        logger.warning('闲鱼风控触发，若 Chrome 中弹出验证码请先完成验证，正在自动重试...')
                        warned = True
            else:
                settled = False
            await asyncio.sleep(2)
        raise TimeoutError('等待手动登录/获取 token 超时')


def fetch_cookies_via_chrome(url=LOGIN_URL, timeout=300, profile_dir=None, port=None):
    """启动本机 Chrome 打开卖家中心，等待手动登录后通过 CDP 获取 cookie。"""
    chrome = os.environ.get('XY_CHROME_PATH') or find_chrome_binary()
    if not chrome:
        raise RuntimeError('未找到 Google Chrome，请安装或设置 XY_CHROME_PATH')
    profile_dir = (
        profile_dir
        or os.environ.get('XY_CHROME_PROFILE')
        or str(Path.home() / '.xianyu' / 'chrome_profile')
    )
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    port = port or _free_port()
    logger.info(f'正在打开 Chrome 并访问 {url}，请在该独立浏览器窗口完成登录')
    logger.info(f'闲鱼 Chrome 独立登录目录: {profile_dir}')
    cmd = [
        chrome,
        f'--remote-debugging-port={port}',
        f'--user-data-dir={profile_dir}',
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-session-crashed-bubble',
        url,
    ]
    process = subprocess.Popen(cmd)
    try:
        deadline = time.time() + timeout
        last_error = ''
        while time.time() < deadline:
            ws_url = _debugger_page_url(port)
            if ws_url:
                # 登录页跳转时 page 级 CDP 连接会正常断开。短轮询后重新发现目标页面，
                # 直到整体登录超时，而不是把一次跳转误报为 Chrome 登录失败。
                attempt_deadline = min(deadline, time.time() + 15)
                try:
                    cookie_string, user_agent, access_token, device_id = _run_in_new_loop(
                        _poll_login_cookies(ws_url, attempt_deadline)
                    )
                    logger.success('已获取登录 cookie')
                    return cookie_string, user_agent, access_token, device_id
                except Exception as exc:
                    last_error = str(exc)
                    logger.debug(f'等待闲鱼登录页面稳定，重新连接 Chrome: {last_error}')
            time.sleep(0.5)
        detail = f'，最后一次连接错误: {last_error}' if last_error else ''
        raise TimeoutError('等待手动登录/获取 token 超时' + detail)
    finally:
        try:
            process.terminate()
        except Exception:
            pass


class CookieStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(data, dict) and 'cookies' in data:
                return {
                    'cookies': data.get('cookies') or {},
                    'user_agent': data.get('user_agent') or '',
                    'access_token': data.get('access_token') or '',
                    'device_id': data.get('device_id') or '',
                }
            if isinstance(data, dict):
                return {'cookies': data, 'user_agent': '', 'access_token': '', 'device_id': ''}
        except Exception:
            logger.warning(f'cookie 文件解析失败: {self.path}')
        return None

    def save(self, cookies, user_agent='', access_token='', device_id=''):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'cookies': cookies,
            'user_agent': user_agent,
            'access_token': access_token,
            'device_id': device_id,
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        logger.info(f'cookie 已保存到 {self.path}')
