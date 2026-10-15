"""Closed positions reported by authenticated, device-bound clients."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import sqlite3
import time
from flask import request, jsonify, g, send_from_directory


FIELDS = {'account','broker','trade_mode','position_id','symbol','side','volume','entry_price','close_price',
          'sl','tp','profit','commission','swap','fee','net_profit','currency','opened_at','closed_at','reason','magic','deal_ids'}


def validate(body):
    if not isinstance(body,dict) or set(body) != FIELDS: raise ValueError('Неверные поля отчёта.')
    for key,limit in [('account',32),('broker',120),('position_id',32),('symbol',64),('currency',16)]:
        if not isinstance(body[key],str) or not 1 <= len(body[key]) <= limit: raise ValueError('Неверное поле '+key)
    if not body['account'].isdigit() or not body['position_id'].isdigit(): raise ValueError('Неверный счёт или позиция.')
    if body['side'] not in ('BUY','SELL'): raise ValueError('Неверное направление.')
    for key in ('trade_mode','reason','magic'):
        if type(body[key]) is not int or not 0 <= body[key] <= 2**63-1: raise ValueError('Неверное поле '+key)
    if body['trade_mode'] not in (0,1,2): raise ValueError('Неверный тип счёта.')
    for key in ('volume','entry_price','close_price','sl','tp','profit','commission','swap','fee','net_profit','opened_at','closed_at'):
        value=body[key]
        if key in ('sl','tp') and value is None: continue
        if type(value) not in (float,int) or not math.isfinite(value) or abs(value)>1e14: raise ValueError('Неверное число '+key)
        if key in ('volume','entry_price','close_price','opened_at','closed_at') and value<=0: raise ValueError('Неверное число '+key)
        if key in ('sl','tp') and value<0: raise ValueError('Неверное число '+key)
    if body['closed_at']<body['opened_at'] or body['closed_at']>time.time()+86400: raise ValueError('Неверное время закрытия.')
    if abs(body['net_profit']-sum(body[k] for k in ('profit','commission','swap','fee')))>0.0001: raise ValueError('Итоговый PnL не совпадает с суммой составляющих.')
    deals=body['deal_ids']
    if not isinstance(deals,list) or not 1<=len(deals)<=500 or any(not isinstance(x,str) or not x.isdigit() or len(x)>32 for x in deals): raise ValueError('Неверные номера сделок.')
    if len(set(deals))!=len(deals): raise ValueError('Повторяющиеся сделки.')
    return body


def register_statistics(app, dbpath):
    with closing(sqlite3.connect(dbpath)) as db, db:
        db.execute('''CREATE TABLE IF NOT EXISTS closed_positions(
            client_id TEXT, email TEXT, account TEXT, broker TEXT, trade_mode INTEGER, position_id TEXT,
            closed_at REAL, currency TEXT, net_profit REAL, payload TEXT, digest TEXT, received_at REAL,
            PRIMARY KEY(client_id,account,broker,trade_mode,position_id))''')
        db.execute('CREATE INDEX IF NOT EXISTS closed_positions_date ON closed_positions(closed_at)')
        db.execute('CREATE INDEX IF NOT EXISTS closed_positions_account ON closed_positions(account)')

    @app.get('/statistics')
    def statistics_page():
        return send_from_directory(app.static_folder,'statistics.html')

    @app.post('/api/positions/closed')
    def receive_position():
        try: body=validate(request.get_json(silent=True))
        except ValueError as exc: return jsonify(error=str(exc)),400
        payload=json.dumps(body,sort_keys=True,ensure_ascii=False,allow_nan=False)
        digest=hashlib.sha256(payload.encode()).hexdigest()
        ident=(g.client_id,body['account'],body['broker'],body['trade_mode'],body['position_id'])
        with closing(sqlite3.connect(dbpath)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            client=db.execute('SELECT email,trading_account,revoked,suspended FROM clients WHERE id=?',(g.client_id,)).fetchone()
            if not client or client[2] or client[3]: return jsonify(error='Client disabled'),403
            if body['account']!=client[1]: return jsonify(error='Номер счёта не соответствует карточке клиента.'),400
            old=db.execute('SELECT digest FROM closed_positions WHERE client_id=? AND account=? AND broker=? AND trade_mode=? AND position_id=?',ident).fetchone()
            if old:
                if old[0]!=digest: return jsonify(error='Позиция уже получена с другими данными.'),409
                return jsonify(accepted=True,duplicate=True)
            db.execute('INSERT INTO closed_positions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (g.client_id,client[0],body['account'],body['broker'],body['trade_mode'],body['position_id'],body['closed_at'],body['currency'],body['net_profit'],payload,digest,time.time()))
        return jsonify(accepted=True,duplicate=False),201

    @app.get('/api/statistics')
    def statistics_data():
        clauses=[];args=[]
        search=request.args.get('search','').strip()
        if len(search)>254: return jsonify(error='Слишком длинный поиск.'),400
        if search:
            clauses.append('(instr(lower(COALESCE(c.email,p.email)),lower(?))>0 OR instr(p.account,?)>0)');args.extend([search,search])
        try:
            start=request.args.get('from','');end=request.args.get('to','')
            tz=timezone(timedelta(hours=3))
            a=datetime.strptime(start,'%Y-%m-%d').replace(tzinfo=tz) if start else None
            b=datetime.strptime(end,'%Y-%m-%d').replace(tzinfo=tz)+timedelta(days=1) if end else None
            if a and b and a>=b: raise ValueError()
            if a: clauses.append('p.closed_at>=?');args.append(a.timestamp())
            if b: clauses.append('p.closed_at<?');args.append(b.timestamp())
            page=int(request.args.get('page','1'))
            if not 1<=page<=1000000: raise ValueError()
        except ValueError: return jsonify(error='Проверьте даты и номер страницы.'),400
        where=' WHERE '+' AND '.join(clauses) if clauses else ''
        source=' FROM closed_positions p LEFT JOIN clients c ON c.id=p.client_id'+where
        with closing(sqlite3.connect(dbpath)) as db:
            count=db.execute('SELECT COUNT(*)'+source,args).fetchone()[0]
            totals=[{'currency':r[0],'positions':r[1],'net_profit':r[2]} for r in db.execute('SELECT p.currency,COUNT(*),SUM(p.net_profit)'+source+' GROUP BY p.currency',args)]
            rows=db.execute('SELECT p.payload,p.client_id,COALESCE(c.email,p.email),p.received_at'+source+' ORDER BY p.closed_at DESC,p.rowid DESC LIMIT 100 OFFSET ?',args+[(page-1)*100]).fetchall()
        return jsonify(rows=[dict(json.loads(r[0]),client_id=r[1],email=r[2],received_at=r[3]) for r in rows],total=count,page=page,page_size=100,totals=totals)
