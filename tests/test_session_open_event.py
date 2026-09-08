import asyncio
import time
import unittest
from unittest.mock import AsyncMock, Mock

from goofish_live import XianyuLive


class SessionOpenEventTest(unittest.TestCase):
    def test_fresh_new_session_arouse_is_reported_to_gateway(self):
        now_ms = int(time.time() * 1000)
        live = XianyuLive.__new__(XianyuLive)
        live._seen_structures = set()
        live._last_sync_log_at = 0
        live._save_raw_message = Mock()
        live.store_id = 203
        live.ws_client = Mock()
        live.ws_client.send = AsyncMock(return_value=True)
        session_event = {
            'operation': {
                'content': {
                    'contentType': 8,
                    'sessionArouse': {
                        'memberFlags': 1,
                        'sessionArouseInfo': {'arouseTimeStamp': now_ms},
                    },
                },
                'sessionInfo': {
                    'createTime': now_ms - 1000,
                    'sessionId': '63655953794',
                    'extensions': {
                        'extUserId': '2803308075',
                        'itemId': '1064670207000',
                        'itemTitle': '全国电影票',
                    },
                },
            },
            'sessionId': '63655953794',
        }

        asyncio.run(live.handle_message({
            'body': {'syncPushPackage': {'data': [{'data': session_event}]}},
        }, None))

        payload = live.ws_client.send.await_args.args[0]
        self.assertEqual(payload['eventName'], 'session_opened')
        self.assertEqual(payload['contentType'], 8)
        self.assertEqual(payload['cid'], '63655953794')
        self.assertEqual(payload['senderUserId'], '2803308075')
        self.assertEqual(payload['itemId'], '1064670207000')
        self.assertEqual(
            live.ws_client.send.await_args.kwargs['dedupe_key'],
            'session_opened:203:63655953794',
        )

    def test_stale_or_existing_session_arouse_is_not_reported(self):
        now_ms = int(time.time() * 1000)
        live = XianyuLive.__new__(XianyuLive)
        stale = self._session_event(now_ms - 300000, now_ms - 301000, 'old-session', 'buyer-1')
        existing = self._session_event(now_ms, now_ms - 300000, 'existing-session', 'buyer-2')

        self.assertIsNone(live._simplify_session_opened(stale, now_ms=now_ms))
        self.assertIsNone(live._simplify_session_opened(existing, now_ms=now_ms))

    @staticmethod
    def _session_event(arouse_time, create_time, session_id, buyer_id):
        return {
            'operation': {
                'content': {
                    'contentType': 8,
                    'sessionArouse': {
                        'memberFlags': 1,
                        'sessionArouseInfo': {'arouseTimeStamp': arouse_time},
                    },
                },
                'sessionInfo': {
                    'createTime': create_time,
                    'sessionId': session_id,
                    'extensions': {'extUserId': buyer_id},
                },
            },
        }


if __name__ == '__main__':
    unittest.main()
