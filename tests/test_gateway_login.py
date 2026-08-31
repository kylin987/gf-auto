import unittest
import base64
import json
import time
from unittest.mock import Mock, patch

from ws_client import (
    GatewayAuthManager,
    GatewayLoginError,
    gateway_login,
    gateway_token_expires_soon,
)


class GatewayLoginTest(unittest.TestCase):
    @staticmethod
    def _token(expires_at):
        payload = base64.urlsafe_b64encode(json.dumps({'exp': expires_at}).encode()).decode().rstrip('=')
        return f'header.{payload}.signature'

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

    def test_detects_gateway_token_before_expiry(self):
        self.assertTrue(gateway_token_expires_soon(self._token(int(time.time()) + 120)))
        self.assertFalse(gateway_token_expires_soon(self._token(int(time.time()) + 3600)))

    @patch('ws_client.gateway_login')
    @patch('ws_client.load_login_config')
    def test_shared_auth_manager_refreshes_stale_token_once(self, load_config, login):
        old_token = self._token(int(time.time()) - 1)
        new_token = self._token(int(time.time()) + 3600)
        auth = {'accessToken': old_token, 'wsUrl': 'wss://old.test/ws'}
        manager = GatewayAuthManager(auth)
        load_config.return_value = {
            'username': 'account', 'password': 'password', 'deviceId': 'device',
        }
        login.return_value = {'accessToken': new_token, 'wsUrl': 'wss://new.test/ws'}

        first = manager.refresh(old_token, False)
        second = manager.refresh(old_token, True)

        self.assertEqual(first['accessToken'], new_token)
        self.assertEqual(second['accessToken'], new_token)
        self.assertEqual(auth['wsUrl'], 'wss://new.test/ws')
        login.assert_called_once_with('account', 'password', device_id='device')


if __name__ == '__main__':
    unittest.main()
