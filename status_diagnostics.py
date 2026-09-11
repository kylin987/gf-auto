from __future__ import annotations

import os
import socket
import sqlite3
import tempfile
import threading
import time
from datetime import datetime

import requests

import app_paths
import updater
from app_version import APP_VERSION
from cookie_auth import check_chrome_installed
from goofish_apis import is_token_expired_response
from ws_client import gateway_client_diagnostics


STATUSES = ('normal', 'warning', 'error', 'unknown')
_SEVERITY = {'normal': 0, 'warning': 1, 'error': 2}
_FAILED_TASK_STATUSES = {'failed', 'timeout', 'cancelled', 'canceled'}
_HEALTH_STALE_SECONDS = 180


def _int_or_zero(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def diagnostic_item(key, title, status, detail='', action=None):
    normalized_status = status if status in STATUSES else 'unknown'
    return {
        'key': str(key),
        'title': str(title),
        'status': normalized_status,
        'detail': str(detail or ''),
        'action': action,
    }


def summarize(items):
    counts = {status: 0 for status in STATUSES}
    completed = []
    for entry in items or []:
        status = entry.get('status') if isinstance(entry, dict) else 'unknown'
        status = status if status in STATUSES else 'unknown'
        counts[status] += 1
        if status in _SEVERITY:
            completed.append(status)
    if completed:
        overall = max(completed, key=_SEVERITY.get)
    elif counts['unknown']:
        overall = 'unknown'
    else:
        overall = 'normal'
    return {'status': overall, 'counts': counts}


def run_isolated_check(key, title, check, timeout=15, timeout_action=None):
    result = {}

    def worker():
        try:
            result['item'] = check()
        except Exception as exc:
            result['error'] = exc

    thread = threading.Thread(target=worker, daemon=True, name=f'diagnostics-{key}')
    thread.start()
    thread.join(max(0.001, float(timeout)))
    if thread.is_alive():
        return diagnostic_item(
            key, title, 'unknown', f'检测超时（超过 {timeout:g} 秒）', timeout_action,
        )
    if 'error' in result:
        return diagnostic_item(key, title, 'unknown', str(result['error']), timeout_action)
    item = result.get('item')
    if not isinstance(item, dict):
        return diagnostic_item(key, title, 'unknown', '检测未返回有效结果', timeout_action)
    return diagnostic_item(
        item.get('key', key),
        item.get('title', title),
        item.get('status', 'unknown'),
        item.get('detail', ''),
        item.get('action'),
    )


def _run_parallel_checks(specs):
    entries = []
    for key, title, check, timeout in specs:
        result = {}

        def worker(target=check, holder=result):
            try:
                holder['item'] = target()
            except Exception as exc:
                holder['error'] = exc

        thread = threading.Thread(target=worker, daemon=True, name=f'diagnostics-{key}')
        started_at = time.monotonic()
        thread.start()
        entries.append((key, title, timeout, started_at, thread, result))

    items = []
    for key, title, timeout, started_at, thread, result in entries:
        remaining = max(0.0, float(timeout) - (time.monotonic() - started_at))
        thread.join(remaining)
        if thread.is_alive():
            items.append(diagnostic_item(key, title, 'unknown', f'检测超时（超过 {timeout:g} 秒）'))
        elif 'error' in result:
            items.append(diagnostic_item(key, title, 'unknown', str(result['error'])))
        else:
            value = result.get('item')
            if isinstance(value, dict):
                items.append(diagnostic_item(
                    value.get('key', key), value.get('title', title),
                    value.get('status', 'unknown'), value.get('detail', ''), value.get('action'),
                ))
            else:
                items.append(diagnostic_item(key, title, 'unknown', '检测未返回有效结果'))
    return items


def diagnose_outbox(summary):
    summary = summary if isinstance(summary, dict) else {}
    pending = int(summary.get('pendingCount') or 0)
    blocked = int(summary.get('blockedCount') or 0)
    age = max(0.0, float(summary.get('oldestPendingAge') or 0.0))
    if blocked:
        return diagnostic_item(
            'outbox', '待上报事件', 'error',
            f'{blocked} 条事件已被网关阻塞，请查看运行日志确认原因', 'view_advice',
        )
    if pending and age > 600:
        return diagnostic_item(
            'outbox', '待上报事件', 'error',
            f'{pending} 条事件待补发，最老一条已超过 10 分钟', 'view_advice',
        )
    if pending:
        return diagnostic_item(
            'outbox', '待上报事件', 'warning',
            f'{pending} 条事件正在等待补发，最老一条约 {int(age)} 秒', 'view_advice',
        )
    return diagnostic_item('outbox', '待上报事件', 'normal', '没有待补发或阻塞事件')


def _response_reason(result):
    if not isinstance(result, dict):
        return str(result)
    ret = result.get('ret') or []
    values = ret if isinstance(ret, list) else [ret]
    reason = '; '.join(str(value) for value in values if value)
    return reason or str(result)


def _is_login_expired_response(result):
    if is_token_expired_response(result):
        return True
    reason = _response_reason(result).upper()
    return (
        'SESSION_EXPIRED' in reason
        or 'SESSION_INVALID' in reason
        or '登录过期' in reason
        or '登录失效' in reason
    )


def diagnose_goods_capability(live):
    try:
        result = live.xianyu.search_seller_items(1, 1)
    except Exception as exc:
        return diagnostic_item(
            'goods_capability', '商品管理能力', 'unknown',
            f'商品管理接口无法判断：{exc}',
        )
    if _is_login_expired_response(result):
        return diagnostic_item(
            'goods_capability', '商品管理能力', 'unknown',
            '闲鱼登录态已失效，无法判断商品管理能力', 'relogin',
        )
    if isinstance(result, dict):
        ret = result.get('ret') or []
        ret_items = ret if isinstance(ret, list) else [ret]
        outer = result.get('data')
        data = outer.get('data') if isinstance(outer, dict) else None
        success = (
            any(str(value).upper().startswith('SUCCESS') for value in ret_items)
            and isinstance(outer, dict)
            and outer.get('code') == 'success'
            and isinstance(data, dict)
            and data.get('success') is True
        )
        if success:
            return diagnostic_item(
                'goods_capability', '商品管理能力', 'normal',
                '商品管理接口可用',
            )
    return diagnostic_item(
        'goods_capability', '商品管理能力', 'unknown',
        f'商品管理接口不可用：{_response_reason(result)}', 'view_advice',
    )


class StatusDiagnostics:
    def __init__(self, auth_manager, auth, instances, lives, run_threads, pub_id,
                 gateway_checker=None):
        self.auth_manager = auth_manager
        self.auth = auth if isinstance(auth, dict) else {}
        self.instances = list(instances or [])
        self.lives = lives if isinstance(lives, dict) else {}
        self.run_threads = run_threads if isinstance(run_threads, dict) else {}
        self.pub_id = int(pub_id or 0)
        self.gateway_checker = gateway_checker or gateway_client_diagnostics

    def run(self):
        local_holder = {}
        gateway_holder = {}
        stores_holder = {}

        local_thread = threading.Thread(
            target=lambda: local_holder.update(items=_run_parallel_checks(self._local_check_specs())),
            daemon=True,
            name='diagnostics-local',
        )
        gateway_thread = threading.Thread(
            target=lambda: gateway_holder.update(self._load_gateway_diagnostics()),
            daemon=True,
            name='diagnostics-gateway',
        )
        stores_thread = threading.Thread(
            target=lambda: stores_holder.update(stores=self._diagnose_stores_parallel()),
            daemon=True,
            name='diagnostics-stores',
        )
        threads = (local_thread, gateway_thread, stores_thread)
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 35
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

        local_items = local_holder.get('items')
        if not isinstance(local_items, list):
            local_items = [diagnostic_item(
                'local_diagnostics', '本机环境检测', 'unknown', '本机环境检测超时',
            )]
        refreshed_auth = gateway_holder.get('auth')
        auth_error = gateway_holder.get('authError')
        gateway_data = gateway_holder.get('data')
        gateway_error = gateway_holder.get('error')

        if refreshed_auth is not None:
            local_items.append(diagnostic_item(
                'gateway_token', 'Gateway 授权', 'normal', '已获取当前 SaaS 店铺授权',
            ))
        else:
            local_items.append(diagnostic_item(
                'gateway_token', 'Gateway 授权', 'unknown',
                f'无法刷新当前授权：{auth_error or "检测超时"}', 'view_advice',
            ))

        if gateway_data is not None:
            gateway_item = diagnostic_item(
                'gateway_diagnostics', 'Gateway 实时诊断', 'normal', '实时诊断接口可用',
            )
        else:
            gateway_item = diagnostic_item(
                'gateway_diagnostics', 'Gateway 实时诊断', 'unknown',
                f'无法取得 Gateway 实时状态：{gateway_error or auth_error or "检测超时"}',
                'view_advice',
            )

        gateway_stores = self._gateway_store_map(gateway_data)
        stores = stores_holder.get('stores')
        if not isinstance(stores, list):
            stores = [self._store_timeout_result(instance) for instance in self.instances]
        stores = [
            self._finalize_store(store, refreshed_auth, gateway_stores, gateway_data is not None)
            for store in stores
        ]

        all_items = list(local_items) + [gateway_item]
        for store in stores:
            all_items.extend(store['items'])
        result_summary = summarize(all_items)
        return {
            'status': result_summary['status'],
            'counts': result_summary['counts'],
            'local': local_items,
            'gateway': gateway_item,
            'stores': stores,
            'checkedAt': datetime.now().astimezone().isoformat(),
        }

    def _load_gateway_diagnostics(self):
        refreshed_auth = None
        auth_error = None
        try:
            refreshed_auth = self.auth_manager.refresh(force=True)
            if not isinstance(refreshed_auth, dict) or not refreshed_auth.get('accessToken'):
                raise RuntimeError('网关授权刷新未返回 accessToken')
        except Exception as exc:
            auth_error = exc

        gateway_data = None
        gateway_error = auth_error
        if refreshed_auth is not None:
            try:
                gateway_data = self.gateway_checker(
                    str(refreshed_auth.get('accessToken') or ''),
                    self._gateway_instances(),
                )
            except Exception as exc:
                gateway_error = exc
        return {
            'auth': refreshed_auth,
            'authError': auth_error,
            'data': gateway_data,
            'error': gateway_error,
        }

    def _diagnose_stores_parallel(self):
        entries = []
        for index, instance in enumerate(self.instances):
            holder = {}

            def worker(item=instance, item_index=index, result=holder):
                try:
                    result['store'] = self._diagnose_store_local(item, item_index)
                except Exception as exc:
                    result['error'] = exc

            thread = threading.Thread(
                target=worker,
                daemon=True,
                name=f'diagnostics-store-{instance.get("id") or index}',
            )
            thread.start()
            entries.append((instance, thread, holder))

        stores = []
        deadline = time.monotonic() + 25
        for instance, thread, holder in entries:
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                error = '该店铺检测超时（超过 25 秒）'
            elif 'error' in holder:
                error = f'该店铺检测未完成：{holder["error"]}'
            else:
                stores.append(holder['store'])
                continue
            stores.append(self._store_timeout_result(instance, error))
        return stores

    @staticmethod
    def _store_timeout_result(instance, detail='该店铺检测超时（超过 25 秒）'):
        failure = diagnostic_item('store_diagnostics', '店铺状态检测', 'unknown', detail)
        store_summary = summarize([failure])
        return {
            'instanceId': str(instance.get('id') or ''),
            'storeId': _int_or_zero(instance.get('storeId')),
            'name': str(instance.get('name') or instance.get('storeName') or ''),
            'status': store_summary['status'],
            'counts': store_summary['counts'],
            'items': [failure],
        }

    def _local_check_specs(self):
        return [
            ('version', '客户端版本', self._check_version, 15),
            ('network_xianyu', '闲鱼网络', lambda: self._check_endpoint(
                'network_xianyu', '闲鱼网络', 'h5api.m.goofish.com'
            ), 5),
            ('network_gateway', 'Gateway 网络', lambda: self._check_endpoint(
                'network_gateway', 'Gateway 网络', 'plugin-gateway.yinghuasuan.com'
            ), 5),
            ('chrome', 'Google Chrome', self._check_chrome, 5),
            ('local_data', '本地数据', self._check_local_data, 10),
            ('configured_instances', '店铺实例', self._check_configured_instances, 2),
        ]

    def _check_version(self):
        info = updater.check_for_update(APP_VERSION)
        if info is None:
            return diagnostic_item('version', '客户端版本', 'normal', f'当前已是 v{APP_VERSION}')
        return diagnostic_item(
            'version', '客户端版本', 'warning',
            f'发现新版本 v{info.version}，当前为 v{APP_VERSION}', 'check_update',
        )

    @staticmethod
    def _check_endpoint(key, title, host):
        try:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            return diagnostic_item(key, title, 'error', f'DNS 解析失败：{exc}', 'view_advice')
        try:
            with socket.create_connection((host, 443), timeout=3):
                pass
        except OSError as exc:
            return diagnostic_item(key, title, 'error', f'网络连接失败：{exc}', 'view_advice')
        return diagnostic_item(key, title, 'normal', f'{host} 可连接')

    @staticmethod
    def _check_chrome():
        installed, path, hint = check_chrome_installed()
        if not installed:
            return diagnostic_item('chrome', 'Google Chrome', 'error', hint, 'view_advice')
        if not os.access(str(path), os.R_OK | os.X_OK):
            return diagnostic_item(
                'chrome', 'Google Chrome', 'error', f'Chrome 不可执行：{path}', 'view_advice',
            )
        return diagnostic_item('chrome', 'Google Chrome', 'normal', f'Chrome 可用：{path}')

    def _check_configured_instances(self):
        if not self.instances:
            return diagnostic_item(
                'configured_instances', '店铺实例', 'warning', '当前没有已配置店铺实例', 'view_advice',
            )
        return diagnostic_item(
            'configured_instances', '店铺实例', 'normal', f'已配置 {len(self.instances)} 个店铺实例',
        )

    def _check_local_data(self):
        config_path = app_paths.instances_file(self.pub_id)
        missing = []
        try:
            self._probe_directory(config_path.parent)
            if config_path.exists():
                self._probe_file(config_path)
            else:
                missing.append('实例配置')
            for instance in self.instances:
                root = config_path.parent / 'instances' / str(instance.get('id') or '')
                for label, directory in (
                    ('实例目录', root),
                    ('Chrome Profile', root / 'chrome-profile'),
                    ('日志目录', root / 'log'),
                ):
                    if directory.is_dir():
                        self._probe_directory(directory)
                    else:
                        missing.append(f'{instance.get("name") or instance.get("id")} {label}')
                cookie_path = root / 'cookies.json'
                if cookie_path.is_file():
                    self._probe_file(cookie_path)
                else:
                    missing.append(f'{instance.get("name") or instance.get("id")} Cookie')
                database_path = root / 'event_outbox.sqlite3'
                if database_path.is_file():
                    self._probe_sqlite(database_path)
                else:
                    missing.append(f'{instance.get("name") or instance.get("id")} Outbox')
            if missing:
                return diagnostic_item(
                    'local_data', '本地数据', 'warning',
                    '以下数据尚未生成：' + '、'.join(missing), 'view_advice',
                )
            return diagnostic_item('local_data', '本地数据', 'normal', '配置和实例数据可读写')
        except Exception as exc:
            return diagnostic_item(
                'local_data', '本地数据', 'error', f'本地数据读写失败：{exc}', 'view_advice',
            )

    @staticmethod
    def _probe_directory(path):
        with tempfile.NamedTemporaryFile(prefix='.diagnostics-', dir=path, delete=True):
            pass

    @staticmethod
    def _probe_file(path):
        with open(path, 'rb') as handle:
            handle.read(1)
        with open(path, 'r+b'):
            pass

    @staticmethod
    def _probe_sqlite(path):
        connection = sqlite3.connect(str(path), timeout=3)
        try:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('CREATE TABLE __diagnostics_write_probe (id INTEGER)')
            connection.rollback()
        finally:
            connection.close()

    def _gateway_instances(self):
        result = []
        for instance in self.instances:
            try:
                store_id = int(instance.get('storeId') or 0)
            except (TypeError, ValueError):
                continue
            instance_id = str(instance.get('id') or '').strip()
            if store_id > 0 and instance_id:
                result.append({'storeId': store_id, 'instanceId': instance_id})
        return result

    @staticmethod
    def _gateway_store_map(gateway_data):
        result = {}
        if not isinstance(gateway_data, dict):
            return result
        for store in gateway_data.get('stores') or []:
            if not isinstance(store, dict):
                continue
            try:
                store_id = int(store.get('storeId') or 0)
            except (TypeError, ValueError):
                continue
            key = (store_id, str(store.get('instanceId') or ''))
            result[key] = store
        return result

    def _diagnose_store_local(self, instance, index):
        instance_id = str(instance.get('id') or '')
        store_id = int(instance.get('storeId') or 0)
        live = self.lives.get(instance_id)
        thread = self.run_threads.get(instance_id)
        running = bool(live is not None and thread is not None and thread.is_alive())
        snapshot = None
        snapshot_error = None
        if live is not None:
            try:
                snapshot = live.health_snapshot()
            except Exception as exc:
                snapshot_error = exc
        snapshot = snapshot if isinstance(snapshot, dict) else {}

        items = [
            diagnostic_item(
                'instance_running', '实例运行状态', 'normal' if running else 'error',
                '实例正在运行' if running else '实例未启动',
                None if running else 'start_instance',
            ),
            self._account_item(instance, snapshot),
            self._login_item(running, snapshot, snapshot_error),
            self._im_item(running, snapshot, snapshot_error),
            self._local_api_item(instance_id, index, running, snapshot),
            self._gateway_websocket_item(running, snapshot, snapshot_error),
            self._outbox_item(instance_id, snapshot),
        ]
        if running:
            goods_item = run_isolated_check(
                'goods_capability', '商品管理能力',
                lambda: diagnose_goods_capability(live), timeout=15,
            )
        else:
            goods_item = diagnostic_item(
                'goods_capability', '商品管理能力', 'unknown', '实例未运行，未执行商品接口检测',
            )
        items.append(goods_item)
        if goods_item.get('action') == 'relogin':
            for item in items:
                if item.get('key') == 'cookie_login':
                    item.update({
                        'status': 'error',
                        'detail': '商品接口确认当前闲鱼登录态已失效',
                        'action': 'relogin',
                    })
                    break

        store_summary = summarize(items)
        return {
            'instanceId': instance_id,
            'storeId': store_id,
            'name': str(instance.get('name') or instance.get('storeName') or instance_id),
            'status': store_summary['status'],
            'counts': store_summary['counts'],
            'items': items,
        }

    def _finalize_store(self, store, refreshed_auth, gateway_stores, gateway_available):
        store_id = _int_or_zero(store.get('storeId'))
        instance_id = str(store.get('instanceId') or '')
        items = [self._authorization_item(store_id, refreshed_auth)]
        items.extend(store.get('items') or [])
        items.extend(self._gateway_store_items(
            gateway_stores.get((store_id, instance_id)), gateway_available,
        ))
        store = dict(store)
        store['items'] = items
        store_summary = summarize(items)
        store['status'] = store_summary['status']
        store['counts'] = store_summary['counts']
        return store

    @staticmethod
    def _authorized_store_ids(auth):
        result = set()
        scope = auth.get('scope') if isinstance(auth, dict) else {}
        for store in (scope or {}).get('stores') or []:
            if isinstance(store, dict):
                try:
                    result.add(int(store.get('id') or 0))
                except (TypeError, ValueError):
                    pass
        return result

    def _authorization_item(self, store_id, refreshed_auth):
        if refreshed_auth is None:
            return diagnostic_item(
                'authorization', 'SaaS 店铺授权', 'unknown', '当前授权刷新失败，不能使用旧授权判定',
            )
        if store_id in self._authorized_store_ids(refreshed_auth):
            return diagnostic_item('authorization', 'SaaS 店铺授权', 'normal', '店铺仍在当前授权范围')
        return diagnostic_item(
            'authorization', 'SaaS 店铺授权', 'error', '店铺已不在当前授权范围', 'view_advice',
        )

    def _instance_root(self, instance_id):
        return app_paths.instances_file(self.pub_id).parent / 'instances' / instance_id

    def _account_item(self, instance, snapshot):
        account = snapshot.get('account') if isinstance(snapshot.get('account'), dict) else None
        if account is None:
            cookie_path = self._instance_root(str(instance.get('id') or '')) / 'cookies.json'
            account = app_paths.chrome_account_from_cookie_file(str(cookie_path))
        actual = str((account or {}).get('userId') or '').strip()
        expected = str(instance.get('platformShopId') or '').strip()
        if not actual:
            return diagnostic_item(
                'account_match', '闲鱼账号匹配', 'error', '未识别到当前闲鱼账号', 'relogin',
            )
        if not expected:
            return diagnostic_item(
                'account_match', '闲鱼账号匹配', 'warning', f'当前闲鱼 ID 为 {actual}，后台尚未绑定',
                'view_advice',
            )
        if actual != expected:
            return diagnostic_item(
                'account_match', '闲鱼账号匹配', 'error',
                f'当前闲鱼 ID {actual} 与绑定 ID {expected} 不一致', 'relogin',
            )
        return diagnostic_item('account_match', '闲鱼账号匹配', 'normal', f'闲鱼 ID {actual} 已匹配')

    @staticmethod
    def _login_item(running, snapshot, snapshot_error):
        if not running:
            return diagnostic_item('cookie_login', 'Cookie 登录态', 'unknown', '实例未运行，未实时验证登录态')
        if snapshot_error:
            return diagnostic_item('cookie_login', 'Cookie 登录态', 'unknown', str(snapshot_error))
        if snapshot.get('loginReady'):
            return diagnostic_item('cookie_login', 'Cookie 登录态', 'normal', '当前登录态有效')
        return diagnostic_item('cookie_login', 'Cookie 登录态', 'error', '当前登录态无效', 'relogin')

    @staticmethod
    def _im_item(running, snapshot, snapshot_error):
        if not running:
            return diagnostic_item('im_websocket', '闲鱼 IM', 'unknown', '实例未运行，未建立 IM 连接')
        if snapshot_error:
            return diagnostic_item('im_websocket', '闲鱼 IM', 'unknown', str(snapshot_error))
        im = snapshot.get('im') if isinstance(snapshot.get('im'), dict) else {}
        last_message = float(im.get('lastMessageAt') or 0.0)
        if im.get('registered'):
            age = max(0.0, time.time() - last_message) if last_message else 0.0
            if not last_message or age > _HEALTH_STALE_SECONDS:
                return diagnostic_item(
                    'im_websocket', '闲鱼 IM', 'error',
                    'IM 已注册，但超过 3 分钟未收到协议响应', 'view_advice',
                )
            return diagnostic_item(
                'im_websocket', '闲鱼 IM', 'normal', f'IM 已注册，最近 {int(age)} 秒内收到协议响应',
            )
        return diagnostic_item(
            'im_websocket', '闲鱼 IM', 'error',
            f'IM 尚未完成注册，最后协议响应时间 {last_message:g}', 'view_advice',
        )

    def _local_api_item(self, instance_id, index, running, snapshot):
        port = int(snapshot.get('localApiPort') or (18000 + index))
        if running:
            def check():
                response = requests.get(f'http://127.0.0.1:{port}/health', timeout=3)
                response.raise_for_status()
                data = response.json()
                if str(data.get('instanceId') or '') != instance_id:
                    return diagnostic_item(
                        'local_api', '本地接口', 'error',
                        f'端口 {port} 属于其他实例：{data.get("instanceId") or "未标识"}', 'view_advice',
                    )
                return diagnostic_item('local_api', '本地接口', 'normal', f'本地接口端口 {port} 正常')

            return run_isolated_check('local_api', '本地接口', check, timeout=5)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            occupied = sock.connect_ex(('127.0.0.1', port)) == 0
        if occupied:
            return diagnostic_item(
                'local_api', '本地接口', 'error', f'停止实例的预定端口 {port} 已被占用', 'view_advice',
            )
        return diagnostic_item('local_api', '本地接口', 'normal', f'预定端口 {port} 未被占用')

    @staticmethod
    def _gateway_websocket_item(running, snapshot, snapshot_error):
        if not running:
            return diagnostic_item('gateway_websocket', 'Gateway WebSocket', 'unknown', '实例未运行')
        if snapshot_error:
            return diagnostic_item('gateway_websocket', 'Gateway WebSocket', 'unknown', str(snapshot_error))
        gateway = snapshot.get('gateway') if isinstance(snapshot.get('gateway'), dict) else {}
        last_pong = float(gateway.get('lastPongAt') or 0.0)
        if gateway.get('bound'):
            age = max(0.0, time.time() - last_pong) if last_pong else 0.0
            if not last_pong or age > _HEALTH_STALE_SECONDS:
                return diagnostic_item(
                    'gateway_websocket', 'Gateway WebSocket', 'error',
                    'Gateway 已绑定，但超过 3 分钟未收到 pong', 'view_advice',
                )
            return diagnostic_item(
                'gateway_websocket', 'Gateway WebSocket', 'normal',
                f'Gateway 已绑定，最近 {int(age)} 秒内收到 pong',
            )
        return diagnostic_item(
            'gateway_websocket', 'Gateway WebSocket', 'error',
            f'Gateway 尚未完成绑定，最后 pong 时间 {last_pong:g}', 'view_advice',
        )

    def _outbox_item(self, instance_id, snapshot):
        summary = snapshot.get('outbox') if isinstance(snapshot.get('outbox'), dict) else None
        if summary is None:
            try:
                summary = self._read_outbox_summary(
                    self._instance_root(instance_id) / 'event_outbox.sqlite3'
                )
            except Exception as exc:
                return diagnostic_item('outbox', '待上报事件', 'unknown', f'无法读取 Outbox：{exc}')
        return diagnose_outbox(summary)

    @staticmethod
    def _read_outbox_summary(path):
        if not path.is_file():
            return {
                'pendingCount': 0, 'blockedCount': 0,
                'oldestPendingAt': 0.0, 'oldestPendingAge': 0.0,
            }
        uri = f'{path.resolve().as_uri()}?mode=ro'
        connection = sqlite3.connect(uri, uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute('''
                SELECT
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                    SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS blocked_count,
                    MIN(CASE WHEN status = 'pending' THEN create_time END) AS oldest_pending_at
                FROM event_outbox
            ''').fetchone()
        finally:
            connection.close()
        oldest = float(row['oldest_pending_at'] or 0.0)
        return {
            'pendingCount': int(row['pending_count'] or 0),
            'blockedCount': int(row['blocked_count'] or 0),
            'oldestPendingAt': oldest,
            'oldestPendingAge': max(0.0, time.time() - oldest) if oldest else 0.0,
        }

    @staticmethod
    def _gateway_store_items(store, gateway_available):
        if not gateway_available or not isinstance(store, dict):
            detail = 'Gateway 实时状态不可用' if not gateway_available else 'Gateway 未返回该店铺状态'
            return [
                diagnostic_item('executor', '当前执行器', 'unknown', detail),
                diagnostic_item('devices', '在线设备', 'unknown', detail),
                diagnostic_item('recent_tasks', '近期任务', 'unknown', detail),
            ]

        executor = store.get('executor') if isinstance(store.get('executor'), dict) else {}
        if not executor.get('active'):
            executor_item = diagnostic_item(
                'executor', '当前执行器', 'error', '当前没有有效且在线的店铺执行器', 'view_advice',
            )
        elif not executor.get('ownedByCurrentDevice'):
            executor_item = diagnostic_item(
                'executor', '当前执行器', 'error',
                f'有效执行器属于其他设备或实例：{executor.get("instanceId") or "未标识"}', 'view_advice',
            )
        else:
            executor_item = diagnostic_item('executor', '当前执行器', 'normal', '本机实例是当前有效执行器')

        device_count = int(store.get('deviceCount') or 0)
        if device_count > 1:
            devices_item = diagnostic_item(
                'devices', '在线设备', 'error',
                f'同一店铺有 {device_count} 台设备在线，请关闭其他电脑上的同一实例', 'view_advice',
            )
        elif device_count == 1:
            devices_item = diagnostic_item('devices', '在线设备', 'normal', '当前仅一台设备在线')
        else:
            devices_item = diagnostic_item('devices', '在线设备', 'warning', 'Gateway 未检测到在线设备')

        tasks = [task for task in (store.get('recentTasks') or []) if isinstance(task, dict)]
        failures = [
            task for task in tasks
            if task.get('success') is False
            or str(task.get('status') or '').lower() in _FAILED_TASK_STATUSES
        ]
        unfinished = [
            task for task in tasks
            if task not in failures and task.get('success') is not True
        ]
        if failures:
            details = []
            for task in failures:
                reason = task.get('errorMessage') or task.get('errorCode') or task.get('status') or '失败'
                details.append(f'{task.get("taskId") or "未知任务"}: {reason}')
            tasks_item = diagnostic_item(
                'recent_tasks', '近期任务', 'error', '; '.join(details), 'view_advice',
            )
        elif unfinished:
            tasks_item = diagnostic_item(
                'recent_tasks', '近期任务', 'warning',
                f'{len(unfinished)} 笔近期任务尚无最终成功结果', 'view_advice',
            )
        else:
            tasks_item = diagnostic_item(
                'recent_tasks', '近期任务', 'normal',
                '最近 30 分钟没有失败任务' if not tasks else f'最近 {len(tasks)} 笔任务均成功',
            )
        return [executor_item, devices_item, tasks_item]
