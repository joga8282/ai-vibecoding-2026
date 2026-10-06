"""Process-local browser sessions for the dashboard served on loopback."""
import hashlib
import ipaddress
import secrets
import time
from urllib.parse import urlsplit

from fastapi import Request


COOKIE_NAME = 'dashboard_session'
SESSION_SECONDS = 8 * 60 * 60


def is_local_dashboard_request(request: Request, *, require_origin=False) -> bool:
    """Reject remote peers, DNS rebinding, foreign origins and cross-site fetches."""
    try:
        if not request.client or not ipaddress.ip_address(request.client.host).is_loopback:
            return False
        if request.url.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            return False
        fetch_site = request.headers.get('Sec-Fetch-Site')
        if fetch_site is not None and fetch_site != 'same-origin':
            return False
        origin = request.headers.get('Origin')
        if not origin:
            return not require_origin
        parsed = urlsplit(origin)
        default_port = 443 if request.url.scheme == 'https' else 80
        return (parsed.scheme == request.url.scheme
                and parsed.hostname == request.url.hostname
                and (parsed.port or default_port) == (request.url.port or default_port)
                and not parsed.username and not parsed.password
                and not parsed.path and not parsed.query and not parsed.fragment)
    except (ValueError, TypeError):
        return False


class LocalDashboardSessions:
    def __init__(self):
        self._sessions = {}

    @staticmethod
    def _token_tag(api_token):
        return hashlib.sha256((api_token or '').encode('utf-8')).hexdigest()

    def issue(self, request: Request, api_token: str) -> str:
        now = time.monotonic()
        self._sessions = {key: value for key, value in self._sessions.items() if value[0] > now}
        # Bound memory; expired or evicted sessions transparently obtain a new cookie.
        if len(self._sessions) >= 128:
            oldest = min(self._sessions, key=lambda key: self._sessions[key][0])
            del self._sessions[oldest]
        session = secrets.token_urlsafe(32)
        self._sessions[session] = (now + SESSION_SECONDS, str(request.url.netloc), self._token_tag(api_token))
        return session

    def allows(self, request: Request, api_token: str) -> bool:
        if not is_local_dashboard_request(request, require_origin=request.method not in {'GET', 'HEAD'}):
            return False
        value = self._sessions.get(request.cookies.get(COOKIE_NAME, ''))
        return bool(value and value[0] > time.monotonic()
                    and value[1] == str(request.url.netloc)
                    and secrets.compare_digest(value[2], self._token_tag(api_token)))
