import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from event_outbox import EventOutbox
from ws_client import GatewayClient


class FakeGatewayWebSocket:
    def __init__(self, client, accepted=True):
        self.client = client
        self.accepted = accepted
        self.messages = []

    async def send(self, raw):
        message = json.loads(raw)
        self.messages.append(message)
        await self.client._handle_message(json.dumps({
            'type': 'xianyu.message.ack',
            'payload': {
                'accepted': self.accepted,
                'eventId': message['id'],
            },
        }))


class EventOutboxTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / 'event_outbox.sqlite3'

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_persists_and_deduplicates_across_instances(self):
        outbox = EventOutbox(self.database_path)
        payload = {'id': 'fish_1', 'payload': {'messageId': 'fish_1'}}
        self.assertTrue(outbox.enqueue('fish_1', payload))
        self.assertFalse(outbox.enqueue('fish_1', payload))

        reopened = EventOutbox(self.database_path)
        self.assertEqual(reopened.count(), 1)
        self.assertEqual(reopened.oldest_pending()['payload'], payload)

    def test_failed_oldest_message_blocks_newer_message(self):
        outbox = EventOutbox(self.database_path)
        outbox.enqueue('fish_1', {'id': 'fish_1'})
        outbox.enqueue('fish_2', {'id': 'fish_2'})
        outbox.mark_failed('fish_1', 'timeout')

        row = outbox.oldest_pending()
        self.assertEqual(row['message_id'], 'fish_1')
        self.assertGreater(row['next_attempt_at'], 0)

    async def test_offline_send_is_kept_until_gateway_ack(self):
        outbox = EventOutbox(self.database_path)
        client = GatewayClient('token', store_id=203, outbox=outbox)
        await client.send({
            'messageId': 'fish_1',
            'contentType': 1,
            'cid': 'cid_1',
            'text': 'hello',
        })
        self.assertEqual(outbox.count('pending'), 1)

        reopened = EventOutbox(self.database_path)
        recovered_client = GatewayClient('token', store_id=203, outbox=reopened)
        websocket = FakeGatewayWebSocket(recovered_client)
        worker = asyncio.create_task(recovered_client._run_outbox(websocket))
        for _ in range(50):
            if reopened.count() == 0:
                break
            await asyncio.sleep(0.01)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

        self.assertEqual(reopened.count(), 0)
        self.assertEqual(websocket.messages[0]['id'], 'fish_1')
        self.assertEqual(websocket.messages[0]['payload']['storeId'], 203)

    async def test_gateway_rejection_blocks_message(self):
        outbox = EventOutbox(self.database_path)
        client = GatewayClient('token', store_id=203, outbox=outbox)
        await client.send({'messageId': 'fish_2', 'contentType': 1, 'text': 'hello'})

        websocket = FakeGatewayWebSocket(client, accepted=False)
        worker = asyncio.create_task(client._run_outbox(websocket))
        for _ in range(50):
            if outbox.count('blocked') == 1:
                break
            await asyncio.sleep(0.01)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

        self.assertEqual(outbox.count('blocked'), 1)
        self.assertEqual(outbox.count('pending'), 0)

    async def test_expired_chat_is_removed_without_sending(self):
        outbox = EventOutbox(self.database_path)
        client = GatewayClient('token', store_id=203, outbox=outbox)
        await client.send({
            'messageId': 'fish_old_image',
            'contentType': 2,
            'time': str(int((time.time() - 601) * 1000)),
            'url': 'https://example.test/old.jpg',
        })

        websocket = FakeGatewayWebSocket(client)
        worker = asyncio.create_task(client._run_outbox(websocket))
        for _ in range(50):
            if outbox.count() == 0:
                break
            await asyncio.sleep(0.01)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

        self.assertEqual(outbox.count(), 0)
        self.assertEqual(websocket.messages, [])

    def test_order_event_never_expires_from_chat_outbox(self):
        old_time = time.time() - 86400
        row = {
            'create_time': old_time,
            'payload': {
                'sentAt': '2026-08-30T00:00:00+08:00',
                'payload': {'contentType': 4, 'time': str(int(old_time * 1000))},
            },
        }

        self.assertFalse(GatewayClient._is_expired_buyer_chat(row, now=time.time()))


if __name__ == '__main__':
    unittest.main()
