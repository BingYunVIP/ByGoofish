"""只保存脱敏的人工验证诊断，不记录凭证、页面源码或网络正文。"""
import re
import secrets
import time
from urllib.parse import urlsplit

RETENTION_SECONDS = 7 * 24 * 3600
MAX_EVENTS = 500
MAX_MESSAGE = 512
_URL = re.compile(r'https?://[^\s<>"\']+', re.I)
_SECRET_PAIR = re.compile(r'''(?ix)(["']?(?:cookie|set-cookie|authorization|password|passwd|token|accessToken|refreshToken|x5sec\w*|sign|signature|secret|api[_-]?key|cookie2|sgcookie|unb|_m_h5_tk\w*)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s;,}]+)''')


def safe_url(value):
    try:
        parsed = urlsplit(str(value))
        if parsed.scheme not in ('http', 'https'):
            return parsed.scheme + ':<已隐藏>' if parsed.scheme else '<地址已隐藏>'
        host = parsed.hostname or '<未知主机>'
        path = re.sub(r'(?<![A-Za-z])\d{6,}', '<id>', parsed.path)
        path = re.sub(r'[A-Za-z0-9_%=-]{24,}', '<已隐藏>', path)
        return f'{parsed.scheme}://{host}{path}'
    except (TypeError, ValueError):
        return '<地址已隐藏>'


def secret_values(cookie_string):
    return tuple(part.split('=', 1)[1].strip() for part in str(cookie_string or '').split(';')
                 if '=' in part and len(part.split('=', 1)[1].strip()) >= 3)


def sanitize(value, known_secrets=()):
    text = str(value or '')
    # 必须先移除整个查询串，再替换已知值；否则占位符会截断 URL 匹配。
    text = _URL.sub(lambda match: safe_url(match.group()), text)
    if re.search(r'''(?ix)["']?(?:(?:request|response)[_-]?)?(?:body|payload|headers|postdata)["']?\s*[:=]''', text):
        return '<正文或请求头转储已隐藏>'
    for secret in sorted(set(known_secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, '<已隐藏>')
    # Cookie 头包含多组分号分隔的值，必须整段移除；不能只隐藏第一项。
    text = re.sub(r'(?im)\b(?:set-cookie|cookie|authorization)\s*:\s*[^\n]+', '<凭证头已隐藏>', text)
    text = re.sub(r'(?i)\b(Bearer|Basic)\s+[^\s,;]+', r'\1 <已隐藏>', text)
    text = _SECRET_PAIR.sub(lambda match: match[1] + '<已隐藏>', text)
    text = re.sub(r'\b[A-Za-z0-9_%+/=-]{24,}\b', '<已隐藏>', text)
    text = re.sub(r'(?<!\d)\d{9,}(?!\d)', '<账号已隐藏>', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '<邮箱已隐藏>', text)
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)[:MAX_MESSAGE]


def initialize(db):
    with db.lock:
        db.conn.executescript('''
            CREATE TABLE IF NOT EXISTS captcha_diagnostics (
                id TEXT PRIMARY KEY, cookie_id TEXT NOT NULL, owner_id INTEGER NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'processing', stage TEXT NOT NULL DEFAULT 'created',
                failure_category TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '',
                close_reason TEXT NOT NULL DEFAULT '', event_count INTEGER NOT NULL DEFAULT 0,
                dropped_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS captcha_diagnostics_account ON captcha_diagnostics(cookie_id,created_at);
            CREATE TABLE IF NOT EXISTS captcha_diagnostic_events (
                diagnostic_id TEXT NOT NULL, seq INTEGER NOT NULL, created_at REAL NOT NULL,
                level TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL,
                PRIMARY KEY(diagnostic_id,seq)
            );
        ''')
        columns = {row[1] for row in db.conn.execute('PRAGMA table_info(risk_control_logs)')}
        if 'diagnostic_id' not in columns:
            db.conn.execute('ALTER TABLE risk_control_logs ADD COLUMN diagnostic_id TEXT')
        columns = {row[1] for row in db.conn.execute('PRAGMA table_info(captcha_diagnostics)')}
        if 'recovery_attempt' not in columns:
            db.conn.execute("ALTER TABLE captcha_diagnostics ADD COLUMN recovery_attempt TEXT NOT NULL DEFAULT ''")
        # 初始化认证服务已把中断会话置回待人工；对应诊断不能继续显示采集中。
        db.conn.execute("UPDATE captcha_diagnostics SET status='failed',stage='interrupted',"
                        "failure_category=CASE WHEN failure_category='' THEN 'interrupted' ELSE failure_category END "
                        "WHERE cookie_id IN (SELECT cookie_id FROM account_auth_recovery WHERE failure_category='interrupted') "
                        "AND (status='processing' OR stage='checking_connection')")
        db.conn.execute("UPDATE risk_control_logs SET processing_status='failed',error_message='interrupted',"
                        "processing_result='恢复检查被中断，仍需本人验证' WHERE processing_status='processing' AND "
                        "diagnostic_id IN (SELECT id FROM captcha_diagnostics WHERE stage='interrupted')")
        db.conn.commit()
        prune(db)


def prune(db):
    with db.lock:
        cutoff = time.time() - RETENTION_SECONDS
        db.conn.execute('DELETE FROM captcha_diagnostic_events WHERE diagnostic_id IN '
                        '(SELECT id FROM captcha_diagnostics WHERE created_at < ?)', (cutoff,))
        db.conn.execute('DELETE FROM captcha_diagnostics WHERE created_at < ?', (cutoff,))
        db.conn.commit()


def latest_id(db, cookie_id):
    with db.lock:
        row = db.conn.execute('SELECT id FROM captcha_diagnostics WHERE cookie_id=? AND created_at>=? '
                              'ORDER BY created_at DESC LIMIT 1', (cookie_id, time.time()-RETENTION_SECONDS)).fetchone()
    return row[0] if row else None


def recovery_result(db, cookie_id, attempt, state, category):
    """只更新绑定的恢复检查；验证页面结束不等于账号恢复。"""
    with db.lock:
        row = db.conn.execute('SELECT id FROM captcha_diagnostics WHERE cookie_id=? AND recovery_attempt=?',
                              (cookie_id, attempt)).fetchone()
        if not row:
            return
        ok = state == 'recovered'
        db.conn.execute('UPDATE captcha_diagnostics SET stage=?,status=?,failure_category=?,updated_at=? WHERE id=?',
                        (state, 'completed' if ok else 'failed', '' if ok else category, time.time(), row[0]))
        db.conn.execute('UPDATE risk_control_logs SET processing_status=?,processing_result=?,error_message=?,updated_at=CURRENT_TIMESTAMP '
                        'WHERE diagnostic_id=?', ('success' if ok else 'failed',
                        '令牌与消息连接已确认恢复' if ok else '认证检查未通过，请按账号事件对应操作处理', '' if ok else category, row[0]))
        db.conn.commit()


def read(db, diagnostic_id, owner_id, after_seq=0, limit=100):
    """诊断所有者与当前账号所有者必须同时匹配。返回前再次脱敏。"""
    prune(db)
    with db.lock:
        row = db.conn.execute('''SELECT d.* FROM captcha_diagnostics d JOIN cookies c ON c.id=d.cookie_id
            WHERE d.id=? AND d.owner_id=? AND c.user_id=? AND d.created_at>=?''',
            (diagnostic_id, owner_id, owner_id, time.time()-RETENTION_SECONDS))
        values = row.fetchone()
        if not values:
            return None
        meta = dict(zip([column[0] for column in row.description], values))
        cookie = db.get_cookie(meta['cookie_id'])
        known = secret_values(cookie)
        events = db.conn.execute('SELECT seq,created_at,level,kind,message FROM captcha_diagnostic_events '
                                 'WHERE diagnostic_id=? AND seq>? ORDER BY seq LIMIT ?',
                                 (diagnostic_id, max(0, after_seq), min(100, max(1, limit)))).fetchall()
    entries = [dict(seq=r[0], created_at=r[1], level=r[2], kind=r[3], message=sanitize(r[4], known)) for r in events]
    return {'diagnostic_id':meta['id'], 'status':meta['status'], 'stage':meta['stage'],
            'failure_category':meta['failure_category'], 'error_code':sanitize(meta['error_code'], known),
            'close_reason':meta['close_reason'], 'created_at':meta['created_at'],
            'dropped_count':meta['dropped_count'], 'event_count':meta['event_count'], 'entries':entries,
            'next_seq':entries[-1]['seq'] if entries else max(0, after_seq),
            'has_more':bool(entries and entries[-1]['seq'] < meta['event_count'])}


class Capture:
    @classmethod
    def existing(cls, db, diagnostic_id):
        """仅供已鉴权的会话收尾；不保留浏览器或旧凭证对象。"""
        capture = cls.__new__(cls)
        capture.db, capture.id, capture.secrets = db, diagnostic_id, ()
        capture._listeners = []
        return capture

    def __init__(self, db, cookie_id, owner_id, cookies_str=''):
        self.db, self.cookie_id = db, cookie_id
        self.id = secrets.token_hex(16)
        self.secrets = secret_values(cookies_str)
        self._listeners = []
        prune(db)
        now = time.time()
        with db.lock:
            db.conn.execute('INSERT INTO captcha_diagnostics(id,cookie_id,owner_id,created_at,updated_at) '
                            'VALUES(?,?,?,?,?)', (self.id,cookie_id,owner_id,now,now))
            db.conn.commit()
        self.emit('info','created','人工验证诊断已创建')

    def emit(self, level, kind, message):
        message = sanitize(message, self.secrets)
        with self.db.lock:
            row = self.db.conn.execute('SELECT event_count FROM captcha_diagnostics WHERE id=?', (self.id,)).fetchone()
            if not row:
                return
            if row[0] >= MAX_EVENTS:
                self.db.conn.execute('UPDATE captcha_diagnostics SET dropped_count=dropped_count+1 WHERE id=?', (self.id,))
            else:
                seq = row[0] + 1
                self.db.conn.execute('INSERT INTO captcha_diagnostic_events VALUES(?,?,?,?,?,?)',
                                     (self.id,seq,time.time(),level,kind,message))
                self.db.conn.execute('UPDATE captcha_diagnostics SET event_count=?,updated_at=? WHERE id=?', (seq,time.time(),self.id))
            self.db.conn.commit()

    def stage(self, name):
        with self.db.lock:
            self.db.conn.execute('UPDATE captcha_diagnostics SET stage=?,updated_at=? WHERE id=?', (name,time.time(),self.id))
            self.db.conn.commit()
        self.emit('info','stage',name)

    def fail(self, category, message, code=''):
        code = code if re.fullmatch(r'[A-Za-z0-9_-]{1,32}', str(code)) else ''
        code = sanitize(code, self.secrets)
        with self.db.lock:
            result = self.db.conn.execute("UPDATE captcha_diagnostics SET failure_category=?,error_code=?,status='failed',updated_at=? "
                                          "WHERE id=? AND failure_category=''", (category,code,time.time(),self.id))
            self.db.conn.commit()
        if result.rowcount:
            self.emit('error',category,message)
        return self.primary_failure()

    def primary_failure(self):
        with self.db.lock:
            row = self.db.conn.execute('SELECT failure_category,error_code FROM captcha_diagnostics WHERE id=?', (self.id,)).fetchone()
        return (row[0],row[1]) if row else ('','')

    def finish(self, reason):
        category, _ = self.primary_failure()
        status = 'failed' if category else 'cancelled' if reason=='cancelled' else 'completed' if reason=='completed' else 'failed'
        with self.db.lock:
            old = self.db.conn.execute('SELECT close_reason FROM captcha_diagnostics WHERE id=?', (self.id,)).fetchone()
            if old and old[0] == reason:
                return
            self.db.conn.execute('UPDATE captcha_diagnostics SET status=?,close_reason=?,updated_at=? WHERE id=?',
                                 (status,reason,time.time(),self.id))
            self.db.conn.commit()
        self.emit('info','closed',reason)

    def attach(self, context):
        started = {}
        def elapsed(request):
            stamp = started.pop(request, None)
            return f'{(time.monotonic()-stamp)*1000:.0f}ms' if stamp is not None else ''
        def console(message):
            if message.type in ('warning','error'):
                # Chromium的favicon/内部加载错误可能只有console事件。
                # 记录脱敏位置才能区分404图标和验证必需的脚本，绝不保存查询串。
                location = message.location or {}
                source = safe_url(location['url']) if location.get('url') else ''
                line = location.get('lineNumber')
                position = f' [来源 {source}:{line}]' if source else ''
                self.emit('error' if message.type=='error' else 'warning','console',message.text + position)
        def response(res):
            kind = res.request.resource_type
            content_type = res.headers.get('content-type','')
            if res.status >= 300 or (kind=='script' and 'text/html' in content_type):
                stamp = started.get(res.request)
                duration = f'{(time.monotonic()-stamp)*1000:.0f}ms' if stamp is not None else ''
                self.emit('warning','response',f'{res.status} {kind} {safe_url(res.url)} {content_type} {duration}')
        for event, callback in [('request',lambda request:started.__setitem__(request,time.monotonic())),
                                ('requestfinished',lambda request:elapsed(request)),
                                ('console',console),
                                ('requestfailed',lambda request:self.emit('error','requestfailed',f'{request.method} {request.resource_type} {safe_url(request.url)} {request.failure} {elapsed(request)}')),
                                ('response',response)]:
            context.on(event,callback)
            self._listeners.append((context,event,callback))
        def new_page(page):
            callback=lambda frame:self.emit('info','navigation',safe_url(frame.url))
            page.on('framenavigated',callback)
            self._listeners.append((page,'framenavigated',callback))
            error_callback=lambda error:self.emit('error','pageerror',str(error))
            page.on('pageerror',error_callback)
            self._listeners.append((page,'pageerror',error_callback))
        context.on('page',new_page)
        self._listeners.append((context,'page',new_page))

    def detach(self):
        for emitter,event,callback in self._listeners:
            emitter.remove_listener(event,callback)
        self._listeners.clear()
