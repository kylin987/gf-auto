import asyncio
import time
import unittest
from unittest.mock import AsyncMock, Mock

from goofish_live import XianyuLive


class SessionOpenEventTest(unittest.TestCase):
    def test_session_arouse_is_reported_to_gateway(self):
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
            'session_opened:203:2803308075:1064670207000',
        )
        self.assertEqual(
            payload['messageId'],
            'xianyu_session_opened_63655953794_1064670207000',
        )

    def test_existing_session_arouse_is_reported_and_outbox_handles_freshness(self):
        now_ms = int(time.time() * 1000)
        live = XianyuLive.__new__(XianyuLive)
        existing = self._session_event(now_ms, now_ms - 300000, 'existing-session', 'buyer-2')

        payload = live._simplify_session_opened(existing)

        self.assertEqual(payload['sessionId'], 'existing-session')
        self.assertEqual(payload['buyerId'], 'buyer-2')
        self.assertEqual(payload['time'], str(now_ms))

    def test_seller_side_session_helper_is_not_reported(self):
        now_ms = int(time.time() * 1000)
        live = XianyuLive.__new__(XianyuLive)
        seller_helper = self._session_event(now_ms, now_ms, 'session-1', 'buyer-1')
        seller_helper['operation']['content']['sessionArouse']['memberFlags'] = 0

        self.assertIsNone(live._simplify_session_opened(seller_helper))

    def test_ignored_session_and_status_events_are_saved_for_diagnosis(self):
        now_ms = int(time.time() * 1000)
        live = XianyuLive.__new__(XianyuLive)
        live._seen_structures = set()
        live._seen_status_events = set()
        live._last_sync_log_at = time.time()
        live._save_raw_message = Mock()
        live._save_sync_diagnostic = Mock()
        live.ws_client = None
        seller_helper = self._session_event(now_ms, now_ms, 'session-1', 'buyer-1')
        seller_helper['operation']['content']['sessionArouse']['memberFlags'] = 0
        status_event = {'1': ['unknown-status']}

        asyncio.run(live.handle_message({
            'body': {'syncPushPackage': {'data': [
                {'bizType': 1, 'objectType': 2, 'data': seller_helper},
                {'bizType': 3, 'objectType': 4, 'data': status_event},
            ]}},
        }, None))

        self.assertEqual(live._save_sync_diagnostic.call_count, 2)
        self.assertEqual(live._save_sync_diagnostic.call_args_list[0].args[0], 'background_content_8')
        self.assertEqual(live._save_sync_diagnostic.call_args_list[1].args[0], 'status_event')

    def test_typing_status_extracts_candidate_for_current_seller(self):
        item = {'bizType': 40, 'objectType': 40006}
        message = {'1': [
            {'1': 'new-cid@goofish', '2': 1, '3': 1, '4': 'seller-1@goofish'},
            {'1': 'other-cid@goofish', '2': 0, '3': 0, '4': 'seller-2@goofish'},
        ]}

        self.assertEqual(
            XianyuLive._typing_candidate_cids(item, message, 'seller-1'),
            ['new-cid'],
        )

    def test_fresh_typing_candidate_queries_conversation_and_reports_session(self):
        async def run_test():
            now_ms = int(time.time() * 1000)
            live = XianyuLive.__new__(XianyuLive)
            live.myid = 'seller-1'
            live.store_id = 203
            live._session_open_candidates = {'new-cid'}
            live._save_raw_message = Mock()
            live._save_sync_diagnostic = Mock()
            live.ws_client = Mock()
            live.ws_client.send = AsyncMock(return_value=True)
            live._request = AsyncMock(return_value={
                'code': 200,
                'body': [{
                    'type': 1,
                    'singleChatUserConversation': {
                        'singleChatConversation': {
                            'cid': 'new-cid@goofish',
                            'pairFirst': 'buyer-1@goofish',
                            'pairSecond': 'seller-1@goofish',
                            'createAt': now_ms - 1000,
                            'extension': {'itemId': 'item-1', 'itemTitle': '电影票'},
                        },
                    },
                }],
            })

            await live._handle_typing_candidate('new-cid', Mock())

            self.assertEqual(live._request.await_args.args[1], '/r/Conversation/getByCids')
            self.assertEqual(live._request.await_args.args[2], [['new-cid@goofish']])
            payload = live.ws_client.send.await_args.args[0]
            self.assertEqual(payload['buyerId'], 'buyer-1')
            self.assertEqual(payload['itemId'], 'item-1')
            self.assertEqual(
                live.ws_client.send.await_args.kwargs['dedupe_key'],
                'session_opened:203:buyer-1:item-1',
            )
            self.assertNotIn('new-cid', live._session_open_candidates)

        asyncio.run(run_test())

    def test_existing_buyer_conversation_for_new_item_is_reported(self):
        now_ms = int(time.time() * 1000)
        live = XianyuLive.__new__(XianyuLive)
        live.myid = 'seller-1'
        live.ws_client = Mock()
        response = {
            'code': 200,
            'body': [{
                '1': 1,
                '2': {
                    '1': {
                        '1': 'old-cid@goofish',
                        '2': 'buyer-1@goofish',
                        '3': 'seller-1@goofish',
                        '4': now_ms - 300000,
                        '6': {'itemId': 'item-1'},
                    },
                },
            }],
        }

        payload = live._new_conversation_payload('old-cid', response, now_ms=now_ms)

        self.assertEqual(payload['buyerId'], 'buyer-1')
        self.assertEqual(payload['itemId'], 'item-1')
        self.assertEqual(payload['time'], str(now_ms))
        self.assertEqual(payload['messageId'], 'xianyu_session_opened_old-cid_item-1')

    def test_same_conversation_uses_different_dedupe_keys_for_different_items(self):
        async def run_test():
            live = XianyuLive.__new__(XianyuLive)
            live.store_id = 203
            live._save_raw_message = Mock()
            live.ws_client = Mock()
            live.ws_client.send = AsyncMock(return_value=True)

            first = {
                'sessionId': 'same-cid',
                'buyerId': 'buyer-1',
                'itemId': 'item-1',
            }
            second = {**first, 'itemId': 'item-2'}

            await live._report_session_opened(first)
            await live._report_session_opened(second)

            self.assertEqual(
                [call.kwargs['dedupe_key'] for call in live.ws_client.send.await_args_list],
                [
                    'session_opened:203:buyer-1:item-1',
                    'session_opened:203:buyer-1:item-2',
                ],
            )

        asyncio.run(run_test())

    def test_conversation_for_another_seller_is_not_reported(self):
        now_ms = int(time.time() * 1000)
        live = XianyuLive.__new__(XianyuLive)
        live.myid = 'seller-1'
        response = {
            'code': 200,
            'body': [{
                'type': 1,
                'singleChatUserConversation': {
                    'singleChatConversation': {
                        'cid': 'new-cid',
                        'pairFirst': 'buyer-1',
                        'pairSecond': 'seller-2',
                        'createAt': now_ms,
                        'extension': {'itemId': 'item-1'},
                    },
                },
            }],
        }

        self.assertIsNone(live._new_conversation_payload('new-cid', response, now_ms=now_ms))

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
                    'extensions': {'extUserId': buyer_id, 'itemId': 'item-1'},
                },
            },
        }


if __name__ == '__main__':
    unittest.main()
