"""Browser authentication tests with no broker, account or network access."""
from dataclasses import replace
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
import time

from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.dashboard_auth import COOKIE_NAME, SESSION_SECONDS, LocalDashboardSessions
from app.main import create_app


class DashboardAuthTest(IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = Settings(mode='live', api_access_token='mock-private-api-token')
        self.app = create_app(self.settings)
        self.app.state.settings = self.settings
        self.app.state.live_broker = SimpleNamespace(
            settings=self.settings, risk=SimpleNamespace(armed=False),
            reconciled=True, last_sync_at=None, last_error=None, disarm=Mock(),
        )
        self.engine = SimpleNamespace(stop=AsyncMock(), status=Mock(return_value={'running': False}))
        self.app.state.engine = self.engine
        self.base = 'http://127.0.0.1:8002'
        self.headers = {'Origin': self.base, 'Sec-Fetch-Site': 'same-origin', 'X-Dashboard-Request': '1'}

    def client(self, peer='127.0.0.1', base=None):
        return AsyncClient(transport=ASGITransport(app=self.app, client=(peer, 12345)),
                           base_url=base or self.base)

    async def test_local_cookie_authenticates_without_exposing_api_secret(self):
        async with self.client() as client:
            self.assertEqual((await client.get('/api/v1/live/status')).status_code, 401)
            response = await client.post('/api/v1/auth/local-session', headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'authenticated': True})
            cookie = response.headers['set-cookie']
            for flag in ('HttpOnly', 'SameSite=strict', 'Path=/api/v1', 'Max-Age=28800'):
                self.assertIn(flag, cookie)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertNotIn(self.settings.api_access_token, cookie + response.text)
            self.assertEqual((await client.get('/api/v1/live/status')).status_code, 200)
            self.assertEqual((await client.post('/api/v1/engine/stop', headers={'Origin': self.base})).status_code, 200)
        self.engine.stop.assert_awaited_once()

    async def test_auto_auth_rejects_remote_peers_rebinding_and_foreign_origins(self):
        cases = [
            ('192.0.2.1', self.base, self.headers),
            ('127.0.0.1', 'http://evil.example:8002', {**self.headers, 'Origin': 'http://evil.example:8002'}),
            ('127.0.0.1', self.base, {**self.headers, 'Origin': 'https://evil.example'}),
            ('127.0.0.1', self.base, {**self.headers, 'Origin': 'http://127.0.0.1:8003'}),
            ('127.0.0.1', self.base, {**self.headers, 'Origin': 'http://127.0.0.1:invalid'}),
            ('127.0.0.1', self.base, {**self.headers, 'Origin': 'null'}),
            ('127.0.0.1', self.base, {**self.headers, 'Sec-Fetch-Site': 'cross-site'}),
            ('127.0.0.1', self.base, {**self.headers, 'Sec-Fetch-Site': 'same-site'}),
            ('127.0.0.1', self.base, {'X-Dashboard-Request': '1'}),
            ('127.0.0.1', self.base, {'Origin': self.base}),
        ]
        for peer, base, headers in cases:
            with self.subTest(peer=peer, base=base, headers=headers):
                async with self.client(peer, base) as client:
                    response = await client.post('/api/v1/auth/local-session', headers=headers)
                    self.assertEqual(response.status_code, 403)
                    self.assertNotIn('set-cookie', response.headers)
        self.engine.stop.assert_not_awaited()

    async def test_cookie_cannot_authorize_foreign_or_originless_mutations(self):
        async with self.client() as client:
            await client.post('/api/v1/auth/local-session', headers=self.headers)
            for headers in ({}, {'Origin': 'https://evil.example'},
                            {'Origin': self.base, 'Sec-Fetch-Site': 'cross-site'}):
                self.assertEqual((await client.post('/api/v1/engine/stop', headers=headers)).status_code, 401)
            self.assertEqual((await client.get('/api/v1/live/status', headers={'Sec-Fetch-Site': 'cross-site'})).status_code, 401)
            self.assertEqual((await client.get('http://127.0.0.1:8003/api/v1/live/status')).status_code, 401)
        self.engine.stop.assert_not_awaited()

    async def test_expired_or_restarted_session_can_be_renewed(self):
        async with self.client() as client:
            await client.post('/api/v1/auth/local-session', headers=self.headers)
            with patch('app.dashboard_auth.time.monotonic', return_value=time.monotonic() + SESSION_SECONDS + 1):
                self.assertEqual((await client.get('/api/v1/live/status')).status_code, 401)
                self.assertEqual((await client.post('/api/v1/auth/local-session', headers=self.headers)).status_code, 200)
                self.assertEqual((await client.get('/api/v1/live/status')).status_code, 200)
            self.app.state.dashboard_sessions = LocalDashboardSessions()
            self.assertEqual((await client.get('/api/v1/live/status')).status_code, 401)
            await client.post('/api/v1/auth/local-session', headers=self.headers)
            self.assertEqual((await client.get('/api/v1/live/status')).status_code, 200)

    async def test_api_key_rotation_invalidates_cookie_and_header_clients_still_work(self):
        async with self.client() as client:
            await client.post('/api/v1/auth/local-session', headers=self.headers)
            self.app.state.settings = replace(self.settings, api_access_token='mock-replaced-token')
            self.assertEqual((await client.get('/api/v1/live/status')).status_code, 401)
            await client.post('/api/v1/auth/local-session', headers=self.headers)
            self.assertEqual((await client.get('/api/v1/live/status')).status_code, 200)
        async with self.client('192.0.2.1', 'http://remote.example') as client:
            self.assertEqual((await client.get('/api/v1/live/status', headers={'X-API-Token': 'mock-replaced-token'})).status_code, 200)
            self.assertEqual((await client.get('/api/v1/live/status', headers={'X-API-Token': 'wrong'})).status_code, 401)

    async def test_localhost_ipv6_and_secure_cookie_supported(self):
        for peer, base in (('::1', 'http://[::1]:8002'), ('127.0.0.1', 'http://localhost:8002'),
                           ('127.0.0.1', 'https://localhost:8002')):
            async with self.client(peer, base) as client:
                response = await client.post('/api/v1/auth/local-session',
                                             headers={**self.headers, 'Origin': base})
                self.assertEqual(response.status_code, 200)
                self.assertEqual((await client.get('/api/v1/live/status')).status_code, 200)
                if base.startswith('https:'):
                    self.assertIn('Secure', response.headers['set-cookie'])

    async def test_forged_cookie_is_rejected(self):
        async with self.client() as client:
            client.cookies.set(COOKIE_NAME, 'forged-session')
            self.assertEqual((await client.get('/api/v1/live/status')).status_code, 401)

