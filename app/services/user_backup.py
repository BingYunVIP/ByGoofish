"""用户备份：固定表范围、服务端字段校验、所有权校验、事务合并恢复。"""
import json
import re
import time

# 插入顺序为父表在前；清理顺序反转。全局设置、用户、会话和API密钥不导入。
SCOPES = {
    'cookies':'user_id', 'cards':'user_id', 'notification_channels':'user_id',
    'logistics_quote_books':'user_id', 'logistics_quote_route_imports':'user_id',
    'logistics_quote_routes':'user_id', 'product_filter_rules':'user_id',
    'product_delete_rules':'user_id', 'product_materials':'user_id',
    'cookie_status':'cookie_id', 'keywords':'cookie_id', 'default_replies':'cookie_id',
    'chat_emoji_cache':'cookie_id', 'chat_action_diagnostics':'cookie_id',
    'auto_reply_activation':'cookie_id', 'auto_reply_receipts':'cookie_id', 'auto_reply_outbound':'cookie_id', 'auto_reply_image_cache':'cookie_id',
    'default_reply_records':'cookie_id', 'message_notifications':'cookie_id',
    'item_info':'cookie_id', 'item_replay':'cookie_id', 'ai_reply_settings':'cookie_id',
    'ai_conversations':'cookie_id', 'orders':'cookie_id', 'risk_control_logs':'cookie_id',
    'auto_reply_message_logs':'cookie_id', 'message_filters':'cookie_id',
    'delivery_rules':'user_id', 'item_delivery_configs':'user_id',
    'product_variants':'user_id', 'variant_delivery_bindings':'user_id',
    'delivery_sku_options':'cookie_id', 'delivery_sku_rules':'cookie_id',
    'delivery_sku_claims':'cookie_id', 'delivery_block_rules':'account_id',
    'personal_blacklist':'owner_id', 'card_shipments':'user_id', 'user_settings':'user_id',
    'logistics_agent_settings':'cookie_id', 'logistics_quote_sessions':'cookie_id',
    'logistics_quote_send_logs':'cookie_id', 'logistics_agent_training_samples':'user_id',
    'logistics_agent_training_rounds':'user_id', 'automation_task_runs':'user_id',
    'delivery_attempts':'cookie_id', 'delivery_stock':'cookie_id',
    'buyer_interaction_attempts':'cookie_id', 'buyer_interaction_activation':'cookie_id',
    'chat_workspace_state':'cookie_id','chat_customer_notes':'cookie_id',
    'chat_handovers':'cookie_id','chat_send_attempts':'cookie_id',
    'account_auth_clients':'cookie_id',
    'buyer_notification_activation':'cookie_id', 'buyer_notification_receipts':'cookie_id',
    'account_offline_incidents':'cookie_id', 'account_offline_notices':'cookie_id',
    'delivery_reconciliation_activation':'cookie_id','delivery_reconciliation_runs':'cookie_id',
    'operational_incidents':'cookie_id','operational_notices':'incident_id','operational_notice_attempts':'incident_id',
}
RELATIONS = {'card_id':'cards', 'auto_card_id':'cards', 'channel_id':'notification_channels',
             'variant_id':'product_variants', 'import_id':'logistics_quote_route_imports'}


def ident(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z_][a-z0-9_]*',value):
        raise ValueError('备份包含无效字段')
    return '"'+value+'"'


def schema(conn, table):
    return list(conn.execute(f'PRAGMA table_info({ident(table)})'))


def condition(column):
    if column in ('cookie_id','account_id'):
        return f'{ident(column)} IN (SELECT id FROM cookies WHERE user_id=?)'
    if column == 'incident_id':
        return f'{ident(column)} IN (SELECT id FROM operational_incidents WHERE cookie_id IN (SELECT id FROM cookies WHERE user_id=?))'
    return f'{ident(column)}=?'


def export_user(conn, user_id):
    data={}
    for table,scope in SCOPES.items():
        if not schema(conn,table):continue
        cursor=conn.execute(f'SELECT * FROM {ident(table)} WHERE {condition(scope)}',(user_id,))
        data[table]={'columns':[c[0] for c in cursor.description],'rows':[list(r) for r in cursor.fetchall()]}
    return {'version':'2.0','timestamp':time.time(),'user_id':user_id,
            'restore_mode':'merge','excluded':['global settings','auth sessions','API keys','global caches'],'data':data}


def import_user(conn, payload, user_id):
    if not isinstance(payload,dict) or not isinstance(payload.get('data'),dict):
        raise ValueError('备份数据格式无效')
    incoming=payload['data']
    if set(incoming)-set(SCOPES):raise ValueError('用户备份包含不允许导入的表')
    tables={}
    for table,part in incoming.items():
        info=schema(conn,table);allowed={r[1] for r in info}
        if not info or not isinstance(part,dict):raise ValueError('备份表结构无效')
        columns=part.get('columns');rows=part.get('rows')
        if not isinstance(columns,list) or not columns or len(set(columns))!=len(columns):raise ValueError('备份字段无效')
        if not set(columns)<=allowed:raise ValueError('备份包含未知字段')
        for col in columns:ident(col)
        if not isinstance(rows,list) or len(rows)>100000:raise ValueError('备份行数超限')
        parsed=[]
        for row in rows:
            if not isinstance(row,list) or len(row)!=len(columns):raise ValueError('备份行列数量不一致')
            if any(value is not None and not isinstance(value,(str,int,float)) for value in row):raise ValueError('备份值类型无效')
            item=dict(zip(columns,row));scope=SCOPES[table]
            if scope not in item:raise ValueError('备份缺少所有权字段')
            if scope in ('user_id','owner_id'):item[scope]=user_id
            if table in ('chat_workspace_state','chat_customer_notes','chat_send_attempts'):item['user_id']=user_id
            if table == 'keywords' and item.get('image_url'):
                from utils.path_security import safe_upload_path
                try:
                    safe_upload_path(str(item['image_url']))
                except ValueError:
                    raise ValueError('备份中的图片附件路径无效') from None
            if table == 'ai_reply_settings' and item.get('base_url'):
                from app.services.ai_client import validate_outbound_url
                try:
                    validate_outbound_url(str(item['base_url']), allow_private=False, require_resolution=False)
                except Exception:
                    raise ValueError('备份中的 AI 地址不允许访问本机或内网') from None
            parsed.append(item)
        tables[table]=(info,columns,parsed)

    owned_cookies={r[0] for r in conn.execute('SELECT id FROM cookies WHERE user_id=?',(user_id,))}
    for item in tables.get('cookies',(None,None,[]))[2]:
        if not item.get('id'):raise ValueError('账号ID不能为空')
        existing=conn.execute('SELECT user_id FROM cookies WHERE id=?',(item['id'],)).fetchone()
        if existing and existing[0]!=user_id:raise ValueError('备份引用了无权访问的账号')
        owned_cookies.add(item['id'])

    def owned_row(table,row):
        scope=SCOPES[table]
        if scope == 'incident_id':
            return row.get(scope) in ids.get('operational_incidents', set())
        return row.get(scope) in owned_cookies if scope in ('cookie_id','account_id') else row.get(scope)==user_id

    ids={}
    for table,scope in SCOPES.items():
        if not schema(conn,table):continue
        cols={c[1] for c in schema(conn,table)}
        if 'id' in cols:
            ids[table]={r[0] for r in conn.execute(f'SELECT id FROM {ident(table)} WHERE {condition(scope)}',(user_id,))}
    for table,(info,columns,rows) in tables.items():
        pk=[r[1] for r in sorted(info,key=lambda r:r[5]) if r[5]]
        for item in rows:
            if not owned_row(table,item):raise ValueError('备份数据不属于当前用户')
            for key in ('cookie_id','account_id'):
                if item.get(key) and item[key] not in owned_cookies:raise ValueError('备份引用了无权访问的账号')
            if pk and all(k in item for k in pk):
                cur=conn.execute(f'SELECT * FROM {ident(table)} WHERE '+ ' AND '.join(f'{ident(k)}=?' for k in pk),tuple(item[k] for k in pk))
                existing=cur.fetchone()
                if existing and not owned_row(table,dict(zip([c[0] for c in cur.description],existing))):
                    raise ValueError('备份记录ID与其他用户冲突')
            if item.get('id') is not None:ids.setdefault(table,set()).add(item['id'])
    for table,(_,_,rows) in tables.items():
        for item in rows:
            for key,parent in RELATIONS.items():
                if item.get(key) is not None and item.get(key) not in ids.get(parent,set()):
                    historical = key=='card_id' and (
                        table=='card_shipments' or
                        (table=='delivery_stock' and item.get('status') in ('delivered','released'))
                    )
                    if historical and not conn.execute('SELECT 1 FROM cards WHERE id=?',(item[key],)).fetchone():
                        continue  # 保留已删除卡券的历史记录；不能借此引用别人的现存卡券。
                    raise ValueError('备份包含无权访问或不存在的关联记录')
            if table=='product_materials' and item.get('rule_id') and item['rule_id'] not in ids.get('product_filter_rules',set()):
                raise ValueError('素材关联规则无效')
    # 全部验证完再开始写；用户数据按主键合并，不清空未出现在备份中的现有数据。
    conn.execute('BEGIN IMMEDIATE')
    try:
        for table in SCOPES:
            if table not in tables:continue
            info,columns,rows=tables[table]
            if table == 'delivery_reconciliation_activation':
                # A restored account must not start shipping old backup orders.
                rows = [dict(row, activated_at=time.time()) for row in rows]
            pk=[r[1] for r in sorted(info,key=lambda r:r[5]) if r[5]]
            updates=[c for c in columns if c not in pk]
            assignments=[]
            for key in updates:
                expression=f'excluded.{ident(key)}'
                if table=='cards' and key=='data_content':
                    expression="CASE WHEN EXISTS (SELECT 1 FROM delivery_stock s WHERE s.card_id=cards.id) THEN cards.data_content ELSE excluded.data_content END"
                if table=='orders' and key=='system_shipped':
                    expression='MAX(COALESCE(orders.system_shipped,0),COALESCE(excluded.system_shipped,0))'
                if table=='orders' and key=='order_status':
                    expression="CASE WHEN orders.order_status IN ('shipped','completed','cancelled','refunded') THEN orders.order_status ELSE excluded.order_status END"
                if table=='orders' and key=='version':
                    expression='MAX(COALESCE(orders.version,0),COALESCE(excluded.version,0))'
                if table=='delivery_attempts' and key=='status':
                    expression="CASE WHEN delivery_attempts.status='completed' THEN 'completed' ELSE excluded.status END"
                if table=='delivery_stock' and key=='status':
                    expression="CASE WHEN delivery_stock.status='delivered' THEN 'delivered' ELSE excluded.status END"
                assignments.append(f'{ident(key)}={expression}')
            conflict=(' ON CONFLICT('+','.join(ident(k) for k in pk)+') DO UPDATE SET '+
                      ','.join(assignments)) if pk and updates else ' ON CONFLICT DO NOTHING'
            if table in ('delivery_attempts','delivery_stock','card_shipments',
                         'buyer_interaction_attempts','buyer_interaction_activation','chat_send_attempts','chat_handovers',
                         'account_auth_clients','delivery_reconciliation_activation','delivery_reconciliation_runs'):
                conflict=' ON CONFLICT DO NOTHING'  # 已有交易证据不被旧备份回写。
            query=f'INSERT INTO {ident(table)} ('+','.join(ident(c) for c in columns)+') VALUES ('+','.join('?' for _ in columns)+')'+conflict
            conn.executemany(query,[tuple(row[c] for c in columns) for row in rows])
        conn.commit()
    except Exception:
        conn.rollback();raise
    return True


def delete_user_data(conn,user_id):
    # 子表先删除，所有子查询仍能看到 cookies；明确覆盖未声明外键的历史表。
    for table,scope in reversed(list(SCOPES.items())):
        if table=='cookies' or not schema(conn,table):continue
        conn.execute(f'DELETE FROM {ident(table)} WHERE {condition(scope)}',(user_id,))
    for table in ('auth_sessions','account_api_keys'):
        if schema(conn,table):conn.execute(f'DELETE FROM {ident(table)} WHERE user_id=?',(user_id,))
    conn.execute('DELETE FROM cookies WHERE user_id=?',(user_id,))
    conn.execute('DELETE FROM users WHERE id=?',(user_id,))


def delete_account_data(conn,cookie_id):
    if schema(conn,'delivery_attempts') and conn.execute(
        "SELECT 1 FROM delivery_attempts WHERE cookie_id=? AND status NOT IN ('completed','released')",(cookie_id,)
    ).fetchone():
        raise ValueError('存在未完成的发货核查，请先处理预留库存')
    if schema(conn,'variant_delivery_bindings'):
        conn.execute('DELETE FROM variant_delivery_bindings WHERE variant_id IN (SELECT id FROM product_variants WHERE cookie_id=?)',(cookie_id,))
    for table in reversed(SCOPES):
        if table=='cookies':continue
        columns={row[1] for row in schema(conn,table)}
        key='cookie_id' if 'cookie_id' in columns else ('account_id' if 'account_id' in columns else None)
        if key:conn.execute(f'DELETE FROM {ident(table)} WHERE {ident(key)}=?',(cookie_id,))
    if schema(conn,'account_api_keys'):conn.execute('DELETE FROM account_api_keys WHERE cookie_id=?',(cookie_id,))
    conn.execute('DELETE FROM cookies WHERE id=?',(cookie_id,))
