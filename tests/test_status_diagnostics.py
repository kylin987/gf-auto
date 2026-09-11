import time
import threading
import unittest
from unittest.mock import Mock, patch

from status_diagnostics import (
    StatusDiagnostics,
    diagnostic_item,
    diagnose_goods_capability,
    diagnose_outbox,
    run_isolated_check,
    summarize,
)
from ws_client import GatewayTokenError, gateway_client_diagnostics


class StatusSummaryTest(unittest.TestCase):
    def test_error_wins_but_unknown_is_counted_separately(self):
        result = summarize([
            diagnostic_item('one', '正常', 'normal'),
            diagnostic_item('two', '未知', 'unknown'),
            diagnostic_item('three', '异常', 'error'),
        ])

        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['counts'], {
            'normal': 1,
            'warning': 0,
            'error': 1,
            'unknown': 1,
        })

    def test_timeout_and_exception_are_isolated(self):
        timeout_item = run_isolated_check(
            'slow', '慢检查', lambda: time.sleep(0.05), timeout=0.001,
        )
        error_item = run_isolated_check(
            'broken', '异常检查', lambda: 1 / 0, timeout=0.1,
        )

        self.assertEqual(timeout_item['status'], 'unknown')
        self.assertIn('超时', timeout_item['detail'])
        self.assertEqual(error_item['status'], 'unknown')
        self.assertIn('division by zero', error_item['detail'])


class GatewayDiagnosticsHelperTest(unittest.TestCase):
    @patch('ws_client.requests.post')
    def test_posts_bearer_token_and_instances(self, post):
        post.return_value = Mock(status_code=200, json=Mock(return_value={
            'code': 200,
            'msg': 'success',
            'data': {'stores': [], 'checkedAt': '2026-09-11T10:00:00+08:00'},
        }))

        result = gateway_client_diagnostics('token', [
            {'storeId': 203, 'instanceId': 'store-a'},
        ])

        self.assertEqual(result['stores'], [])
        _, kwargs = post.call_args
        self.assertEqual(kwargs['headers'], {'Authorization': 'Bearer token'})
        self.assertEqual(kwargs['json'], {
            'instances': [{'storeId': 203, 'instanceId': 'store-a'}],
        })
        self.assertEqual(kwargs['timeout'], 15)

    @patch('ws_client.requests.post')
    def test_raises_token_error_for_http_or_business_401(self, post):
        responses = [
            Mock(status_code=401, json=Mock(return_value={'code': 401, 'msg': 'expired'})),
            Mock(status_code=200, json=Mock(return_value={'code': 401, 'msg': 'expired'})),
        ]
        for response in responses:
            with self.subTest(status_code=response.status_code):
                post.return_value = response
                with self.assertRaisesRegex(GatewayTokenError, 'expired'):
                    gateway_client_diagnostics('token', [])

    @patch('ws_client.requests.post')
    def test_rejects_http_error_even_when_body_code_is_200(self, post):
        post.return_value = Mock(status_code=500, json=Mock(return_value={
            'code': 200,
            'msg': 'unexpected proxy response',
            'data': {'stores': []},
        }))

        with self.assertRaisesRegex(RuntimeError, 'unexpected proxy response'):
            gateway_client_diagnostics('token', [])


class DiagnosticClassificationTest(unittest.TestCase):
    def test_blocked_outbox_is_error(self):
        result = diagnose_outbox({
            'pendingCount': 0,
            'blockedCount': 1,
            'oldestPendingAt': 0.0,
            'oldestPendingAge': 0.0,
        })

        self.assertEqual(result['status'], 'error')

    def test_pending_over_ten_minutes_is_error(self):
        result = diagnose_outbox({
            'pendingCount': 2,
            'blockedCount': 0,
            'oldestPendingAt': 100.0,
            'oldestPendingAge': 601.0,
        })

        self.assertEqual(result['status'], 'error')
        self.assertIn('10 分钟', result['detail'])

    def test_empty_goods_list_is_normal_and_uses_single_page(self):
        live = Mock()
        live.xianyu.search_seller_items.return_value = {
            'ret': ['SUCCESS::调用成功'],
            'data': {
                'code': 'success',
                'data': {'success': True, 'itemSearchResponseList': []},
            },
        }

        result = diagnose_goods_capability(live)

        self.assertEqual(result['status'], 'normal')
        live.xianyu.search_seller_items.assert_called_once_with(1, 1)

    def test_expired_goods_login_is_unknown(self):
        live = Mock()
        live.xianyu.search_seller_items.return_value = {
            'ret': ['FAIL_SYS_SESSION_EXPIRED::Session过期'],
        }

        result = diagnose_goods_capability(live)

        self.assertEqual(result['status'], 'unknown')
        self.assertIn('登录态', result['detail'])
        self.assertEqual(result['action'], 'relogin')

    def test_other_goods_failure_keeps_original_reason(self):
        live = Mock()
        live.xianyu.search_seller_items.return_value = {
            'ret': ['FAIL_BIZ_LIMIT::原始失败原因'],
        }

        result = diagnose_goods_capability(live)

        self.assertEqual(result['status'], 'unknown')
        self.assertIn('FAIL_BIZ_LIMIT::原始失败原因', result['detail'])
        self.assertNotIn('无商品管理权限', result['detail'])

    def test_goods_network_failure_is_unknown(self):
        live = Mock()
        live.xianyu.search_seller_items.side_effect = ConnectionError('network down')

        result = diagnose_goods_capability(live)

        self.assertEqual(result['status'], 'unknown')
        self.assertIn('network down', result['detail'])

    def test_executor_requires_active_and_current_device_ownership(self):
        inactive = StatusDiagnostics._gateway_store_items({
            'deviceCount': 1,
            'executor': {'active': False, 'ownedByCurrentDevice': True},
            'recentTasks': [],
        }, True)
        other_device = StatusDiagnostics._gateway_store_items({
            'deviceCount': 1,
            'executor': {'active': True, 'ownedByCurrentDevice': False},
            'recentTasks': [],
        }, True)

        self.assertEqual(inactive[0]['status'], 'error')
        self.assertIn('没有有效且在线', inactive[0]['detail'])
        self.assertEqual(other_device[0]['status'], 'error')
        self.assertIn('其他设备或实例', other_device[0]['detail'])

    @patch('status_diagnostics.time.time', return_value=1_000.0)
    def test_registered_connections_require_recent_health_evidence(self, now):
        im = StatusDiagnostics._im_item(True, {
            'im': {'registered': True, 'lastMessageAt': 700.0},
        }, None)
        gateway = StatusDiagnostics._gateway_websocket_item(True, {
            'gateway': {'bound': True, 'lastPongAt': 700.0},
        }, None)

        self.assertEqual(im['status'], 'error')
        self.assertEqual(gateway['status'], 'error')


class StatusDiagnosticsRunTest(unittest.TestCase):
    @staticmethod
    def _diagnostics(instances=None, lives=None, threads=None, auth_manager=None,
                     gateway_check=None):
        auth = {
            'accessToken': 'old-token',
            'scope': {'stores': [
                {'id': 203, 'platformShopId': 'seller-a'},
                {'id': 204, 'platformShopId': 'seller-b'},
            ]},
        }
        manager = auth_manager or Mock()
        manager.refresh.return_value = {
            'accessToken': 'fresh-token',
            'scope': auth['scope'],
        }
        return StatusDiagnostics(
            manager,
            auth,
            instances or [],
            lives or {},
            threads or {},
            101,
            gateway_checker=gateway_check or Mock(return_value={'stores': []}),
        )

    def test_gateway_failure_keeps_local_results(self):
        diagnostics = self._diagnostics(
            gateway_check=Mock(side_effect=ConnectionError('gateway down')),
        )
        diagnostics._local_check_specs = Mock(return_value=[
            ('local_ok', '本机正常', lambda: diagnostic_item(
                'local_ok', '本机正常', 'normal', '正常'
            ), 0.1),
        ])

        result = diagnostics.run()

        self.assertEqual(result['local'][0]['status'], 'normal')
        self.assertEqual(result['gateway']['status'], 'unknown')
        self.assertIn('gateway down', result['gateway']['detail'])

    def test_refresh_failure_marks_current_authorization_unknown(self):
        manager = Mock()
        manager.refresh.side_effect = RuntimeError('refresh failed')
        gateway_check = Mock()
        instance = {'id': 'store-a', 'storeId': 203, 'platformShopId': 'seller-a'}
        diagnostics = self._diagnostics(
            instances=[instance], auth_manager=manager, gateway_check=gateway_check,
        )
        diagnostics._local_check_specs = Mock(return_value=[])

        result = diagnostics.run()

        manager.refresh.assert_called_once_with(force=True)
        gateway_check.assert_not_called()
        self.assertEqual(result['gateway']['status'], 'unknown')
        authorization = next(
            item for item in result['stores'][0]['items'] if item['key'] == 'authorization'
        )
        self.assertEqual(authorization['status'], 'unknown')

    def test_no_running_instance_reports_start_action_without_goods_call(self):
        instance = {'id': 'store-a', 'storeId': 203, 'platformShopId': 'seller-a'}
        diagnostics = self._diagnostics(instances=[instance])
        diagnostics._local_check_specs = Mock(return_value=[])

        result = diagnostics.run()

        items = {item['key']: item for item in result['stores'][0]['items']}
        self.assertEqual(items['instance_running']['status'], 'error')
        self.assertEqual(items['instance_running']['action'], 'start_instance')
        self.assertEqual(items['goods_capability']['status'], 'unknown')

    def test_expired_goods_response_marks_login_error_but_goods_unknown(self):
        instance = {'id': 'store-a', 'storeId': 203, 'platformShopId': 'seller-a'}
        live = Mock()
        live.health_snapshot.return_value = {
            'instanceId': 'store-a',
            'storeId': 203,
            'localApiPort': 18000,
            'loginReady': True,
            'account': {'userId': 'seller-a', 'nick': 'A'},
            'im': {'registered': True, 'lastMessageAt': 100.0},
            'gateway': {'bound': True, 'lastPongAt': 100.0},
            'outbox': {'pendingCount': 0, 'blockedCount': 0},
        }
        live.xianyu.search_seller_items.return_value = {
            'ret': ['FAIL_SYS_SESSION_EXPIRED::Session过期'],
        }
        thread = Mock()
        thread.is_alive.return_value = True
        gateway_check = Mock(return_value={'stores': [{
            'storeId': 203,
            'instanceId': 'store-a',
            'authorized': True,
            'connectionCount': 1,
            'deviceCount': 1,
            'executor': {'active': True, 'ownedByCurrentDevice': True},
            'recentTasks': [],
        }]})
        diagnostics = self._diagnostics(
            instances=[instance], lives={'store-a': live},
            threads={'store-a': thread}, gateway_check=gateway_check,
        )
        diagnostics._local_check_specs = Mock(return_value=[])
        diagnostics._local_api_item = Mock(return_value=diagnostic_item(
            'local_api', '本地接口', 'normal', '正常'
        ))

        result = diagnostics.run()

        items = {item['key']: item for item in result['stores'][0]['items']}
        self.assertEqual(items['cookie_login']['status'], 'error')
        self.assertEqual(items['goods_capability']['status'], 'unknown')

    def test_multi_store_gateway_results_do_not_cross(self):
        instance_a = {'id': 'store-a', 'storeId': 203, 'platformShopId': 'seller-a'}
        instance_b = {'id': 'store-b', 'storeId': 204, 'platformShopId': 'seller-b'}
        gateway_check = Mock(return_value={'stores': [
            {
                'storeId': 204,
                'instanceId': 'store-b',
                'authorized': True,
                'connectionCount': 2,
                'deviceCount': 2,
                'executor': {
                    'active': True,
                    'ownedByCurrentDevice': False,
                    'instanceId': 'other',
                },
                'recentTasks': [{
                    'taskId': 'failed-task',
                    'taskType': 'sendMessage',
                    'status': 'failed',
                    'success': False,
                    'errorMessage': 'executor failed',
                }],
            },
            {
                'storeId': 203,
                'instanceId': 'store-a',
                'authorized': True,
                'connectionCount': 1,
                'deviceCount': 1,
                'executor': {
                    'active': True,
                    'ownedByCurrentDevice': True,
                    'instanceId': 'store-a',
                },
                'recentTasks': [{
                    'taskId': 'success-task',
                    'taskType': 'queryOrder',
                    'status': 'succeeded',
                    'success': True,
                }],
            },
        ]})
        diagnostics = self._diagnostics(
            instances=[instance_a, instance_b], gateway_check=gateway_check,
        )
        diagnostics._local_check_specs = Mock(return_value=[])

        result = diagnostics.run()

        stores = {store['instanceId']: store for store in result['stores']}
        items_a = {item['key']: item for item in stores['store-a']['items']}
        items_b = {item['key']: item for item in stores['store-b']['items']}
        self.assertEqual(items_a['executor']['status'], 'normal')
        self.assertEqual(items_a['devices']['status'], 'normal')
        self.assertEqual(items_a['recent_tasks']['status'], 'normal')
        self.assertEqual(items_b['executor']['status'], 'error')
        self.assertEqual(items_b['devices']['status'], 'error')
        self.assertEqual(items_b['recent_tasks']['status'], 'error')
        self.assertIn('failed-task', items_b['recent_tasks']['detail'])

    def test_store_checks_start_in_parallel(self):
        instances = [
            {'id': 'store-a', 'storeId': 203},
            {'id': 'store-b', 'storeId': 204},
        ]
        diagnostics = self._diagnostics(instances=instances)
        barrier = threading.Barrier(2)

        def check(instance, index, refreshed_auth, gateway_stores, gateway_available):
            barrier.wait(timeout=0.5)
            return {
                'instanceId': instance['id'], 'storeId': instance['storeId'],
                'name': instance['id'], 'status': 'normal',
                'counts': summarize([])['counts'], 'items': [],
            }

        diagnostics._diagnose_store_local = check

        stores = diagnostics._diagnose_stores_parallel()

        self.assertEqual([store['instanceId'] for store in stores], ['store-a', 'store-b'])

    def test_local_gateway_and_store_groups_start_in_parallel(self):
        diagnostics = self._diagnostics(instances=[{'id': 'store-a', 'storeId': 203}])
        barrier = threading.Barrier(3)

        diagnostics._local_check_specs = Mock(return_value=[(
            'local', '本机', lambda: (
                barrier.wait(timeout=0.5), diagnostic_item('local', '本机', 'normal')
            )[1], 1,
        )])
        diagnostics._load_gateway_diagnostics = Mock(side_effect=lambda: (
            barrier.wait(timeout=0.5), {
                'auth': {'accessToken': 'token', 'scope': {'stores': [{'id': 203}]}},
                'authError': None, 'data': {'stores': []}, 'error': None,
            }
        )[1])
        diagnostics._diagnose_stores_parallel = Mock(side_effect=lambda: (
            barrier.wait(timeout=0.5), [{
                'instanceId': 'store-a', 'storeId': 203, 'name': 'A',
                'status': 'normal', 'counts': summarize([])['counts'], 'items': [],
            }]
        )[1])

        result = diagnostics.run()

        self.assertEqual(result['local'][0]['status'], 'normal')


if __name__ == '__main__':
    unittest.main()
