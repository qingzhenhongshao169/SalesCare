"""Commit-time wake hints; durable jobs and permission-checked DB reads stay authoritative."""
import asyncio
import hashlib
import logging
import threading
import psycopg
from .config import settings

JOB_CHANNEL = 'salescare_ai_wake'
MESSAGE_CHANNEL = 'salescare_message_wake'


def conversation_key(tenant, conversation):
    return hashlib.sha256((tenant+':'+conversation).encode()).hexdigest()


class PgListener:
    """One connection per process, never one database connection per visitor."""
    def __init__(self, channel, callback):
        if channel not in (JOB_CHANNEL, MESSAGE_CHANNEL):
            raise ValueError('Unknown notification channel')
        self.channel, self.callback = channel, callback
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name='salescare-wake')
        self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                url = settings()['database_url'].replace('postgresql+psycopg:', 'postgresql:')
                with psycopg.connect(url, autocommit=True, connect_timeout=3) as conn:
                    conn.execute('LISTEN '+self.channel)  # Fixed platform constants only.
                    self.ready.set()
                    while not self.stop.is_set():
                        for note in conn.notifies(timeout=.25, stop_after=1):
                            self.callback(note.payload)
            except Exception:
                self.ready.clear()
                logging.getLogger('salescare.realtime').warning('Notification listener reconnecting; durable polling retained.')
                self.stop.wait(1)
        self.ready.clear()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)


class MessageBus:
    def __init__(self):
        self.lock = threading.Lock()
        self.subscribers = {}
        self.listener = PgListener(MESSAGE_CHANNEL, self._dispatch)

    def _dispatch(self, key):
        with self.lock:
            targets = list(self.subscribers.get(key, {}).values())
        for loop, event in targets:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass  # Client loop was already closed.

    def subscribe(self, tenant, conversation):
        key = conversation_key(tenant, conversation)
        event = asyncio.Event()
        with self.lock:
            self.subscribers.setdefault(key, {})[id(event)] = (asyncio.get_running_loop(), event)
        self.listener.start()
        return key, event

    def unsubscribe(self, key, event):
        with self.lock:
            group = self.subscribers.get(key, {})
            group.pop(id(event), None)
            if not group:
                self.subscribers.pop(key, None)


message_bus = MessageBus()
