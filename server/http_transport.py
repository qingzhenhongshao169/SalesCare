"""Bounded per-tenant provider connection reuse; no shared authorization or cookies."""
import atexit
import hashlib
import http.cookiejar
import threading
import time
from contextlib import contextmanager
from collections import OrderedDict
import httpx


class NoCookies(http.cookiejar.DefaultCookiePolicy):
    def set_ok(self, cookie, request):return False
    def return_ok(self, cookie, request):return False


class ClientPool:
    def __init__(self, limit=16, ttl=120):
        self.limit, self.ttl = limit, ttl
        self.lock = threading.RLock()
        self.entries = OrderedDict()

    @contextmanager
    def acquire(self, cfg):
        # The constructor is part of the key so injected test transports cannot
        # accidentally reuse a real client or a different test's mock transport.
        identity = cfg.get('_tenant_id') or hashlib.sha256(cfg['api_key'].encode()).hexdigest()
        key = (identity, cfg['base_url'], cfg['protocol'], httpx.Client)
        cached = True
        with self.lock:
            for old, entry in list(self.entries.items()):
                if not entry['active'] and time.monotonic()-entry['used'] > self.ttl:
                    entry['client'].close();self.entries.pop(old)
            entry = self.entries.get(key)
            if entry is None:
                if len(self.entries) >= self.limit:
                    idle = next((k for k,e in self.entries.items() if not e['active']), None)
                    if idle is not None:self.entries.pop(idle)['client'].close()
                    else:cached = False
                client = httpx.Client(trust_env=False, follow_redirects=False,
                    cookies=http.cookiejar.CookieJar(policy=NoCookies()),
                    limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=30))
                entry = {'client':client, 'active':0, 'used':time.monotonic()}
                if cached:self.entries[key] = entry
            elif cached:self.entries.move_to_end(key)
            entry['active'] += 1
        try:yield entry['client']
        finally:
            with self.lock:
                entry['active'] -= 1;entry['used'] = time.monotonic()
                if not cached:entry['client'].close()

    def close(self):
        with self.lock:
            for key, entry in list(self.entries.items()):
                if not entry['active']:entry['client'].close();self.entries.pop(key)


provider_clients = ClientPool()
atexit.register(provider_clients.close)
