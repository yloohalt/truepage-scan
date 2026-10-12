"""Single-owner OAuth: SDK validates redirects and PKCE; owner approves with server key.

Ephemeral registrations/tokens deliberately expire on server restart. This is not
a multi-user identity provider. Use a managed OAuth provider for shared deployments.
"""
import html
import secrets
import time
from urllib.parse import urlencode

from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizeError, TokenError
from mcp.shared.auth import OAuthToken
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse


class OwnerOAuth:
    def __init__(self, base_url, owner_key):
        self.base_url, self.owner_key = base_url, owner_key
        self.clients, self.pending, self.codes, self.tokens = {}, {}, {}, {}

    def prune(self):
        now = time.time()
        self.pending = {k:v for k,v in self.pending.items() if v['expires'] > now}
        self.codes = {k:v for k,v in self.codes.items() if v.expires_at > now}
        self.tokens = {k:v for k,v in self.tokens.items() if v.expires_at > now}

    async def get_client(self, client_id):
        entry = self.clients.get(client_id)
        return entry

    async def register_client(self, client_info):
        from mcp.server.auth.provider import RegistrationError
        if len(self.clients) >= 256:
            raise RegistrationError('invalid_client_metadata', 'Registration capacity reached.')
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client, params):
        self.prune()
        if params.resource and params.resource.rstrip('/') != self.base_url + '/mcp':
            raise AuthorizeError('invalid_request', 'Incorrect resource audience.')
        if len(self.pending) >= 128:
            raise AuthorizeError('temporarily_unavailable', 'Too many pending authorizations.')
        ticket = secrets.token_urlsafe(32)
        self.pending[ticket] = {'client':client, 'params':params, 'expires':time.time()+300, 'attempts':0}
        return self.base_url + '/connect?ticket=' + ticket

    async def connect(self, request):
        self.prune()
        ticket = request.query_params.get('ticket', '')
        pending = self.pending.get(ticket)
        if not pending:
            return HTMLResponse('Connection request expired. Start connecting again.', status_code=400)
        if request.method == 'POST':
            form = await request.form()
            pending['attempts'] += 1
            if pending['attempts'] > 5:
                self.pending.pop(ticket, None)
                return HTMLResponse('Too many attempts. Start connecting again.', status_code=429)
            if not secrets.compare_digest(str(form.get('key', '')), self.owner_key):
                return HTMLResponse('Incorrect access key. Go back and try again.', status_code=401)
            params, client = pending['params'], pending['client']
            self.pending.pop(ticket)
            code = secrets.token_urlsafe(32)
            self.codes[code] = AuthorizationCode(code=code, scopes=params.scopes or ['scan'],
                expires_at=time.time()+60, client_id=client.client_id, code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=self.base_url+'/mcp', subject='owner')
            query = {'code':code}
            if params.state is not None:
                query['state'] = params.state
            redirect = str(params.redirect_uri)
            return RedirectResponse(redirect + ('&' if '?' in redirect else '?') + urlencode(query), status_code=303)
        client = pending['client']
        name = html.escape(client.client_name or client.client_id)
        redirect = html.escape(str(pending['params'].redirect_uri))
        return HTMLResponse(f'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Connect TruePage Scan</title><main style="font:17px system-ui;max-width:560px;margin:70px auto;padding:24px">
        <h1>Connect TruePage Scan</h1><p>Allow <strong>{name}</strong> to create upload sessions, process reviewed photos, retrieve scans, and delete sessions on your server?</p>
        <p>Return address: <code>{redirect}</code></p><p>Only continue if you started this connection. Find your access key in the Render service's Environment settings. Do not paste it into chat.</p>
        <form method="post"><label>Server access key <input name="key" type="password" required autocomplete="off"></label><p><button>Approve connection</button></p></form>
        <p>Access expires in one hour or when the service restarts. Reconnect when needed.</p></main>''')

    async def load_authorization_code(self, client, authorization_code):
        self.prune()
        code = self.codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    async def exchange_authorization_code(self, client, authorization_code):
        code = self.codes.pop(authorization_code.code, None)
        if not code or code.expires_at <= time.time() or code.client_id != client.client_id:
            raise TokenError('invalid_grant', 'Code expired or already used.')
        self.prune()
        if len(self.tokens) >= 256:
            raise TokenError('invalid_request', 'Token capacity reached.')
        token = secrets.token_urlsafe(32)
        self.tokens[token] = AccessToken(token=token, client_id=client.client_id, scopes=code.scopes,
            expires_at=int(time.time())+3600, resource=self.base_url+'/mcp', subject='owner')
        return OAuthToken(access_token=token, expires_in=3600, scope=' '.join(code.scopes))

    async def load_access_token(self, token):
        if secrets.compare_digest(token, self.owner_key):
            return AccessToken(token=token, client_id='owner-api', scopes=['scan'], resource=self.base_url+'/mcp', subject='owner')
        self.prune()
        return self.tokens.get(token)

    async def load_refresh_token(self, client, refresh_token):
        return None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        raise TokenError('invalid_grant', 'Reconnect to obtain a new access token.')

    async def revoke_token(self, token):
        self.tokens.pop(token.token, None)
