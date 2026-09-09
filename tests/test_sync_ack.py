import unittest

from goofish_live import XianyuLive


class SyncAckTest(unittest.TestCase):
    def test_regular_sync_does_not_require_diff_ack(self):
        message = {
            'lwp': '/s/sync',
            'body': {'syncPushPackage': {'data': []}},
        }

        self.assertFalse(XianyuLive._needs_sync_diff_ack(message))

    def test_oversized_sync_requires_diff_ack(self):
        for sync_type in (1, 2, '1', '2'):
            with self.subTest(sync_type=sync_type):
                message = {
                    'lwp': '/s/sync',
                    'body': {
                        'syncExtraType': {'type': sync_type},
                        'syncPushPackage': {'data': []},
                    },
                }

                self.assertTrue(XianyuLive._needs_sync_diff_ack(message))

    def test_other_sync_extra_type_does_not_require_diff_ack(self):
        message = {
            'lwp': '/s/sync',
            'body': {'syncExtraType': {'type': 3}},
        }

        self.assertFalse(XianyuLive._needs_sync_diff_ack(message))


if __name__ == '__main__':
    unittest.main()
