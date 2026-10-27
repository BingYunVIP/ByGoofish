#!/usr/bin/env python3
"""真实临时SQLite验证发货占用/预留/核查/重启幂等，不调用平台接口。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1]

CODE=r'''
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from app.db_manager import db_manager as db, DBManager
from app.services import delivery_ledger as ledger
from app.services.user_backup import export_user, import_user
results=[]
def check(name,value):
    results.append({'check':name,'passed':bool(value)})
    assert value,name
uid=db.conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
db.save_cookie('ledger-account','unb=ledger-fixture',uid)
db.insert_or_update_order('ledger-order',cookie_id='ledger-account',item_id='item',buyer_id='buyer',order_status='pending_ship')
second=DBManager(db.db_path)
with ThreadPoolExecutor(2) as pool:
    claims=list(pool.map(lambda manager:ledger.claim(manager,'ledger-account','ledger-order','buyer','item'),[db,second]))
check('two real connections only one claim',sum(value is not None for value in claims)==1)
token=next(value for value in claims if value)
card=db.create_card('real-test-stock','data',data_content='one\ntwo',user_id=uid)
old_backup=export_user(db.conn,uid)
check('insufficient stock no partial debit',ledger.reserve(db,token,card,3) is None and db.get_card_by_id(card)['data_content']=='one\ntwo')
check('reserve returns complete batch',ledger.reserve(db,token,card,2)==['one','two'])
check('same attempt reserve idempotent',ledger.reserve(db,token,card,2)==['one','two'])
check('reservation not recorded as shipped',db.conn.execute('SELECT count(*) FROM card_shipments').fetchone()[0]==0)
try:
    db.delete_card(card,uid)
    delete_blocked=False
except ValueError:delete_blocked=True
check('reserved card cannot be deleted',delete_blocked)
ledger.finish(db,'ledger-account','ledger-order',token=token)
check('uncertain remains blocked',ledger.is_blocked(second,'ledger-account','ledger-order'))
check('uncertain retains reservation',db.conn.execute("SELECT count(*) FROM delivery_stock WHERE status='reserved'").fetchone()[0]==2)
ledger.release_after_review(db,'ledger-account','ledger-order')
check('manual release restores stock',db.get_card_by_id(card)['data_content']=='one\ntwo')
try:
    ledger.release_after_review(db,'ledger-account','ledger-order')
    double_release=False
except ValueError:double_release=True
check('manual release cannot duplicate stock',double_release)
token2=ledger.claim(second,'ledger-account','ledger-order','buyer','item')
check('reviewed release permits new attempt',bool(token2))
ledger.reserve(second,token2,card,2)
ledger.finish(second,'ledger-account','ledger-order',completed=True,token=token2)
ledger.finish(second,'ledger-account','ledger-order',completed=True,token=token2)
rows=db.conn.execute('SELECT order_id,cookie_id,buyer_id,item_id FROM card_shipments').fetchall()
check('completion records exactly once with ownership',len(rows)==2 and all(tuple(r)==('ledger-order','ledger-account','buyer','item') for r in rows))
import_user(db.conn,old_backup,uid)
check('old user backup cannot resurrect sold stock',db.get_card_by_id(card)['data_content']=='')
check('old user backup preserves delivery token',db.conn.execute('SELECT attempt_token,status FROM delivery_attempts').fetchone()==(token2,'completed'))
db.delete_card(card,uid)
check('completed card can be deleted',db.get_card_by_id(card) is None)
history=export_user(db.conn,uid)
check('deleted card shipment history roundtrip',import_user(db.conn,history,uid))
third=DBManager(db.db_path)
check('completed order blocked on reopened DB',ledger.claim(third,'ledger-account','ledger-order','buyer','item') is None)
db.insert_or_update_order('old-shipped',cookie_id='ledger-account',system_shipped=True)
check('old system_shipped blocks without ledger',ledger.claim(db,'ledger-account','old-shipped','buyer','item') is None)
check('database integrity',db.conn.execute('PRAGMA quick_check').fetchone()[0]=='ok')
import json
Path('result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
'''


def main():
    with tempfile.TemporaryDirectory(prefix='xianyu-delivery-check-') as directory:
        work=Path(directory)
        env={**os.environ,'PYTHONPATH':str(ROOT),'DB_PATH':str(work/'test.db'),
             'SQL_LOG_ENABLED':'false','PYTHONDONTWRITEBYTECODE':'1','ADMIN_PASSWORD':'local-fixture-password-only'}
        result=subprocess.run([sys.executable,'-B','-c',CODE],cwd=work,env=env,capture_output=True,text=True)
        if result.returncode:
            print(result.stderr[-1600:]);raise SystemExit(result.returncode)
        data=json.loads((work/'result.json').read_text())
        for row in data:print('[OK] '+row['check'])
        if '--record' in sys.argv:
            (ROOT/'Custom/delivery-smoke.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
        print(f'{len(data)} actual SQLite checks passed; no platform transactions')


if __name__=='__main__':main()
