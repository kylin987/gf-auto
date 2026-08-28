from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path


class EventOutbox:
    """Per-store durable queue for buyer events awaiting Gateway ACK."""

    def __init__(self, database_path):
        self.database_path = str(database_path)
        self._lock = threading.RLock()
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._lock, self._connect() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('''
                CREATE TABLE IF NOT EXISTS event_outbox (
                    message_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    create_time REAL NOT NULL,
                    update_time REAL NOT NULL
                )
            ''')
            connection.execute('''
                CREATE INDEX IF NOT EXISTS idx_event_outbox_pending
                ON event_outbox (status, next_attempt_at, create_time)
            ''')

    def enqueue(self, message_id, payload):
        now = time.time()
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        with self._lock, self._connect() as connection:
            cursor = connection.execute('''
                INSERT OR IGNORE INTO event_outbox (
                    message_id, payload_json, status, attempts, last_error,
                    next_attempt_at, create_time, update_time
                ) VALUES (?, ?, 'pending', 0, '', 0, ?, ?)
            ''', (str(message_id), payload_json, now, now))
            return cursor.rowcount > 0

    def oldest_pending(self):
        with self._lock, self._connect() as connection:
            row = connection.execute('''
                SELECT message_id, payload_json, attempts, next_attempt_at, create_time
                FROM event_outbox
                WHERE status = 'pending'
                ORDER BY create_time ASC
                LIMIT 1
            ''').fetchone()
        if row is None:
            return None
        return {
            'message_id': row['message_id'],
            'payload': json.loads(row['payload_json']),
            'attempts': int(row['attempts']),
            'next_attempt_at': float(row['next_attempt_at']),
            'create_time': float(row['create_time']),
        }

    def remove(self, message_id):
        with self._lock, self._connect() as connection:
            connection.execute('DELETE FROM event_outbox WHERE message_id = ?', (str(message_id),))

    def mark_failed(self, message_id, error):
        now = time.time()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                'SELECT attempts FROM event_outbox WHERE message_id = ?',
                (str(message_id),),
            ).fetchone()
            if row is None:
                return
            attempts = int(row['attempts']) + 1
            retry_delay = min(60, max(2, 2 ** min(attempts, 6)))
            connection.execute('''
                UPDATE event_outbox
                SET attempts = ?, last_error = ?, next_attempt_at = ?, update_time = ?
                WHERE message_id = ?
            ''', (attempts, str(error)[:500], now + retry_delay, now, str(message_id)))

    def mark_blocked(self, message_id, error):
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute('''
                UPDATE event_outbox
                SET status = 'blocked', attempts = attempts + 1,
                    last_error = ?, update_time = ?
                WHERE message_id = ?
            ''', (str(error)[:500], now, str(message_id)))

    def count(self, status=None):
        with self._lock, self._connect() as connection:
            if status:
                row = connection.execute(
                    'SELECT COUNT(*) AS total FROM event_outbox WHERE status = ?',
                    (str(status),),
                ).fetchone()
            else:
                row = connection.execute('SELECT COUNT(*) AS total FROM event_outbox').fetchone()
        return int(row['total'])
