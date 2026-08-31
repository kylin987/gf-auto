import threading
import unittest
from unittest.mock import Mock, patch

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
        live.ensure_login = Mock(return_value=True)
        live._set_login_invalid = Mock()
        live._request_im_reconnect = Mock()

        self.assertTrue(live.relogin('code=401', expected_revision=2))
        live.ensure_login.assert_called_once_with(force=True)
        live._request_im_reconnect.assert_called_once_with()

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
