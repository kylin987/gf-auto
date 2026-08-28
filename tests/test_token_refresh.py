import time
import threading
import unittest
from unittest.mock import Mock

from requests.cookies import RequestsCookieJar

from goofish_apis import XianyuApis, is_token_expired_response
from goofish_live import XianyuLive


class FakeResponse:
    def __init__(self, payload, cookies=None):
        self._payload = payload
        self.cookies = RequestsCookieJar()
        for name, value in (cookies or {}).items():
            self.cookies.set(name, value, domain='.goofish.com', path='/')

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses, cookies):
        self.responses = list(responses)
        self.cookies = RequestsCookieJar()
        for name, value in cookies.items():
            self.cookies.set(name, value, domain='.goofish.com', path='/')
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class TokenRefreshTest(unittest.TestCase):
    def _api(self, responses):
        api = XianyuApis({'_m_h5_tk': 'old_token_1000'}, 'device')
        api.session = FakeSession(responses, {'_m_h5_tk': 'old_token_1000'})
        return api

    def test_recognizes_known_expired_token_variants(self):
        self.assertTrue(is_token_expired_response({'ret': ['FAIL_SYS_TOKEN_EXOIRED::令牌过期']}))
        self.assertTrue(is_token_expired_response({'ret': 'TOKEN_EXPIRED'}))
        self.assertFalse(is_token_expired_response({'ret': ['SUCCESS::调用成功']}))

    def test_refresh_retries_once_after_cookie_rotation(self):
        api = self._api([
            FakeResponse(
                {'ret': ['FAIL_SYS_TOKEN_EXOIRED::令牌过期']},
                {'_m_h5_tk': 'fresh_token_2000'},
            ),
            FakeResponse({'ret': ['SUCCESS::调用成功'], 'data': {'userId': '1'}}),
        ])

        result = api.refresh_token()

        self.assertIn('SUCCESS', result['ret'][0])
        self.assertEqual(len(api.session.calls), 2)
        self.assertEqual(api._cookie_value('_m_h5_tk'), 'fresh_token_2000')

    def test_refresh_never_retries_more_than_once(self):
        api = self._api([
            FakeResponse({'ret': ['TOKEN_EXPIRED']}, {'_m_h5_tk': 'fresh_token_2000'}),
            FakeResponse({'ret': ['TOKEN_EXPIRED']}),
        ])

        result = api.refresh_token()

        self.assertTrue(is_token_expired_response(result))
        self.assertEqual(len(api.session.calls), 2)

    def test_login_check_is_scheduled_before_cookie_expiry(self):
        now = time.time()
        live = XianyuLive.__new__(XianyuLive)
        live.heartbeat_interval = 600
        live.cookies = {}
        live.xianyu = type('Api', (), {
            'export_cookies': lambda self: {
                '_m_h5_tk': f'token_{int((now + 1000) * 1000)}',
            },
        })()

        self.assertEqual(live._seconds_until_login_check(now), 600)

        live.xianyu = type('Api', (), {
            'export_cookies': lambda self: {
                '_m_h5_tk': f'token_{int((now + 420) * 1000)}',
            },
        })()
        self.assertAlmostEqual(live._seconds_until_login_check(now), 120, delta=1)

    def test_stable_message_id_uses_source_id_or_content_fallback(self):
        live = XianyuLive.__new__(XianyuLive)
        live.store_id = 203
        simplified = {'cid': '1', 'senderUserId': '2', 'time': '3', 'contentType': 1, 'text': 'hello'}

        source_a = live._stable_message_id({'messageId': 'upstream-1'}, simplified)
        source_b = live._stable_message_id({'messageId': 'upstream-1'}, simplified)
        fallback_a = live._stable_message_id({}, simplified)
        fallback_b = live._stable_message_id({}, simplified)

        self.assertEqual(source_a, source_b)
        self.assertEqual(fallback_a, fallback_b)
        self.assertNotEqual(source_a, fallback_a)

    def test_access_token_change_requests_im_reconnect(self):
        live = XianyuLive.__new__(XianyuLive)
        live.cookies = {'_m_h5_tk': 'token_1000'}
        live.myid = 'seller_1'
        live.access_token = 'old-access-token'
        live.ws = object()
        live._login_state_lock = threading.RLock()
        live._login_valid = True
        live._sync_saved_session = Mock()
        live._request_im_reconnect = Mock()
        live._set_login_invalid = Mock()
        live.xianyu = type('Api', (), {
            'refresh_token': lambda self: {'ret': ['SUCCESS::调用成功']},
            'get_token': lambda self: {
                'ret': ['SUCCESS::调用成功'],
                'data': {'accessToken': 'new-access-token'},
            },
        })()

        self.assertTrue(live.check_login())
        self.assertEqual(live.access_token, 'new-access-token')
        live._request_im_reconnect.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
