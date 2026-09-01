import threading
import unittest
from unittest.mock import Mock, call, patch

import dashboard
from cookie_auth import fetch_cookies_via_chrome
from goofish_live import XianyuLive


class ControlledThread:
    def __init__(self, target=None, args=(), **kwargs):
        self.target = target
        self.args = args
        self.alive = False

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive


class InstanceLifecycleTest(unittest.TestCase):
    @staticmethod
    def _app():
        app = dashboard.XianyuDesktopApp.__new__(dashboard.XianyuDesktopApp)
        app.root = Mock()
        app.instances = []
        app.states = {}
        app.lives = {}
        app.run_threads = {}
        app.run_generations = {}
        app.pub_id = 101
        app._event = Mock()
        app._refresh_current_view = Mock()
        return app

    def test_same_store_cannot_start_again_until_previous_thread_exits(self):
        app = self._app()
        instance = {'id': 'store-a', 'storeId': 203}
        app.instances = [instance]
        threads = []

        def make_thread(**kwargs):
            thread = ControlledThread(**kwargs)
            threads.append(thread)
            return thread

        with patch.object(dashboard.threading, 'Thread', side_effect=make_thread):
            app._start_instance(instance)
            app._start_instance(instance)
            self.assertEqual(len(threads), 1)

            app._stop_instance(instance)
            app._start_instance(instance)
            self.assertEqual(len(threads), 1)
            self.assertEqual(app.states['store-a']['status'], 'stopping')

            threads[0].alive = False
            app._start_instance(instance)

        self.assertEqual(len(threads), 2)
        self.assertEqual(app.run_generations['store-a'], 2)

    def test_old_run_cannot_own_new_store_instance(self):
        app = self._app()
        old_thread = object()
        current_thread = object()
        app.run_generations['store-a'] = 2
        app.run_threads['store-a'] = current_thread

        self.assertFalse(app._is_current_run('store-a', 1, old_thread))
        self.assertTrue(app._is_current_run('store-a', 2, current_thread))

    def test_two_stores_keep_separate_run_threads(self):
        app = self._app()
        store_a = {'id': 'store-a', 'storeId': 29}
        store_b = {'id': 'store-b', 'storeId': 203}
        app.instances = [store_a, store_b]

        with patch.object(dashboard.threading, 'Thread', side_effect=ControlledThread):
            app._start_instance(store_a)
            app._start_instance(store_b)

        self.assertIsNot(app.run_threads['store-a'], app.run_threads['store-b'])
        self.assertEqual(app.run_generations, {'store-a': 1, 'store-b': 1})

    @patch('dashboard.save_instances')
    @patch('ws_client.gateway_bind_xianyu_store')
    def test_empty_platform_shop_id_is_bound_after_chrome_login(self, bind_store, save):
        app = self._app()
        instance = {'id': 'store-a', 'storeId': 203, 'platformShopId': ''}
        app.instances = [instance]
        app.gateway_auth = {
            'accessToken': 'token',
            'scope': {'stores': [{'id': 203, 'platformShopId': ''}]},
        }
        app.gateway_auth_manager = Mock()
        bind_store.return_value = {
            'storeId': 203,
            'platformShopId': '436176214',
            'bound': True,
            'accessToken': 'bound-token',
            'scope': {
                'stores': [{'id': 203, 'platformShopId': '436176214'}],
            },
        }

        result = app._bind_platform_shop_id(instance, '436176214')

        self.assertEqual(result, '436176214')
        self.assertEqual(instance['platformShopId'], '436176214')
        self.assertEqual(app.gateway_auth['accessToken'], 'bound-token')
        self.assertEqual(app.gateway_auth['scope']['stores'][0]['platformShopId'], '436176214')
        save.assert_called_once_with(app.instances, app.pub_id)

    def test_stale_im_401_does_not_open_chrome_again(self):
        live = XianyuLive.__new__(XianyuLive)
        live._relogin_lock = threading.Lock()
        live._login_state_lock = threading.RLock()
        live._stop_event = threading.Event()
        live._login_revision = 2
        live.ensure_login = Mock(return_value=True)
        live._set_login_invalid = Mock()
        live._request_im_reconnect = Mock()

        self.assertTrue(live.relogin('code=401', expected_revision=1))
        live.ensure_login.assert_not_called()
        live._set_login_invalid.assert_not_called()

    def test_current_im_401_refreshes_once(self):
        live = XianyuLive.__new__(XianyuLive)
        live._relogin_lock = threading.Lock()
        live._login_state_lock = threading.RLock()
        live._stop_event = threading.Event()
        live._login_revision = 2
        live._recover_saved_im_login = Mock(return_value=False)
        live.ensure_login = Mock(return_value=True)
        live._set_login_invalid = Mock()
        live._request_im_reconnect = Mock()

        self.assertTrue(live.relogin('code=401', expected_revision=2))
        live.ensure_login.assert_called_once_with(force=True)
        live._request_im_reconnect.assert_called_once_with()

    def test_current_im_401_recovers_saved_cookie_before_opening_chrome(self):
        live = XianyuLive.__new__(XianyuLive)
        live._relogin_lock = threading.Lock()
        live._login_state_lock = threading.RLock()
        live._stop_event = threading.Event()
        live._login_revision = 2
        live._recover_saved_im_login = Mock(return_value=True)
        live.ensure_login = Mock(return_value=True)
        live._set_login_invalid = Mock()
        live._request_im_reconnect = Mock()

        self.assertTrue(live.relogin('code=401', expected_revision=2))
        live.ensure_login.assert_not_called()
        live._set_login_invalid.assert_not_called()
        live._request_im_reconnect.assert_called_once_with()

    def test_network_error_during_saved_cookie_recovery_does_not_open_chrome(self):
        live = XianyuLive.__new__(XianyuLive)
        live._relogin_lock = threading.Lock()
        live._login_state_lock = threading.RLock()
        live._stop_event = threading.Event()
        live._login_revision = 2
        live._recover_saved_im_login = Mock(return_value=None)
        live.ensure_login = Mock(return_value=True)
        live._set_login_invalid = Mock()
        live._request_im_reconnect = Mock()

        self.assertFalse(live.relogin('network', expected_revision=2))
        live.ensure_login.assert_not_called()
        live._set_login_invalid.assert_not_called()
        live._request_im_reconnect.assert_not_called()

    @patch('goofish_live.time.monotonic', side_effect=[100.0, 100.0, 120.0])
    def test_failed_auto_login_starts_cooldown(self, monotonic):
        live = XianyuLive.__new__(XianyuLive)
        live._relogin_lock = threading.Lock()
        live._login_state_lock = threading.RLock()
        live._stop_event = threading.Event()
        live._login_revision = 2
        live._auto_login_cooldown = 900
        live._auto_login_retry_at = 0.0
        live._auto_login_notice_at = 0.0
        live._recover_saved_im_login = Mock(return_value=False)
        live.ensure_login = Mock(return_value=False)
        live._set_login_invalid = Mock()

        self.assertFalse(live.relogin('session expired', expected_revision=2))
        self.assertFalse(live.relogin('session expired', expected_revision=2))

        live.ensure_login.assert_called_once_with(force=True)
        self.assertEqual(live._auto_login_retry_at, 1000.0)

    @patch('goofish_live.time.monotonic', return_value=120.0)
    def test_cooldown_still_allows_saved_cookie_recovery(self, monotonic):
        live = XianyuLive.__new__(XianyuLive)
        live._relogin_lock = threading.Lock()
        live._login_state_lock = threading.RLock()
        live._stop_event = threading.Event()
        live._login_revision = 2
        live._auto_login_retry_at = 1000.0
        live._recover_saved_im_login = Mock(return_value=True)
        live.ensure_login = Mock(return_value=True)
        live._set_login_invalid = Mock()
        live._request_im_reconnect = Mock()

        self.assertTrue(live.relogin('session expired', expected_revision=2))

        live.ensure_login.assert_not_called()
        live._request_im_reconnect.assert_called_once_with()

    @patch('goofish_live.fetch_cookies_via_chrome', side_effect=TimeoutError('等待手动登录/获取 token 超时'))
    @patch('goofish_live.check_chrome_installed', return_value=(True, 'chrome.exe', ''))
    def test_chrome_login_timeout_keeps_login_required_state(self, installed, fetch_cookies):
        live = XianyuLive.__new__(XianyuLive)
        live._stop_event = threading.Event()
        live.login_timeout = 300
        live.chrome_profile_dir = '/tmp/store-a-profile'
        live.login_state_callback = Mock()

        self.assertFalse(live.ensure_login(force=True))

        self.assertEqual(live.login_state_callback.call_args_list, [
            call('login_required', '已打开独立 Chrome，请在 5 分钟内完成登录'),
            call('login_required', '登录超时，Chrome 窗口已关闭，等待自动重试'),
        ])

    def test_saved_cookie_recovery_reports_running_state(self):
        live = XianyuLive.__new__(XianyuLive)
        live.xianyu = Mock()
        live.xianyu.get_token.return_value = {
            'ret': ['SUCCESS::调用成功'],
            'data': {'accessToken': 'new-token'},
        }
        live.access_token = 'old-token'
        live._login_state_lock = threading.RLock()
        live._login_revision = 1
        live._sync_saved_session = Mock()
        live.login_state_callback = Mock()

        self.assertTrue(live._recover_saved_im_login())

        live.login_state_callback.assert_called_once_with('running', '登录态已静默恢复，正在监听消息')

    @patch('dashboard.messagebox.showwarning')
    def test_login_required_warning_is_shown_once_until_recovered(self, warning):
        app = self._app()
        instance = {'id': 'store-a', 'name': '小影票务'}
        app.instances = [instance]
        app.run_generations['store-a'] = 1
        app.states['store-a'] = {'status': 'running', 'hint': '正在监听消息'}

        app._apply_login_state(instance, 1, 'login_required', '已打开独立 Chrome，请在 5 分钟内完成登录')
        app._apply_login_state(instance, 1, 'login_required', 'Chrome 窗口已关闭，需要登录，15 分钟后自动重试')

        self.assertEqual(app.states['store-a']['status'], 'login_required')
        self.assertEqual(app.states['store-a']['hint'], 'Chrome 窗口已关闭，需要登录，15 分钟后自动重试')
        warning.assert_called_once()

        app._apply_login_state(instance, 1, 'running', '登录态已静默恢复，正在监听消息')
        app._apply_login_state(instance, 1, 'login_required', '已打开独立 Chrome，请在 5 分钟内完成登录')
        self.assertEqual(warning.call_count, 2)

    def test_login_required_instance_can_be_stopped(self):
        app = self._app()
        instance = {'id': 'store-a'}
        app.instances = [instance]
        app.states['store-a'] = {'status': 'login_required'}
        app._stop_instance = Mock()

        app._toggle_instance(instance)

        app._stop_instance.assert_called_once_with(instance)

    @patch('cookie_auth.subprocess.Popen')
    @patch('cookie_auth.find_chrome_binary', return_value='chrome.exe')
    def test_stopping_store_cancels_chrome_login_poll(self, find_chrome, popen):
        stop_event = threading.Event()
        stop_event.set()
        process = popen.return_value

        with self.assertRaises(InterruptedError):
            fetch_cookies_via_chrome(
                timeout=300,
                profile_dir='/tmp/store-a-profile',
                stop_event=stop_event,
            )

        process.terminate.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
