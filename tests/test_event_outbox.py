import asyncio
import json
import tempfile
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


if __name__ == '__main__':
    unittest.main()
