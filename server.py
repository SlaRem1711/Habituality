#!/usr/bin/env python3
"""habituality: a small self-hosted server for the habituality journal.

Python 3.9+ standard library only. Everything lives in HB_DATA:
habituality.db (SQLite) and files/ (photos and songs).

  python3 server.py                  run the server
  python3 server.py invite           print a one-time invite code
  python3 server.py passwd NAME      set a new password for an account
  python3 server.py users            list accounts
  python3 server.py setup-code       show the first-run setup code
  python3 server.py bans             list blocked addresses (and ones with recent wrong tries)
  python3 server.py unban IP|all     unblock an address
  python3 server.py backup [DIR]     copy the database and uploads to DIR

Settings (environment): HB_HOST (127.0.0.1), HB_PORT (8080), HB_DATA (./data),
HB_SECURE (auto: Secure cookies when the request comes in over https / *.ts.net),
HB_BAN_AFTER (10: wrong tries in a row from one address before it is blocked).
"""
import getpass
import gzip
import hashlib
import hmac
import http.cookies
import ipaddress
import json
import os
import queue
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.environ.get('HB_DATA', os.path.join(HERE, 'data')))
FILES = os.path.join(DATA, 'files')
HOST = os.environ.get('HB_HOST', '127.0.0.1')
PORT = int(os.environ.get('HB_PORT', '8080'))
SECURE = os.environ.get('HB_SECURE', 'auto').strip().lower()
INDEX = os.environ.get('HB_INDEX', os.path.join(HERE, 'index.html'))

MAX_JSON = 512 * 1024
MAX_DOC = 300 * 1024
MAX_UPLOAD = 25 * 1024 * 1024
USER_QUOTA = 1024 * 1024 * 1024
KEEP_FREE = 300 * 1024 * 1024
SESSION_TTL = 30 * 86400
INVITE_TTL = 7 * 86400
BAN_AFTER = max(3, int(os.environ.get('HB_BAN_AFTER', '10')))
MIN_PASSWORD = 12
COMMON_FILE = os.path.join(HERE, 'common-passwords.txt.gz')
ALPHA = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'

SEG = r'[A-Za-z0-9_.~:@+-]{1,200}'
RE_PEOPLE = re.compile(r'^people/(%s)$' % SEG)
RE_ENTRY = re.compile(r'^entries/(%s)/(days|reactions|replies|urges)/(%s)$' % (SEG, SEG))
RE_MUSIC = re.compile(r'^music/(%s)$' % SEG)
RE_FILE = re.compile(r'^[A-Za-z0-9_-]{8,80}$')
RE_USERNAME = re.compile(r'^[A-Za-z0-9_.-]{3,24}$')
CONFIG = 'pact/config'
DEFAULT_CONFIG = {
    'habits': [{'id': 'smoking', 'name': 'Smoking'}],
    'tags': ['gym', 'argument', 'exhausted from week', 'slept badly', 'exams', 'friends', 'family', 'work stress', 'outside', 'good food'],
}

# accepted upload types -> the type the file is stored and served as
TYPES = {
    'image/png': 'image/png', 'image/jpeg': 'image/jpeg', 'image/jpg': 'image/jpeg', 'image/webp': 'image/webp', 'image/gif': 'image/gif',
    'audio/mpeg': 'audio/mpeg', 'audio/mp3': 'audio/mpeg', 'audio/mp4': 'audio/mp4', 'audio/x-m4a': 'audio/mp4', 'audio/m4a': 'audio/mp4',
    'audio/aac': 'audio/aac', 'audio/x-aac': 'audio/aac', 'audio/ogg': 'audio/ogg', 'audio/opus': 'audio/ogg', 'audio/wav': 'audio/wav',
    'audio/x-wav': 'audio/wav', 'audio/wave': 'audio/wav', 'audio/vnd.wave': 'audio/wav', 'audio/flac': 'audio/flac', 'audio/x-flac': 'audio/flac',
    'audio/webm': 'audio/webm', 'video/mp4': 'video/mp4', 'video/webm': 'video/webm',
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, name TEXT NOT NULL,
  pw BLOB NOT NULL, salt BLOB NOT NULL, role TEXT NOT NULL DEFAULT 'member', created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created INTEGER NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS invites(
  code TEXT PRIMARY KEY, created_by TEXT, created INTEGER NOT NULL, expires INTEGER NOT NULL, used_by TEXT);
CREATE TABLE IF NOT EXISTS docs(path TEXT PRIMARY KEY, data TEXT NOT NULL, owner TEXT, updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, owner TEXT NOT NULL, type TEXT NOT NULL, size INTEGER NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS ipfails(
  ip TEXT PRIMARY KEY, fails INTEGER NOT NULL, banned INTEGER NOT NULL DEFAULT 0,
  first INTEGER NOT NULL, last INTEGER NOT NULL, last_user TEXT);
"""


def now():
    return int(time.time())


def log(*parts):
    sys.stderr.write(time.strftime('%Y-%m-%d %H:%M:%S ') + ' '.join(str(p) for p in parts) + '\n')
    sys.stderr.flush()


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def pw_hash(password, salt):
    return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=2 ** 14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)


def new_code():
    return ''.join(secrets.choice(ALPHA) for _ in range(10))


def show_code(code):
    return code[:5] + '-' + code[5:]


def norm_code(code):
    return re.sub(r'[^A-Z0-9]', '', str(code or '').upper())[:40]


class ApiError(Exception):
    def __init__(self, status, code, message=''):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---------------------------------------------------------------- storage

class Store:
    def __init__(self):
        os.makedirs(FILES, exist_ok=True)
        self.lock = threading.RLock()
        self.con = sqlite3.connect(os.path.join(DATA, 'habituality.db'), check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute('PRAGMA journal_mode=WAL')
        self.con.execute('PRAGMA synchronous=NORMAL')
        self.con.execute('PRAGMA busy_timeout=5000')
        self.con.execute('PRAGMA foreign_keys=ON')
        self.con.executescript(SCHEMA)

    def q(self, sql, args=()):
        with self.lock:
            return self.con.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    @contextmanager
    def tx(self):
        with self.lock:
            self.con.execute('BEGIN IMMEDIATE')
            try:
                yield self.con
                self.con.execute('COMMIT')
            except BaseException:
                self.con.execute('ROLLBACK')
                raise

    def user_count(self):
        return self.one('SELECT COUNT(*) AS n FROM users')['n']


store = None


class Hub:
    """Live updates: every signed-in tab keeps one event stream open."""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = set()

    def add(self):
        q = queue.Queue(maxsize=2000)
        q.dead = False
        with self.lock:
            self.clients.add(q)
        return q

    def remove(self, q):
        with self.lock:
            self.clients.discard(q)

    def send(self, event, payload):
        msg = 'event: %s\ndata: %s\n\n' % (event, json.dumps(payload, separators=(',', ':'), ensure_ascii=False))
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    q.dead = True
                    self.clients.discard(q)


HUB = Hub()


class Limiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.hits = {}

    def blocked(self, key, limit, window=600):
        t = time.time()
        with self.lock:
            xs = [x for x in self.hits.get(key, []) if t - x < window]
            if xs:
                self.hits[key] = xs
            else:
                self.hits.pop(key, None)
            return len(xs) >= limit

    def hit(self, *keys):
        t = time.time()
        with self.lock:
            for k in keys:
                self.hits.setdefault(k, []).append(t)


LIMIT = Limiter()


# ---------------------------------------------------------------- the page

WRAP_HEAD = (
    '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
    '<meta name="theme-color" content="#ECE2D0" media="(prefers-color-scheme: light)">\n'
    '<meta name="theme-color" content="#0C111A" media="(prefers-color-scheme: dark)">\n'
    '<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 64 64%22%3E'
    '%3Ccircle cx=%2232%22 cy=%2232%22 r=%2226%22 fill=%22%23D9661E%22/%3E%3C/svg%3E">\n'
)
_index = {'mtime': None, 'body': b''}


def index_body():
    st = os.stat(INDEX)
    if _index['mtime'] != st.st_mtime:
        with open(INDEX, encoding='utf-8') as f:
            txt = f.read()
        if not txt.lstrip().lower().startswith('<!doctype'):
            cut = txt.find('<header')
            cut = cut if cut >= 0 else 0
            txt = WRAP_HEAD + txt[:cut] + '</head>\n<body>\n' + txt[cut:] + '\n</body>\n</html>\n'
        _index['body'] = txt.encode('utf-8')
        _index['mtime'] = st.st_mtime
    return _index['body']


PAGE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


# ---------------------------------------------------------------- rules

def doc_path(qs):
    p = (qs.get('path') or [''])[0]
    if len(p) > 700 or not (p == CONFIG or RE_PEOPLE.match(p) or RE_ENTRY.match(p) or RE_MUSIC.match(p)):
        raise ApiError(400, 'invalid_argument', 'That path is not allowed.')
    return p


def may_write(user, path, owner):
    """people/<you> and entries/<you>/... are yours alone; the shared habit list is everyone's;
    a song can be changed by whoever added it, or by the owner account."""
    m = RE_PEOPLE.match(path)
    if m:
        return m.group(1) == user['id']
    m = RE_ENTRY.match(path)
    if m:
        return m.group(1) == user['id']
    if path == CONFIG:
        return True
    if RE_MUSIC.match(path):
        return owner is None or owner == user['id'] or user['role'] == 'owner'
    return False


def dump(data):
    raw = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
    if len(raw.encode('utf-8')) > MAX_DOC:
        raise ApiError(413, 'too_big', 'That is too much for one entry.')
    return raw


def looks_like(kind, head):
    if kind == 'image/png':
        return head.startswith(b'\x89PNG\r\n\x1a\n')
    if kind == 'image/jpeg':
        return head.startswith(b'\xff\xd8\xff')
    if kind == 'image/gif':
        return head[:6] in (b'GIF87a', b'GIF89a')
    if kind == 'image/webp':
        return head[:4] == b'RIFF' and head[8:12] == b'WEBP'
    return head.lstrip()[:1] != b'<'


def public_user(row):
    return {'id': row['id'], 'username': row['username'], 'name': row['name'], 'role': row['role']}


# ---------------------------------------------------------------- passwords + blocking

_common = None


def common_passwords():
    global _common
    if _common is None:
        try:
            with gzip.open(COMMON_FILE, 'rt', encoding='utf-8') as f:
                _common = {line.strip() for line in f if line.strip() and not line.startswith('#')}
        except OSError:
            log('common-passwords.txt.gz is missing; only the length and pattern rules apply')
            _common = set()
    return _common


SEQUENCES = ('0123456789' * 3, 'abcdefghijklmnopqrstuvwxyz' * 2, 'qwertyuiopasdfghjklzxcvbnm' * 2,
             'qwertzuiopasdfghjklyxcvbnm' * 2, 'azertyuiopqsdfghjklmwxcvbn' * 2)


def password_problem(pw, username='', name=''):
    """None if the password is fine, otherwise a short reason to show."""
    if len(pw) < MIN_PASSWORD:
        return 'Use at least %d characters. 3–4 random words work well, like blue-kettle-rides-north.' % MIN_PASSWORD
    if len(pw) > 200:
        return 'That password is too long.'
    low = pw.lower()
    if len(set(low)) < 5:
        return 'That password repeats itself too much. Mix in more different characters.'
    if any(low in s or low[::-1] in s for s in SEQUENCES):
        return 'That is a keyboard or number sequence. Pick something less predictable.'
    for part in [username] + name.split():
        if len(part) >= 4 and part.lower() in low:
            return "Don't put your name or username in the password."
    common = common_passwords()
    if low in common or low.rstrip('0123456789!?.*#') in common:
        return 'That password is on a list of common passwords. Pick another one.'
    return None


def ip_key(raw):
    """One key per visitor address. IPv6 is grouped by /64, because one home or phone gets a whole /64."""
    try:
        ip = ipaddress.ip_address(str(raw).strip().strip('[]'))
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.version == 6:
        return str(ipaddress.ip_network('%s/64' % ip, strict=False))
    return str(ip)


BANNED_MSG = 'Too many wrong tries from your network (%s). Ask the owner to unblock it.'


def check_ban(ipk):
    if ipk and store.one('SELECT 1 FROM ipfails WHERE ip = ? AND banned = 1', (ipk,)):
        raise ApiError(403, 'banned', BANNED_MSG % ipk)


def note_fail(ipk, tried):
    """Count a wrong password / wrong code from this address. Returns the count in a row."""
    if not ipk:
        return 0
    t = now()
    with store.tx() as c:
        c.execute('INSERT INTO ipfails(ip, fails, banned, first, last, last_user) VALUES (?, 1, 0, ?, ?, ?) '
                  'ON CONFLICT(ip) DO UPDATE SET fails = fails + 1, last = excluded.last, last_user = excluded.last_user',
                  (ipk, t, t, str(tried)[:40]))
        fails = c.execute('SELECT fails FROM ipfails WHERE ip = ?', (ipk,)).fetchone()[0]
        if fails >= BAN_AFTER:
            c.execute('UPDATE ipfails SET banned = 1 WHERE ip = ?', (ipk,))
    if fails == BAN_AFTER:
        log('BLOCKED %s after %d wrong tries in a row (last tried: %r). To unblock: habituality unban %s' % (ipk, fails, str(tried)[:24], ipk))
    return fails


def note_ok(ipk):
    if ipk:
        store.q('DELETE FROM ipfails WHERE ip = ? AND banned = 0', (ipk,))


def wrong(ipk, fails, code, message):
    if fails >= BAN_AFTER:
        raise ApiError(403, 'banned', BANNED_MSG % ipk)
    left = BAN_AFTER - fails
    if fails and left <= 3:
        message += ' %d %s left before this network is blocked.' % (left, 'try' if left == 1 else 'tries')
    raise ApiError(400, code, message)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'habituality'
    sys_version = ''
    timeout = 90

    def log_message(self, fmt, *args):
        pass

    def log_request(self, code='-', size='-'):
        try:
            if int(code) >= 400:
                log('%s %s -> %s' % (self.command, urlsplit(self.path).path, int(code)))
        except (TypeError, ValueError):
            pass

    # responses
    def send(self, status, body=b'', ctype='application/json; charset=utf-8', headers=None):
        hdrs = {'Cache-Control': 'no-store'}
        hdrs.update(headers or {})
        self.send_response(status)
        if status != 204:
            self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        for k, v in hdrs.items():
            for one in (v if isinstance(v, list) else [v]):
                self.send_header(k, one)
        self.end_headers()
        if self.command != 'HEAD' and body:
            self.wfile.write(body)

    def json(self, status, obj, headers=None):
        self.send(status, json.dumps(obj, separators=(',', ':'), ensure_ascii=False).encode('utf-8'), headers=headers)

    # requests
    def length(self, limit):
        if self.headers.get('Transfer-Encoding'):
            raise ApiError(411, 'invalid_argument', 'Send a Content-Length.')
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            raise ApiError(400, 'invalid_argument', 'Bad Content-Length.')
        if n < 0:
            raise ApiError(400, 'invalid_argument', 'Bad Content-Length.')
        if n > limit:
            raise ApiError(413, 'too_large', 'That is too big.')
        return n

    def body_json(self):
        n = self.length(MAX_JSON)
        raw = self.rfile.read(n) if n else b''
        if not raw:
            return {}
        try:
            return json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, 'invalid_argument', 'Bad JSON.')

    def client_ip(self):
        """The visitor's address as a block key, or None if unknown. Tailscale Serve/Funnel
        overwrite X-Forwarded-For with the real source address, so the header is trusted only
        on connections from this machine (that is tailscaled). Direct local requests return None."""
        remote = self.client_address[0]
        if remote in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
            fwd = (self.headers.get('X-Forwarded-For') or '').split(',')[-1].strip()
            return ip_key(fwd) if fwd else None
        return ip_key(remote)

    def is_secure(self):
        if SECURE in ('1', 'true', 'yes', 'on'):
            return True
        if SECURE in ('0', 'false', 'no', 'off'):
            return False
        host = (self.headers.get('Host') or '').split(':')[0].lower()
        return (self.headers.get('X-Forwarded-Proto') or '').lower() == 'https' or host.endswith('.ts.net')

    def cookie(self, token, max_age):
        parts = ['hb_session=%s' % token, 'Path=/', 'HttpOnly', 'SameSite=Lax', 'Max-Age=%d' % max_age]
        if self.is_secure():
            parts.append('Secure')
        return '; '.join(parts)

    def token(self):
        raw = self.headers.get('Cookie')
        if not raw:
            return None
        c = http.cookies.SimpleCookie()
        try:
            c.load(raw)
        except http.cookies.CookieError:
            return None
        m = c.get('hb_session')
        return m.value if m and 20 <= len(m.value) <= 200 else None

    def current_user(self):
        tok = self.token()
        if not tok:
            return None
        row = store.one('SELECT u.id, u.username, u.name, u.role, s.expires, s.token FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?', (sha(tok),))
        if not row or row['expires'] < now():
            return None
        return dict(row)

    def need_user(self):
        u = self.current_user()
        if not u:
            raise ApiError(401, 'unauthenticated', 'Sign in first.')
        return u

    # routing
    def do_GET(self):
        self.route('GET')

    def do_HEAD(self):
        self.route('HEAD')

    def do_POST(self):
        self.route('POST')

    def do_PUT(self):
        self.route('PUT')

    def do_PATCH(self):
        self.route('PATCH')

    def do_DELETE(self):
        self.route('DELETE')

    def route(self, method):
        try:
            u = urlsplit(self.path)
            path, qs = u.path, parse_qs(u.query)
            if method in ('POST', 'PUT', 'PATCH', 'DELETE') and self.headers.get('X-HB') != '1':
                raise ApiError(403, 'forbidden', 'Missing X-HB header.')
            if path in ('/', '/index.html') and method in ('GET', 'HEAD'):
                return self.send(200, index_body(), 'text/html; charset=utf-8', {
                    'Cache-Control': 'no-cache', 'Content-Security-Policy': PAGE_CSP, 'X-Frame-Options': 'DENY'})
            if path == '/favicon.ico':
                return self.send(204)
            if path.startswith('/_blob/') and method in ('GET', 'HEAD'):
                return self.blob(self.need_user(), path[len('/_blob/'):])
            if path.startswith('/api/'):
                fn = ROUTES.get(('GET' if method == 'HEAD' else method, path[len('/api/'):]))
                if fn:
                    return fn(self, qs)
            raise ApiError(404, 'not_found', 'Not found.')
        except ApiError as e:
            self.close_connection = True
            try:
                self.json(e.status, {'code': e.code, 'message': e.message})
            except OSError:
                pass
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            traceback.print_exc()
            self.close_connection = True
            try:
                self.json(500, {'code': 'internal', 'message': 'Something went wrong on the server.'})
            except OSError:
                pass

    # files
    def blob(self, user, fid):
        if not RE_FILE.match(fid):
            raise ApiError(404, 'not_found', 'Not found.')
        row = store.one('SELECT type, size FROM files WHERE id = ?', (fid,))
        fpath = os.path.join(FILES, fid)
        if not row or not os.path.isfile(fpath):
            raise ApiError(404, 'not_found', 'Not found.')
        size = os.path.getsize(fpath)
        start, end, status = 0, size - 1, 200
        rng = (self.headers.get('Range') or '').strip()
        m = re.match(r'^bytes=(\d*)-(\d*)$', rng)
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header('Content-Range', 'bytes */%d' % size)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header('Content-Type', row['type'])
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Cache-Control', 'private, max-age=31536000, immutable')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; sandbox")
        self.send_header('Content-Disposition', 'inline')
        if status == 206:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        self.end_headers()
        if self.command == 'HEAD':
            return
        with open(fpath, 'rb') as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(256 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    # live stream
    def stream(self, user):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-transform')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        q = HUB.add()
        checked = time.time()

        def chunk(text):
            b = text.encode('utf-8')
            self.wfile.write(b'%x\r\n' % len(b) + b + b'\r\n')
            self.wfile.flush()

        try:
            chunk('retry: 3000\n: hello\n\n')
            while not q.dead:
                try:
                    msg = q.get(timeout=20)
                except queue.Empty:
                    msg = ': ping\n\n'
                chunk(msg)
                if time.time() - checked > 60:
                    checked = time.time()
                    if not self.current_user():
                        break
            try:
                self.wfile.write(b'0\r\n\r\n')
            except OSError:
                pass
        except OSError:
            pass
        finally:
            HUB.remove(q)
            self.close_connection = True


# ---------------------------------------------------------------- API

def api_me(h, qs):
    u = h.current_user()
    if not u:
        return h.json(401, {'code': 'unauthenticated', 'setup': store.user_count() == 0})
    headers = {}
    if u['expires'] - now() < SESSION_TTL - 86400:
        store.q('UPDATE sessions SET expires = ? WHERE token = ?', (now() + SESSION_TTL, u['token']))
        headers['Set-Cookie'] = h.cookie(h.token(), SESSION_TTL)
    h.json(200, public_user(u), headers)


def new_session(uid):
    tok = secrets.token_urlsafe(32)
    store.q('INSERT INTO sessions(token, user_id, created, expires) VALUES (?,?,?,?)', (sha(tok), uid, now(), now() + SESSION_TTL))
    return tok


def api_login(h, qs):
    b = h.body_json()
    username = str(b.get('username', '')).strip()[:64]
    password = str(b.get('password', ''))[:200]
    ipk = h.client_ip()
    check_ban(ipk)
    # Second layer against many addresses guessing one name: after 10 wrong tries on a name in
    # 10 minutes, addresses that have already been wrong must wait. An address with a clean
    # record always gets through, so a bot can't lock a friend out of their own account.
    keys = ('user:' + username.lower(), 'all')
    clean = bool(ipk) and not store.one('SELECT 1 FROM ipfails WHERE ip = ?', (ipk,))
    if not clean and (LIMIT.blocked(keys[0], 10) or LIMIT.blocked(keys[1], 200)):
        # trying again while told to wait also counts toward the block
        if note_fail(ipk, username) >= BAN_AFTER:
            raise ApiError(403, 'banned', BANNED_MSG % ipk)
        raise ApiError(429, 'rate_limited', 'Too many tries. Wait ten minutes and try again.')
    row = store.one('SELECT * FROM users WHERE username = ?', (username,))
    ok = False
    if row:
        ok = hmac.compare_digest(pw_hash(password, row['salt']), row['pw'])
    else:
        pw_hash(password, b'0' * 16)
    if not ok:
        LIMIT.hit(*keys)
        fails = note_fail(ipk, username)
        log('login failed for', repr(username[:24]), 'from', ipk or 'this machine', '(%d in a row)' % fails if fails else '')
        wrong(ipk, fails, 'bad_login', 'Wrong username or password.')
    note_ok(ipk)
    tok = new_session(row['id'])
    h.json(200, public_user(row), {'Set-Cookie': h.cookie(tok, SESSION_TTL)})


def api_signup(h, qs):
    b = h.body_json()
    name = ' '.join(str(b.get('name', '')).split())[:40]
    username = str(b.get('username', '')).strip()
    password = str(b.get('password', ''))
    code = norm_code(b.get('code', ''))
    ipk = h.client_ip()
    check_ban(ipk)
    if LIMIT.blocked('code', 100):
        raise ApiError(429, 'rate_limited', 'Too many tries. Wait ten minutes and try again.')
    if not name:
        raise ApiError(400, 'invalid_argument', 'Add your name.')
    if not RE_USERNAME.match(username):
        raise ApiError(400, 'invalid_argument', 'Usernames are 3–24 letters, numbers, dots, dashes or underscores.')
    problem = password_problem(password, username, name)
    if problem:
        raise ApiError(400, 'weak_password', problem)
    salt = secrets.token_bytes(16)
    ph = pw_hash(password, salt)
    uid = 'u_' + secrets.token_urlsafe(12)
    with store.tx() as c:
        n = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        role = None
        if n == 0:
            r = c.execute("SELECT v FROM meta WHERE k = 'setup_code'").fetchone()
            if r and code and hmac.compare_digest(code, r[0]):
                role = 'owner'
        elif code:
            r = c.execute('SELECT code FROM invites WHERE code = ? AND used_by IS NULL AND expires > ?', (code, now())).fetchone()
            if r:
                role = 'member'
        if not role:
            bad_code = "That setup code isn't right. It's in the server log." if n == 0 else "That invite code doesn't work. It may be used up or expired."
        else:
            bad_code = None
            if c.execute('SELECT 1 FROM users WHERE username = ?', (username,)).fetchone():
                raise ApiError(409, 'taken', 'That username is taken. Pick another one.')
            c.execute('INSERT INTO users(id, username, name, pw, salt, role, created) VALUES (?,?,?,?,?,?,?)', (uid, username, name, ph, salt, role, now()))
            if role == 'owner':
                c.execute("DELETE FROM meta WHERE k = 'setup_code'")
            else:
                c.execute('UPDATE invites SET used_by = ? WHERE code = ?', (uid, code))
    if bad_code:
        # counted outside the transaction above, so the count is kept
        LIMIT.hit('code')
        wrong(ipk, note_fail(ipk, 'code ' + code[:12]), 'bad_code', bad_code)
    note_ok(ipk)
    if role == 'owner':
        try:
            os.remove(os.path.join(DATA, 'setup-code.txt'))
        except OSError:
            pass
    log('new account', repr(username), 'as', role)
    tok = new_session(uid)
    HUB.send('users', {})
    h.json(200, {'id': uid, 'username': username, 'name': name, 'role': role}, {'Set-Cookie': h.cookie(tok, SESSION_TTL)})


def api_logout(h, qs):
    h.body_json()
    tok = h.token()
    if tok:
        store.q('DELETE FROM sessions WHERE token = ?', (sha(tok),))
    h.json(200, {'ok': True}, {'Set-Cookie': h.cookie('x', 0)})


def api_users(h, qs):
    h.need_user()
    rows = store.q('SELECT id, username, name, role FROM users ORDER BY created')
    h.json(200, {'users': [public_user(r) for r in rows]})


def api_invites(h, qs):
    u = h.need_user()
    h.body_json()
    open_n = store.one('SELECT COUNT(*) AS n FROM invites WHERE created_by = ? AND used_by IS NULL AND expires > ?', (u['id'], now()))['n']
    if open_n >= 10:
        raise ApiError(429, 'rate_limited', 'You already have 10 unused invites. Wait until some are used.')
    code = new_code()
    store.q('INSERT INTO invites(code, created_by, created, expires) VALUES (?,?,?,?)', (code, u['id'], now(), now() + INVITE_TTL))
    h.json(200, {'code': show_code(code), 'expires': now() + INVITE_TTL})


def api_docs(h, qs):
    h.need_user()
    rows = store.q('SELECT path, data FROM docs')
    body = '{"docs":[' + ','.join('{"path":%s,"data":%s}' % (json.dumps(r['path']), r['data']) for r in rows) + ']}'
    h.send(200, body.encode('utf-8'))


def api_doc_put(h, qs):
    u = h.need_user()
    path = doc_path(qs)
    data = h.body_json()
    if not isinstance(data, dict):
        raise ApiError(400, 'invalid_argument', 'A document must be an object.')
    with store.tx() as c:
        r = c.execute('SELECT owner FROM docs WHERE path = ?', (path,)).fetchone()
        owner = r[0] if r else None
        if not may_write(u, path, owner):
            raise ApiError(403, 'forbidden', "You can't change that.")
        if path.startswith('music/'):
            data['by'] = owner or u['id']
        raw = dump(data)
        c.execute('INSERT INTO docs(path, data, owner, updated) VALUES (?,?,?,?) '
                  'ON CONFLICT(path) DO UPDATE SET data = excluded.data, updated = excluded.updated',
                  (path, raw, owner or u['id'], now()))
    HUB.send('doc', {'path': path, 'data': data})
    h.json(200, {'ok': True})


def api_doc_patch(h, qs):
    u = h.need_user()
    path = doc_path(qs)
    patch = h.body_json()
    if not isinstance(patch, dict):
        raise ApiError(400, 'invalid_argument', 'A patch must be an object.')
    with store.tx() as c:
        r = c.execute('SELECT owner, data FROM docs WHERE path = ?', (path,)).fetchone()
        if not r:
            raise ApiError(404, 'invalid_argument', 'Nothing to update yet.')
        if not may_write(u, path, r[0]):
            raise ApiError(403, 'forbidden', "You can't change that.")
        data = json.loads(r[1])
        data.update(patch)
        if path.startswith('music/'):
            data['by'] = r[0]
        c.execute('UPDATE docs SET data = ?, updated = ? WHERE path = ?', (dump(data), now(), path))
    HUB.send('doc', {'path': path, 'data': data})
    h.json(200, {'data': data})


def api_doc_delete(h, qs):
    u = h.need_user()
    path = doc_path(qs)
    with store.tx() as c:
        r = c.execute('SELECT owner FROM docs WHERE path = ?', (path,)).fetchone()
        if not may_write(u, path, r[0] if r else None):
            raise ApiError(403, 'forbidden', "You can't change that.")
        if r:
            c.execute('DELETE FROM docs WHERE path = ?', (path,))
    if r:
        HUB.send('doc', {'path': path, 'data': None})
    h.json(200, {'ok': True})


def api_stream(h, qs):
    h.stream(h.need_user())


def api_upload(h, qs):
    u = h.need_user()
    ctype = (h.headers.get('Content-Type') or '').split(';')[0].strip().lower()
    kind = TYPES.get(ctype)
    if not kind:
        raise ApiError(415, 'unsupported_type', "That kind of file isn't accepted.")
    n = h.length(MAX_UPLOAD)
    if n == 0:
        raise ApiError(400, 'invalid_argument', 'The file is empty.')
    used = store.one('SELECT COALESCE(SUM(size), 0) AS s FROM files WHERE owner = ?', (u['id'],))['s']
    if used + n > USER_QUOTA:
        raise ApiError(413, 'quota_or_state', 'Your storage is full. Remove some songs or photos first.')
    if shutil.disk_usage(DATA).free - n < KEEP_FREE:
        raise ApiError(507, 'quota_or_state', 'The server disk is almost full.')
    fid = secrets.token_urlsafe(18)
    tmp = os.path.join(FILES, fid + '.part')
    head = b''
    try:
        with open(tmp, 'wb') as f:
            left = n
            while left > 0:
                chunk = h.rfile.read(min(256 * 1024, left))
                if not chunk:
                    raise ApiError(400, 'invalid_argument', 'The upload was cut off.')
                if len(head) < 32:
                    head += chunk[:32 - len(head)]
                f.write(chunk)
                left -= len(chunk)
        if not looks_like(kind, head):
            raise ApiError(415, 'unsupported_type', "That file doesn't match its type.")
        os.replace(tmp, os.path.join(FILES, fid))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    store.q('INSERT INTO files(id, owner, type, size, created) VALUES (?,?,?,?,?)', (fid, u['id'], kind, n, now()))
    h.json(200, {'id': fid, 'url': '/_blob/' + fid, 'type': kind, 'size': n})


def api_upload_delete(h, qs):
    u = h.need_user()
    fid = (qs.get('id') or [''])[0]
    if not RE_FILE.match(fid):
        raise ApiError(400, 'invalid_argument', 'Bad file id.')
    r = store.one('SELECT owner FROM files WHERE id = ?', (fid,))
    if r:
        if r['owner'] != u['id'] and u['role'] != 'owner':
            raise ApiError(403, 'forbidden', "You can't remove that file.")
        store.q('DELETE FROM files WHERE id = ?', (fid,))
        try:
            os.remove(os.path.join(FILES, fid))
        except OSError:
            pass
    h.json(200, {'deleted': True})


ROUTES = {
    ('GET', 'me'): api_me,
    ('POST', 'login'): api_login,
    ('POST', 'signup'): api_signup,
    ('POST', 'logout'): api_logout,
    ('GET', 'users'): api_users,
    ('POST', 'invites'): api_invites,
    ('GET', 'docs'): api_docs,
    ('PUT', 'doc'): api_doc_put,
    ('PATCH', 'doc'): api_doc_patch,
    ('DELETE', 'doc'): api_doc_delete,
    ('GET', 'stream'): api_stream,
    ('POST', 'upload'): api_upload,
    ('DELETE', 'upload'): api_upload_delete,
}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


# ---------------------------------------------------------------- startup + housekeeping

def ensure_config():
    if not store.one('SELECT 1 FROM docs WHERE path = ?', (CONFIG,)):
        store.q('INSERT INTO docs(path, data, owner, updated) VALUES (?,?,?,?)', (CONFIG, dump(dict(DEFAULT_CONFIG, updatedAt=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))), None, now()))


def ensure_setup_code():
    path = os.path.join(DATA, 'setup-code.txt')
    if store.user_count():
        store.q("DELETE FROM meta WHERE k = 'setup_code'")
        if os.path.exists(path):
            os.remove(path)
        return None
    r = store.one("SELECT v FROM meta WHERE k = 'setup_code'")
    code = r['v'] if r else new_code()
    store.q("INSERT OR REPLACE INTO meta(k, v) VALUES ('setup_code', ?)", (code,))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(show_code(code) + '\n')
    return show_code(code)


def janitor():
    while True:
        time.sleep(3600)
        try:
            store.q('DELETE FROM sessions WHERE expires < ?', (now(),))
            store.q('DELETE FROM invites WHERE expires < ?', (now() - 30 * 86400,))
            # wrong-try counts that never reached a block are forgotten after 30 quiet days; blocks stay
            store.q('DELETE FROM ipfails WHERE banned = 0 AND last < ?', (now() - 30 * 86400,))
            for name in os.listdir(FILES):
                p = os.path.join(FILES, name)
                if name.endswith('.part') and time.time() - os.path.getmtime(p) > 3600:
                    os.remove(p)
        except Exception:
            traceback.print_exc()


def serve():
    global store
    os.umask(0o027)
    store = Store()
    store.q('DELETE FROM sessions WHERE expires < ?', (now(),))
    ensure_config()
    code = ensure_setup_code()
    common_passwords()
    if not os.path.isfile(INDEX):
        log('index.html is missing next to server.py:', INDEX)
        sys.exit(1)
    srv = Server((HOST, PORT), Handler)
    threading.Thread(target=janitor, daemon=True).start()
    log('habituality is running on http://%s:%d  (data: %s)' % (HOST, PORT, DATA))
    if code:
        log('No accounts yet. Open the site and create the owner account with this setup code: %s' % code)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cli(args):
    global store
    os.umask(0o027)
    store = Store()
    cmd = args[0]
    if cmd == 'invite':
        code = new_code()
        store.q('INSERT INTO invites(code, created_by, created, expires) VALUES (?,?,?,?)', (code, 'cli', now(), now() + INVITE_TTL))
        print('Invite code (one use, 7 days): %s' % show_code(code))
        print('Link: https://<your-funnel-address>/?invite=%s' % show_code(code))
    elif cmd == 'passwd' and len(args) == 2:
        row = store.one('SELECT id, username, name FROM users WHERE username = ?', (args[1],))
        if not row:
            sys.exit('No account called %r.' % args[1])
        pw = getpass.getpass('New password: ')
        problem = password_problem(pw, row['username'], row['name'])
        if problem:
            sys.exit(problem)
        if pw != getpass.getpass('Again: '):
            sys.exit('The two passwords are different.')
        salt = secrets.token_bytes(16)
        store.q('UPDATE users SET pw = ?, salt = ? WHERE id = ?', (pw_hash(pw, salt), salt, row['id']))
        store.q('DELETE FROM sessions WHERE user_id = ?', (row['id'],))
        print('Password changed. That account is signed out everywhere.')
    elif cmd == 'users':
        for r in store.q('SELECT username, name, role, created FROM users ORDER BY created'):
            print('%-24s %-24s %-7s since %s' % (r['username'], r['name'], r['role'], time.strftime('%Y-%m-%d', time.localtime(r['created']))))
    elif cmd == 'bans':
        rows = store.q('SELECT ip, fails, banned, last, last_user FROM ipfails ORDER BY banned DESC, last DESC')
        if not rows:
            print('Nobody is blocked, and there are no recent wrong tries.')
        for r in rows:
            print('%-9s %-28s %3d wrong in a row   last %s   tried %s' % (
                'BLOCKED' if r['banned'] else 'watching', r['ip'], r['fails'],
                time.strftime('%Y-%m-%d %H:%M', time.localtime(r['last'])), r['last_user'] or '-'))
        if any(r['banned'] for r in rows):
            print('\nUnblock one with:  habituality unban <address>     (or: habituality unban all)')
    elif cmd == 'unban' and len(args) == 2:
        with store.lock:
            if args[1] == 'all':
                n = store.con.execute('DELETE FROM ipfails').rowcount
            else:
                n = store.con.execute('DELETE FROM ipfails WHERE ip = ?', (ip_key(args[1]) or args[1],)).rowcount
        print('Unblocked %d address(es). It works right away, no restart needed.' % n if n else 'That address is not on the list. See: habituality bans')
    elif cmd == 'setup-code':
        code = ensure_setup_code()
        print(code or 'There are accounts already, so no setup code is needed.')
    elif cmd == 'backup':
        dest = os.path.abspath(args[1] if len(args) > 1 else os.path.join(DATA, 'backups'))
        os.makedirs(os.path.join(dest, 'files'), exist_ok=True)
        target = os.path.join(dest, 'habituality-%s.db' % time.strftime('%Y%m%d-%H%M%S'))
        out = sqlite3.connect(target)
        with store.lock:
            store.con.backup(out)
        out.close()
        copied = 0
        for name in os.listdir(FILES):
            src, dst = os.path.join(FILES, name), os.path.join(dest, 'files', name)
            if not name.endswith('.part') and not os.path.exists(dst):
                shutil.copy2(src, dst)
                copied += 1
        print('Saved %s and %d new upload(s) to %s' % (os.path.basename(target), copied, dest))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == '__main__':
    if len(sys.argv) > 1:
        cli(sys.argv[1:])
    else:
        serve()
