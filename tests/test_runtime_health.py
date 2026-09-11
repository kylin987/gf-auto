import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from urllib.request import urlopen

from event_outbox import EventOutbox
from goofish_live import XianyuLive
from local_api import LocalApiServer
from ws_client import GatewayClient, GatewayLoginError


class FakeBindWebSocket:
    def __init__(self, client=None, stop_event=None, bind_success=True):
        self.client = client
        self.stop_event = stop_event
        self.bind_success = bind_success
        self.messages = []
        self.bound_snapshot = None

    async def send(self, raw):
        self.messages.append(json.loads(raw))

    async def recv(self):
        return json.dumps({
            'type': 'client.bind.ack',
            'payload': {
                'success': self.bind_success,
                'executor': {'generation': 7},
                'reason': 'denied',
            },
        })

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.stop_event is not None:
            self.stop_event.set()
        if self.client is not None:
            self.bound_snapshot = self.client.health_snapshot()
        raise StopAsyncIteration


class GatewayRuntimeHealthTest(unittest.IsolatedAsyncioTestCase):
    def test_gateway_health_requires_completed_bind(self):
        client = GatewayClient.__new__(GatewayClient)
        client.ws = object()
        client.gateway_bound = False
        client.bound_at = 0.0
        client.last_pong_at = 0.0
        client.executor_generation = 0

        snapshot = client.health_snapshot()

        self.assertTrue(snapshot['connected'])
        self.assertFalse(snapshot['bound'])

    def test_disconnected_gateway_cannot_report_bound(self):
        client = GatewayClient.__new__(GatewayClient)
        client.ws = None
        client.gateway_bound = True
        client.bound_at = 100.0
        client.last_pong_at = 110.0
        client.executor_generation = 1

        snapshot = client.health_snapshot()

        self.assertFalse(snapshot['connected'])
        self.assertFalse(snapshot['bound'])

    async def test_bind_ack_and_pong_update_gateway_health(self):
        client = GatewayClient('token', store_id=203, instance_id='store-a')
        websocket = FakeBindWebSocket()
        client.ws = websocket

        with patch('ws_client.time.time', return_value=100.0):
            await client._bind(websocket)

        self.assertTrue(client.health_snapshot()['bound'])
        self.assertEqual(client.health_snapshot()['boundAt'], 100.0)
        self.assertEqual(client.health_snapshot()['executorGeneration'], 7)

        with patch('ws_client.time.time', return_value=120.0):
            await client._handle_message(json.dumps({
                'type': 'server.pong',
                'payload': {'executor': {'generation': 8}},
            }))

        snapshot = client.health_snapshot()
        self.assertEqual(snapshot['lastPongAt'], 120.0)
        self.assertEqual(snapshot['executorGeneration'], 8)

    async def test_failed_rebind_clears_previous_bound_state(self):
        client = GatewayClient('token')
        client.gateway_bound = True
        client.bound_at = 80.0

        with self.assertRaisesRegex(GatewayLoginError, 'denied'):
            await client._bind(FakeBindWebSocket(bind_success=False))

        self.assertFalse(client.health_snapshot()['bound'])
        self.assertEqual(client.health_snapshot()['boundAt'], 80.0)

    async def test_disconnect_clears_gateway_connection_state(self):
        stop_event = threading.Event()
        client = GatewayClient('token', stop_event=stop_event)
        websocket = FakeBindWebSocket(client=client, stop_event=stop_event)

        with patch('ws_client.websockets.connect', return_value=websocket), \
                patch('ws_client.time.time', return_value=100.0):
            await client.run()

        self.assertTrue(websocket.bound_snapshot['connected'])
        self.assertTrue(websocket.bound_snapshot['bound'])
        snapshot = client.health_snapshot()
        self.assertFalse(snapshot['connected'])
        self.assertFalse(snapshot['bound'])
        self.assertEqual(snapshot['boundAt'], 100.0)


class XianyuRuntimeHealthTest(unittest.IsolatedAsyncioTestCase):
    async def test_sending_im_registration_does_not_mark_registered(self):
        live = XianyuLive.__new__(XianyuLive)
        live.access_token = 'access-token'
        live.device_id = 'device-id'
        live.im_registered = False
        live.im_registered_at = 0.0
        live.im_registration_mid = ''
        websocket = Mock()
        websocket.send = AsyncMock()

        with patch('goofish_live.generate_mid', return_value='reg-mid'):
            await live._initialize_im_connection(websocket)

        sent = json.loads(websocket.send.await_args.args[0])
        self.assertEqual(sent['lwp'], '/reg')
        self.assertEqual(sent['headers']['mid'], 'reg-mid')
        self.assertFalse(live.im_registered)
        self.assertEqual(live.im_registered_at, 0.0)
        self.assertEqual(live.im_registration_mid, 'reg-mid')

    async def test_failed_im_init_does_not_mark_registered(self):
        live = XianyuLive.__new__(XianyuLive)
        live.im_registered = True
        live.im_registered_at = 150.0
        live.im_registration_mid = 'old-mid'
        live.init = AsyncMock(side_effect=RuntimeError('init failed'))

        with self.assertRaisesRegex(RuntimeError, 'init failed'):
            await live._initialize_im_connection(object())

        self.assertFalse(live.im_registered)
        self.assertEqual(live.im_registered_at, 150.0)
        self.assertEqual(live.im_registration_mid, '')

    def test_only_matching_registration_mid_marks_im_registered_for_code_200(self):
        live = XianyuLive.__new__(XianyuLive)
        live.im_registered = False
        live.im_registered_at = 0.0
        live.im_registration_mid = 'reg-mid'
        live.last_im_message_at = 0.0

        with patch('goofish_live.time.time', return_value=220.0):
            live._record_im_message({'code': 200, 'headers': {'mid': 'other-mid'}})

        self.assertFalse(live.im_registered)
        self.assertEqual(live.im_registered_at, 0.0)
        self.assertEqual(live.last_im_message_at, 220.0)

        with patch('goofish_live.time.time', return_value=221.0):
            live._record_im_message({'code': 200, 'headers': {'mid': 'reg-mid'}})

        self.assertTrue(live.im_registered)
        self.assertEqual(live.im_registered_at, 221.0)
        self.assertEqual(live.last_im_message_at, 221.0)

    def test_valid_sync_frame_marks_im_registered(self):
        live = XianyuLive.__new__(XianyuLive)
        live.im_registered = False
        live.im_registered_at = 0.0
        live.im_registration_mid = 'reg-mid'
        live.last_im_message_at = 0.0

        with patch('goofish_live.time.time', return_value=225.0):
            live._record_im_message({
                'lwp': '/s/sync',
                'headers': {},
                'body': {'syncPushPackage': {'data': []}},
            })

        self.assertTrue(live.im_registered)
        self.assertEqual(live.im_registered_at, 225.0)
        self.assertEqual(live.last_im_message_at, 225.0)

    def test_auth_rejection_does_not_mark_im_registered(self):
        rejected_frames = [
            {'code': 401, 'headers': {}},
            {'code': 200, 'lwp': '/push/kickout', 'headers': {}},
        ]
        for frame in rejected_frames:
            with self.subTest(frame=frame):
                live = XianyuLive.__new__(XianyuLive)
                live.im_registered = False
                live.im_registered_at = 0.0
                live.im_registration_mid = 'reg-mid'
                live.last_im_message_at = 0.0

                with patch('goofish_live.time.time', return_value=230.0):
                    live._record_im_message(frame)

                self.assertFalse(live.im_registered)
                self.assertEqual(live.im_registered_at, 0.0)
                self.assertEqual(live.im_registration_mid, '')
                self.assertEqual(live.last_im_message_at, 230.0)

    def test_disconnect_clears_im_registered_but_keeps_last_health(self):
        live = XianyuLive.__new__(XianyuLive)
        live.im_registered = True
        live.im_registered_at = 200.0
        live.im_registration_mid = 'reg-mid'
        live.last_im_message_at = 220.0

        live._clear_im_connection_health()

        self.assertFalse(live.im_registered)
        self.assertEqual(live.im_registration_mid, '')
        self.assertEqual(live.im_registered_at, 200.0)
        self.assertEqual(live.last_im_message_at, 220.0)

    def test_disconnected_im_cannot_report_registered(self):
        live = XianyuLive.__new__(XianyuLive)
        live.instance_id = 'store-a'
        live.store_id = 203
        live.instance_name = '店铺 A'
        live.local_api_port = 8010
        live.ws = None
        live.im_registered = True
        live.im_registered_at = 200.0
        live.last_im_message_at = 220.0
        live.is_login_ready = Mock(return_value=True)
        live.current_chrome_account = Mock(return_value={'nick': 'seller-a', 'userId': 'seller-1'})
        live.ws_client = None
        live.outbox = None

        snapshot = live.health_snapshot()

        self.assertFalse(snapshot['im']['connected'])
        self.assertFalse(snapshot['im']['registered'])

    def test_health_snapshot_is_read_only_and_combines_runtime_state(self):
        live = XianyuLive.__new__(XianyuLive)
        live.instance_id = 'store-a'
        live.store_id = 203
        live.instance_name = '店铺 A'
        live.local_api_port = 8010
        live.ws = object()
        live.im_registered = True
        live.im_registered_at = 200.0
        live.last_im_message_at = 220.0
        live.is_login_ready = Mock(return_value=True)
        live.current_chrome_account = Mock(return_value={
            'nick': 'seller-a',
            'userId': 'seller-1',
        })
        live.check_login = Mock(side_effect=AssertionError('must not check login'))
        live.ws_client = Mock()
        live.ws_client.health_snapshot.return_value = {
            'connected': True,
            'bound': True,
            'boundAt': 210.0,
            'lastPongAt': 230.0,
            'executorGeneration': 3,
        }
        live.outbox = Mock()
        live.outbox.status_summary.return_value = {
            'pendingCount': 1,
            'blockedCount': 0,
            'oldestPendingAt': 190.0,
            'oldestPendingAge': 50.0,
        }

        snapshot = live.health_snapshot()

        self.assertEqual(snapshot['instanceId'], 'store-a')
        self.assertEqual(snapshot['storeId'], 203)
        self.assertEqual(snapshot['localApiPort'], 8010)
        self.assertTrue(snapshot['loginReady'])
        self.assertEqual(snapshot['account']['userId'], 'seller-1')
        self.assertTrue(snapshot['im']['connected'])
        self.assertTrue(snapshot['im']['registered'])
        self.assertTrue(snapshot['gateway']['bound'])
        self.assertEqual(snapshot['outbox']['pendingCount'], 1)
        live.check_login.assert_not_called()


class EventOutboxRuntimeHealthTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / 'event_outbox.sqlite3'

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_status_summary_counts_blocked_and_oldest_pending_age(self):
        outbox = EventOutbox(self.database_path)
        with patch('event_outbox.time.time', return_value=900.0):
            outbox.enqueue('pending-1', {'id': 'pending-1'})
        with patch('event_outbox.time.time', return_value=950.0):
            outbox.enqueue('blocked-1', {'id': 'blocked-1'})
            outbox.mark_blocked('blocked-1', 'rejected')

        summary = outbox.status_summary(now=1_000.0)

        self.assertEqual(summary, {
            'pendingCount': 1,
            'blockedCount': 1,
            'oldestPendingAt': 900.0,
            'oldestPendingAge': 100.0,
        })

    def test_empty_status_summary_uses_zero_values(self):
        outbox = EventOutbox(self.database_path)

        self.assertEqual(outbox.status_summary(now=1_000.0), {
            'pendingCount': 0,
            'blockedCount': 0,
            'oldestPendingAt': 0.0,
            'oldestPendingAge': 0.0,
        })


class LocalApiRuntimeHealthTest(unittest.TestCase):
    def test_health_preserves_old_field_and_exposes_completion_state(self):
        live = Mock()
        live.health_snapshot.return_value = {
            'instanceId': 'store-a',
            'im': {'connected': True, 'registered': True},
            'gateway': {'bound': False},
        }
        server = LocalApiServer(('127.0.0.1', 0), live)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            with urlopen(f'http://{host}:{port}/health', timeout=2) as response:
                payload = json.loads(response.read().decode('utf-8'))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(payload, {
            'status': 'ok',
            'ws_connected': True,
            'instanceId': 'store-a',
            'imRegistered': True,
            'gatewayBound': False,
        })


if __name__ == '__main__':
    unittest.main()
