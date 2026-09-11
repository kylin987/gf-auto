import unittest
from unittest.mock import Mock, patch

import dashboard


class ControlledThread:
    def __init__(self, target=None, args=(), **kwargs):
        self.target = target
        self.args = args
        self.started = False

    def start(self):
        self.started = True


class StatusDiagnosticsDashboardTest(unittest.TestCase):
    @staticmethod
    def _app():
        app = dashboard.XianyuDesktopApp.__new__(dashboard.XianyuDesktopApp)
        app.root = Mock()
        app.gateway_auth_manager = Mock()
        app.gateway_auth = {'accessToken': 'token'}
        app.instances = [
            {'id': 'store-a', 'storeId': 203, 'name': '店铺 A'},
            {'id': 'store-b', 'storeId': 204, 'name': '店铺 B'},
        ]
        app.lives = {}
        app.run_threads = {}
        app.pub_id = 101
        app.current_view = 'diagnostics'
        app.diagnostics_running = False
        app.diagnostics_result = None
        app.diagnostics_error = ''
        app._clear_main = Mock()
        app._navigate_button = Mock()
        app._render_diagnostics_page = Mock()
        return app

    def test_navigation_places_diagnostics_before_update(self):
        labels = [label for _, label in dashboard.WORKBENCH_NAV_ITEMS + dashboard.CLIENT_NAV_ITEMS]

        self.assertEqual(labels, [
            '运行概览', '店铺管理', '运行日志', '状态检测', '软件更新', '设置',
        ])

    def test_opening_page_does_not_start_detection(self):
        app = self._app()
        app._start_status_diagnostics = Mock()

        app._show_diagnostics()

        app._start_status_diagnostics.assert_not_called()
        app._render_diagnostics_page.assert_called_once_with()

    def test_duplicate_click_is_ignored(self):
        app = self._app()
        app.diagnostics_running = True

        with patch.object(dashboard.threading, 'Thread') as thread:
            app._start_status_diagnostics()

        thread.assert_not_called()

    def test_click_starts_one_background_worker(self):
        app = self._app()
        threads = []

        def make_thread(**kwargs):
            thread = ControlledThread(**kwargs)
            threads.append(thread)
            return thread

        with patch.object(dashboard.threading, 'Thread', side_effect=make_thread):
            app._start_status_diagnostics()

        self.assertTrue(app.diagnostics_running)
        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0].started)
        self.assertIs(threads[0].target.__self__, app)
        self.assertIs(
            threads[0].target.__func__,
            dashboard.XianyuDesktopApp._status_diagnostics_worker,
        )

    def test_worker_finishes_through_root_after(self):
        app = self._app()
        result = {'status': 'normal', 'counts': {}, 'local': [], 'stores': []}
        diagnostics = Mock()
        diagnostics.run.return_value = result
        app._finish_status_diagnostics = Mock()

        with patch.object(dashboard, 'StatusDiagnostics', return_value=diagnostics):
            app._status_diagnostics_worker()

        app.root.after.assert_called_once()
        delay, callback = app.root.after.call_args.args
        self.assertEqual(delay, 0)
        app._finish_status_diagnostics.assert_not_called()
        callback()
        app._finish_status_diagnostics.assert_called_once_with(result, None)

    def test_multi_store_results_are_kept_in_separate_groups(self):
        result = {
            'local': [{'key': 'version', 'title': '客户端版本'}],
            'gateway': {'key': 'gateway', 'title': 'Gateway'},
            'stores': [
                {'instanceId': 'store-a', 'name': '店铺 A', 'items': [{'key': 'a'}]},
                {'instanceId': 'store-b', 'name': '店铺 B', 'items': [{'key': 'b'}]},
            ],
        }

        groups = dashboard.XianyuDesktopApp._diagnostic_groups(result)

        self.assertEqual([group['key'] for group in groups], ['local', 'store-a', 'store-b'])
        self.assertEqual(groups[1]['items'], [{'key': 'a'}])
        self.assertEqual(groups[2]['items'], [{'key': 'b'}])

    def test_actions_only_call_existing_explicit_handlers(self):
        app = self._app()
        app._start_instance = Mock()
        app._force_relogin = Mock()
        app._navigate = Mock()
        app._start_update_check = Mock()

        app._run_diagnostics_action('start_instance', app.instances[0], {'detail': '启动'})
        app._run_diagnostics_action('relogin', app.instances[1], {'detail': '登录'})
        app._run_diagnostics_action('check_update', None, {'detail': '更新'})

        app._start_instance.assert_called_once_with(app.instances[0])
        app._force_relogin.assert_called_once_with(app.instances[1])
        app._navigate.assert_called_once_with('update')
        app._start_update_check.assert_called_once_with()

        app._run_diagnostics_action('unsupported', app.instances[0], {'detail': '禁止'})
        self.assertEqual(app._start_instance.call_count, 1)
        self.assertEqual(app._force_relogin.call_count, 1)

    @patch.object(dashboard.messagebox, 'showinfo')
    def test_view_advice_only_displays_the_selected_result(self, showinfo):
        app = self._app()

        app._run_diagnostics_action('view_advice', app.instances[0], {
            'title': '在线设备',
            'detail': '请关闭其他电脑上的同一实例',
        })

        showinfo.assert_called_once_with(
            '在线设备',
            '请关闭其他电脑上的同一实例',
            parent=app.root,
        )


if __name__ == '__main__':
    unittest.main()
