"""订单发货的持久化占用与库存预留；状态不确定时禁止自动重试。"""
import secrets
import time

ACTIVE_TOKENS=set()


def initialize(db):
    with db.lock:
        db.conn.executescript('''
            CREATE TABLE IF NOT EXISTS delivery_attempts (
                cookie_id TEXT NOT NULL, order_id TEXT NOT NULL, attempt_token TEXT NOT NULL,
                status TEXT NOT NULL, buyer_id TEXT, item_id TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(cookie_id,order_id)
            );
            CREATE TABLE IF NOT EXISTS delivery_stock (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cookie_id TEXT NOT NULL,
                order_id TEXT NOT NULL, attempt_token TEXT NOT NULL, card_id INTEGER NOT NULL,
                card_name TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
                buyer_id TEXT, item_id TEXT, user_id INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS delivery_stock_attempt ON delivery_stock(attempt_token,status);
        ''')


def is_blocked(db,cookie_id,order_id):
    with db.lock:
        row=db.conn.execute('SELECT system_shipped FROM orders WHERE order_id=? AND cookie_id=?',(order_id,cookie_id)).fetchone()
        if row and row[0]:return True
        row=db.conn.execute('SELECT status FROM delivery_attempts WHERE cookie_id=? AND order_id=?',(cookie_id,order_id)).fetchone()
        return bool(row and row[0]!='released')


def claim(db,cookie_id,order_id,buyer_id,item_id):
    if not order_id:return None
    with db.lock:
        db.conn.execute('BEGIN IMMEDIATE')
        try:
            if is_blocked(db,cookie_id,order_id):
                db.conn.rollback();return None
            token=secrets.token_hex(16);now=time.time()
            db.conn.execute('''INSERT INTO delivery_attempts VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(cookie_id,order_id) DO UPDATE SET attempt_token=excluded.attempt_token,
                status='claimed',buyer_id=excluded.buyer_id,item_id=excluded.item_id,
                created_at=excluded.created_at,updated_at=excluded.updated_at''',
                (cookie_id,order_id,token,'claimed',buyer_id,item_id,now,now))
            db.conn.commit();ACTIVE_TOKENS.add(token);return token
        except Exception:db.conn.rollback();raise


def reserve(db,token,card_id,quantity):
    quantity=int(quantity)
    if quantity<1:raise ValueError('库存数量必须为正数')
    with db.lock:
        db.conn.execute('BEGIN IMMEDIATE')
        try:
            attempt=db.conn.execute("SELECT cookie_id,order_id,buyer_id,item_id,status FROM delivery_attempts WHERE attempt_token=?",(token,)).fetchone()
            if not attempt or attempt[4] not in ('claimed','reserved'):raise ValueError('发货状态不允许预留库存')
            existing=db.conn.execute("SELECT content FROM delivery_stock WHERE attempt_token=? AND card_id=? AND status='reserved' ORDER BY id",(token,card_id)).fetchall()
            if existing:
                db.conn.rollback();return [r[0] for r in existing]
            card=db.conn.execute('''SELECT c.data_content,c.name,c.user_id FROM cards c
                JOIN cookies a ON a.user_id=c.user_id WHERE a.id=? AND c.id=? AND c.type='data' AND c.enabled=1''',(attempt[0],card_id)).fetchone()
            lines=[v.strip() for v in (card[0] if card else '').splitlines() if v.strip()]
            if len(lines)<quantity:
                db.conn.rollback();return None
            chosen=lines[:quantity]
            db.conn.execute('UPDATE cards SET data_content=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',('\n'.join(lines[quantity:]),card_id))
            db.conn.executemany('''INSERT INTO delivery_stock(cookie_id,order_id,attempt_token,card_id,card_name,content,status,buyer_id,item_id,user_id)
                VALUES(?,?,?,?,?,?,'reserved',?,?,?)''',[(attempt[0],attempt[1],token,card_id,card[1],line,attempt[2],attempt[3],card[2]) for line in chosen])
            db.conn.execute("UPDATE delivery_attempts SET status='reserved',updated_at=? WHERE attempt_token=?",(time.time(),token))
            db.conn.commit();return chosen
        except Exception:db.conn.rollback();raise


def finish(db,cookie_id,order_id,completed=False,token=None):
    with db.lock:
        db.conn.execute('BEGIN IMMEDIATE')
        try:
            attempt=db.conn.execute('SELECT attempt_token,status FROM delivery_attempts WHERE cookie_id=? AND order_id=?',(cookie_id,order_id)).fetchone()
            if not attempt or (token and token!=attempt[0]) or attempt[1] in ('completed','released'):
                db.conn.rollback();return
            if completed:
                db.conn.execute('''INSERT INTO card_shipments(card_id,card_name,content,order_id,item_id,buyer_id,cookie_id,user_id)
                    SELECT card_id,card_name,content,order_id,item_id,buyer_id,cookie_id,user_id
                    FROM delivery_stock WHERE attempt_token=? AND status='reserved' ''',(attempt[0],))
                db.conn.execute("UPDATE delivery_stock SET status='delivered' WHERE attempt_token=? AND status='reserved'",(attempt[0],))
                db.conn.execute('UPDATE orders SET system_shipped=1 WHERE cookie_id=? AND order_id=?',(cookie_id,order_id))
            db.conn.execute('UPDATE delivery_attempts SET status=?,updated_at=? WHERE attempt_token=?',('completed' if completed else 'needs_review',time.time(),attempt[0]))
            db.conn.commit()
            ACTIVE_TOKENS.discard(attempt[0])
        except Exception:db.conn.rollback();raise


def release_after_review(db,cookie_id,order_id):
    with db.lock:
        db.conn.execute('BEGIN IMMEDIATE')
        try:
            row=db.conn.execute('SELECT attempt_token,status,updated_at FROM delivery_attempts WHERE cookie_id=? AND order_id=?',(cookie_id,order_id)).fetchone()
            if not row or row[1] in ('completed','released'):raise ValueError('该发货记录不能释放')
            if row[0] in ACTIVE_TOKENS:raise ValueError('发货仍在执行，禁止释放库存')
            if row[1]!='needs_review' and time.time()-row[2]<180:raise ValueError('发货可能仍在执行，请稍后核查')
            stock=db.conn.execute("SELECT card_id,content FROM delivery_stock WHERE attempt_token=? AND status='reserved' ORDER BY id",(row[0],)).fetchall()
            for card_id,content in stock:
                card=db.conn.execute('SELECT data_content FROM cards WHERE id=?',(card_id,)).fetchone()
                if not card:raise ValueError('原卡券不存在，请先恢复卡券再释放')
                db.conn.execute('UPDATE cards SET data_content=? WHERE id=?',('\n'.join(v for v in [card[0],content] if v),card_id))
            db.conn.execute("UPDATE delivery_stock SET status='released' WHERE attempt_token=?",(row[0],))
            db.conn.execute("UPDATE delivery_attempts SET status='released',updated_at=? WHERE attempt_token=?",(time.time(),row[0]))
            db.conn.commit()
        except Exception:db.conn.rollback();raise


def list_pending(db,user_id):
    with db.lock:
        rows=db.conn.execute('''SELECT a.cookie_id,a.order_id,a.status,a.updated_at,
            (SELECT count(*) FROM delivery_stock s WHERE s.attempt_token=a.attempt_token AND s.status='reserved')
            ,a.attempt_token
            FROM delivery_attempts a JOIN cookies c ON c.id=a.cookie_id WHERE c.user_id=?
            AND a.status NOT IN ('completed','released') ORDER BY a.updated_at DESC LIMIT 100''',(user_id,)).fetchall()
    return [{'cookie_id':r[0],'order_id':r[1],'status':r[2],'updated_at':r[3],'reserved_count':r[4],
             'reviewable':r[5] not in ACTIVE_TOKENS and (r[2]=='needs_review' or time.time()-r[3]>=180)} for r in rows]
