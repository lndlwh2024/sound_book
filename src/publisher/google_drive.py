"""Google Drive read-only desktop OAuth and API. No Drive mutation methods."""
import base64
import ctypes
import hashlib
import json
import os
import secrets
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, parse_qs, quote
from urllib.request import Request, build_opener, HTTPRedirectHandler

SCOPE = 'https://www.googleapis.com/auth/drive.readonly'
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
API_URL = 'https://www.googleapis.com/drive/v3/'
FILE_FIELDS = 'id,name,mimeType,size,md5Checksum,modifiedTime,version,capabilities(canDownload)'


class DriveError(RuntimeError):
    pass


class DriveAuthRequired(DriveError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an OAuth bearer token to a redirect destination.
        return None


def request(url, *, data=None, token=None, stream=False):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    body = urlencode(data).encode() if data is not None else None
    try:
        response = build_opener(NoRedirect).open(Request(url, data=body, headers=headers), timeout=20)
        if stream:
            return response
        with response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 401:
            raise DriveAuthRequired('Google授权已失效，请重新连接') from None
        if error.code == 400 and url == TOKEN_URL:
            raise DriveAuthRequired('Google授权不可用或已撤销，请重新连接') from None
        raise DriveError(f'Google请求失败（HTTP {error.code}），请检查权限、网络与Drive API设置') from None
    except (URLError, TimeoutError, OSError) as error:
        reason = error.reason if isinstance(error,URLError) else error
        if isinstance(reason,ssl.SSLCertVerificationError):
            detail = 'TLS证书验证失败'
        elif isinstance(reason,ssl.SSLError):
            detail = 'TLS连接失败'
        elif isinstance(reason,socket.gaierror):
            detail = 'DNS域名解析失败'
        elif isinstance(reason,TimeoutError):
            detail = '连接或读取超时'
        elif isinstance(reason,ConnectionRefusedError):
            detail = '连接被拒绝，请检查代理或网络服务'
        else:
            detail = type(reason).__name__
        code = getattr(reason,'winerror',None) or getattr(reason,'errno',None)
        if isinstance(code,int):
            detail += f'，错误码{code}'
        # Do not include arbitrary transport text containing proxy credentials or URLs.
        raise DriveError('无法连接Google（'+detail+'），请检查网络后重试；未据此判定授权失效') from None


def _protect(data, decrypt=False):
    """Windows user-bound DPAPI; never fall back to plaintext credentials."""
    if os.name != 'nt':
        raise DriveError('当前凭据存储需要Windows DPAPI支持')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    if decrypt:
        crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,
            ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        crypt.CryptUnprotectData.restype = wintypes.BOOL
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        crypt.CryptProtectData.argtypes = [ctypes.POINTER(Blob),wintypes.LPCWSTR,ctypes.c_void_p,
            ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        crypt.CryptProtectData.restype = wintypes.BOOL
        ok = crypt.CryptProtectData(ctypes.byref(source), 'ShuSheng Google Drive', None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise DriveAuthRequired('云盘凭据无法解密或保存，请在当前Windows账号重新连接')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel = ctypes.WinDLL('kernel32')
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


class DriveCredentials:
    """One production Drive login shared by resource bindings, outside account identity."""
    def __init__(self, root):
        self.root = Path(root) / 'google_drive'
        self.client_path = self.root / 'client.json'
        self.token_path = self.root / 'credentials.dpapi'
        self.lock = threading.RLock()
        self.refresh_lock = threading.Lock()
        self.generation = 0

    def client(self):
        try:
            value = json.loads(self.client_path.read_text(encoding='utf-8-sig'))['installed']
            if not isinstance(value,dict) or not all(isinstance(value.get(k),str) and value[k] for k in ('client_id','client_secret')):
                raise ValueError()
            # Endpoints are constants, never taken from imported JSON.
            return {'client_id': value['client_id'], 'client_secret': value['client_secret']}
        except (OSError, ValueError, KeyError, TypeError):
            raise DriveError('请先导入Google桌面应用OAuth客户端JSON，并启用Google Drive API') from None

    def import_client(self, path):
        raw = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        data = raw.get('installed', {}) if isinstance(raw,dict) else {}
        if not isinstance(data,dict) or not all(isinstance(data.get(k),str) and data[k] for k in ('client_id','client_secret')):
            raise DriveError('请选择Google“桌面应用”类型的OAuth客户端JSON')
        # Validate before replacing an existing usable client or connection.
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            temp = self.client_path.with_suffix('.tmp')
            temp.write_text(json.dumps({'installed': {'client_id': data['client_id'],
                'client_secret': data['client_secret']}}), encoding='utf-8')
            temp.replace(self.client_path)
            self.disconnect()

    def read(self):
        try:
            data = json.loads(_protect(self.token_path.read_bytes(), decrypt=True))
            if not isinstance(data,dict):
                raise ValueError()
            return data
        except FileNotFoundError:
            return {}
        except (ValueError, OSError):
            raise DriveAuthRequired('云盘凭据损坏，请重新连接') from None

    def save(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.token_path.with_suffix('.tmp')
        temp.write_bytes(_protect(json.dumps(data).encode()))
        temp.replace(self.token_path)

    def connected(self):
        try:
            with self.lock:
                return bool(self.read().get('refresh_token'))
        except DriveError:
            return False

    def disconnect(self):
        with self.lock:
            self.generation += 1
            self.token_path.unlink(missing_ok=True)

    def install(self, data, generation):
        with self.lock:
            if generation != self.generation:
                raise InterruptedError('连接操作已取消')
            if not data.get('refresh_token'):
                raise DriveError('Google未返回离线授权，请重新连接并确认授权')
            data['expires_at'] = time.time() + data.get('expires_in', 3600)
            self.save(data)
            self.generation += 1

    def access_token(self):
        # Network refresh never holds the UI-facing credential lock.
        with self.refresh_lock:
            with self.lock:
                data = self.read()
                generation = self.generation
                client = self.client()
            if not data.get('refresh_token'):
                raise DriveAuthRequired('Google云盘未连接')
            if data.get('expires_at', 0) <= time.time() + 60:
                response = request(TOKEN_URL, data={**client, 'grant_type': 'refresh_token',
                    'refresh_token': data['refresh_token']})
                data.update(response)
                data['expires_at'] = time.time() + response.get('expires_in', 3600)
                with self.lock:
                    if generation != self.generation:
                        raise DriveAuthRequired('Google云盘已断开')
                    self.save(data)
            return data['access_token']


def authorize(credentials, open_url, stopped, seconds=300):
    """Bind loopback before opening the system browser. PKCE + state, cancellable."""
    client = credentials.client()
    generation = credentials.generation
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    state = secrets.token_urlsafe(32)
    result = {}
    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Authorization code/state must never enter logs.

        def do_GET(self):
            parsed = urlsplit(self.path)
            values = parse_qs(parsed.query)
            if parsed.path != '/oauth/callback' or not secrets.compare_digest(values.get('state', [''])[0], state):
                self.send_error(400, 'Invalid callback')
                return
            if values.get('code'):
                result['code'] = values['code'][0]
            else:
                result['error'] = '授权已取消或拒绝'
            body = '<meta charset="utf-8"><h2>已收到授权结果，请返回书声查看连接状态。</h2>'.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    with HTTPServer(('127.0.0.1', 0), Callback) as server:
        server.timeout = .25
        redirect = f'http://127.0.0.1:{server.server_port}/oauth/callback'
        open_url(AUTH_URL + '?' + urlencode({'client_id': client['client_id'], 'redirect_uri': redirect,
            'response_type': 'code', 'scope': SCOPE, 'access_type': 'offline', 'prompt': 'consent',
            'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'}))
        deadline = time.monotonic() + seconds
        while not result and not stopped() and time.monotonic() < deadline:
            server.handle_request()
        if stopped():
            raise InterruptedError('已取消连接')
        if not result.get('code'):
            raise DriveError(result.get('error', '等待Google授权超时，原连接状态保留'))
        data = request(TOKEN_URL, data={**client, 'code': result['code'], 'code_verifier': verifier,
            'redirect_uri': redirect, 'grant_type': 'authorization_code'})
        if stopped():
            raise InterruptedError('已取消连接')
        credentials.install(data, generation)


def folder_id(value):
    value = value.strip()
    if value.startswith('https://'):
        parsed = urlsplit(value)
        if parsed.hostname != 'drive.google.com':
            raise DriveError('请输入Google云盘文件夹链接或目录ID')
        parts = parsed.path.split('/folders/')
        value = parts[1].split('/')[0] if len(parts) == 2 else parse_qs(parsed.query).get('id', [''])[0]
    if not value or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in value):
        raise DriveError('云盘目录ID无效，请使用文件夹链接或目录ID')
    return value


def query_literal(value):
    return value.replace('\\', '\\\\').replace("'", "\\'")


class DriveClient:
    def __init__(self, credentials):
        self.credentials = credentials
        self._identity_lock = threading.Lock()
        self._identity_generation = None
        self._account_identity = None

    def account_identity(self):
        """Identify the actual Drive user, never the OAuth client or access token."""
        with self._identity_lock:
            generation = self.credentials.generation
            if self._identity_generation == generation:
                return self._account_identity
            user = self.get('about', fields='user(permissionId)').get('user', {})
            permission_id = user.get('permissionId')
            if not isinstance(permission_id, str) or not permission_id.strip():
                raise DriveError('无法确认Google云盘账号，暂不扫描或下载，请稍后重试')
            identity = 'google:' + hashlib.sha256(permission_id.encode()).hexdigest()
            with self.credentials.lock:
                if generation != self.credentials.generation:
                    raise InterruptedError('云盘连接已变更，取消本轮扫描')
                self._account_identity = identity
                self._identity_generation = generation
            return identity

    def get(self, route, **params):
        return request(API_URL + route + '?' + urlencode(params), token=self.credentials.access_token())

    def metadata(self, identity):
        return self.get('files/' + quote(folder_id(identity), safe=''), fields=FILE_FIELDS, supportsAllDrives='true')

    def children(self, parent='root', folders_only=False):
        query = f"'{query_literal(folder_id(parent))}' in parents and trashed = false"
        if folders_only:
            query += " and mimeType = 'application/vnd.google-apps.folder'"
        page = None
        output = []
        while True:
            params = dict(q=query, fields=f'nextPageToken,incompleteSearch,files({FILE_FIELDS})', pageSize=1000,
                supportsAllDrives='true', includeItemsFromAllDrives='true')
            if page:
                params['pageToken'] = page
            data = self.get('files', **params)
            if data.get('incompleteSearch'):
                raise DriveError('Google目录查询不完整，请使用明确的文件夹链接重试')
            output.extend(data.get('files', []))
            page = data.get('nextPageToken')
            if not page:
                return sorted(output, key=lambda item: (item['name'].casefold(), item['id']))

    def today_files(self, parent, day):
        folders = [f for f in self.children(parent, True) if f['name'] == day]
        if not folders:
            return None, []
        # Drive allows equal folder names. Read every existing match, keeping
        # stable IDs; never create, rename, merge or delete remote folders.
        files = {}
        for folder in folders:
            for item in self.children(folder['id']):
                files.setdefault(item['id'], item)
        selected = dict(folders[0], existing_folder_ids=[f['id'] for f in folders])
        return selected, sorted(files.values(), key=lambda item: (item['name'].casefold(), item['id']))

    def download(self, identity, destination, stopped, progress):
        if stopped():
            raise InterruptedError()
        url = API_URL + 'files/' + quote(folder_id(identity), safe='') + '?' + urlencode({'alt': 'media', 'supportsAllDrives': 'true'})
        with request(url, token=self.credentials.access_token(), stream=True) as response, Path(destination).open('wb') as stream:
            total = 0
            while True:
                if stopped():
                    raise InterruptedError()
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                stream.write(chunk)
                total += len(chunk)
                progress(total)
            stream.flush()
            os.fsync(stream.fileno())
