import unittest
from unittest.mock import Mock, patch

from ws_client import GatewayLoginError, gateway_login


class GatewayLoginTest(unittest.TestCase):
    @patch('ws_client.save_login_config')
    @patch('ws_client.requests.post')
    def test_accepts_gateway_success_code_200(self, post, save_login_config):
        post.return_value = Mock(json=Mock(return_value={
            'code': 200,
            'msg': 'success',
            'data': {'accessToken': 'token', 'wsUrl': 'wss://example.test/ws'},
        }))

        result = gateway_login('account', 'password', 'device')

        self.assertEqual(result['accessToken'], 'token')
        save_login_config.assert_called_once_with(
            username='account',
            password='password',
            device_id='device',
        )

    @patch('ws_client.save_login_config')
    @patch('ws_client.requests.post')
    def test_rejects_legacy_success_code_0(self, post, save_login_config):
        post.return_value = Mock(json=Mock(return_value={
            'code': 0,
            'msg': 'success',
            'data': {'accessToken': 'token'},
        }))

        with self.assertRaisesRegex(GatewayLoginError, 'success'):
            gateway_login('account', 'password', 'device')

        save_login_config.assert_not_called()


if __name__ == '__main__':
    unittest.main()
