import httpx
from server.http_transport import ClientPool


def test_clients_reuse_connections_without_cross_tenant_headers_or_cookies(monkeypatch):
    original=httpx.Client;requests=[];pool=ClientPool(limit=2)
    def respond(request):
        requests.append(request)
        return httpx.Response(200,headers={'Set-Cookie':'private-session=should-not-return; Path=/'},json={'ok':True})
    monkeypatch.setattr(httpx,'Client',lambda **kw:original(transport=httpx.MockTransport(respond),**kw))
    a={'_tenant_id':'a','base_url':'https://api.example','protocol':'openai','api_key':'synthetic'}
    b={**a,'_tenant_id':'b'}
    try:
        with pool.acquire(a) as first:first.get(a['base_url'],headers={'Authorization':'Bearer tenant-a'})
        with pool.acquire(a) as again:
            assert again is first;again.get(a['base_url'],headers={'Authorization':'Bearer tenant-a'})
        with pool.acquire(b) as other:
            assert other is not first;other.get(b['base_url'],headers={'Authorization':'Bearer tenant-b'})
        assert all('cookie' not in r.headers for r in requests)
        assert [r.headers['authorization'] for r in requests]==['Bearer tenant-a','Bearer tenant-a','Bearer tenant-b']
        assert len(pool.entries)==2
    finally:pool.close()
