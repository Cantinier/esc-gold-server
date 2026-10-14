from contextlib import closing
"""Flask authenticated broadcast relay; run one worker with multiple threads."""
import hashlib
import hmac
import json
import os
import queue
import secrets
import re
import uuid
import sqlite3
import threading
import time
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from werkzeug.security import generate_password_hash, check_password_hash
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, g
from flask_sock import Sock
from signaldesk.wire import validate_event, CONTROL_TYPES
from signaldesk.core import SignalError
from relay.telegram import Telegram, format_event


def create_app(data_dir=None, telegram_transport=None, telegram_worker=True):
    root = Path(data_dir or os.environ.get('SIGNALDESK_RELAY_DATA', '/data'))
    subscriber = (root / 'subscriber.token').read_text().strip()
    publisher = (root / 'publisher.token').read_text().strip()
    if min(len(subscriber), len(publisher)) < 32 or subscriber == publisher:
        raise RuntimeError('Configure separate publisher and subscriber tokens.')
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=16384, SOCK_SERVER_OPTIONS={'ping_interval':25, 'max_message_size':16384})
    sock = Sock(app)
    peers = {}
    lock = threading.RLock()
    dbpath = root / 'published.sqlite3'
    with closing(sqlite3.connect(dbpath)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS publisher_auth(id INTEGER PRIMARY KEY CHECK(id=1), password_hash TEXT NOT NULL)')
        if not db.execute('SELECT 1 FROM publisher_auth WHERE id=1').fetchone():
            db.execute('INSERT INTO publisher_auth VALUES(1,?)', (generate_password_hash(publisher),))
        db.execute('CREATE TABLE IF NOT EXISTS device_bindings(client_id TEXT PRIMARY KEY,public_key TEXT NOT NULL,bound_at REAL NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,client_id TEXT,kind TEXT,ip TEXT,message TEXT,created REAL,unread INTEGER DEFAULT 1)')
        db.execute('CREATE TABLE IF NOT EXISTS published(id TEXT PRIMARY KEY,digest TEXT,created REAL)')
        db.execute('CREATE TABLE IF NOT EXISTS delivery_results(command_id TEXT,client_id TEXT,report TEXT,PRIMARY KEY(command_id,client_id))')
        db.execute('CREATE TABLE IF NOT EXISTS signals(id TEXT PRIMARY KEY,payload TEXT,recipients TEXT,reports TEXT,created REAL,archived INTEGER DEFAULT 0)')
        db.execute('CREATE TABLE IF NOT EXISTS signal_commands(id TEXT PRIMARY KEY,signal_id TEXT,kind TEXT,payload TEXT,recipients TEXT,results TEXT,created REAL)')
        db.execute('''CREATE TABLE IF NOT EXISTS clients(
            id TEXT PRIMARY KEY, telegram TEXT NOT NULL, trading_account TEXT NOT NULL,
            email TEXT NOT NULL, description TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0, suspended INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL)''')
        db.execute('INSERT OR IGNORE INTO clients(id,telegram,trading_account,email,description,key_hash,created) VALUES(?,?,?,?,?,?,?)',
                   ('legacy', '', '', '', 'Ранее выданный общий ключ', hashlib.sha256(subscriber.encode()).hexdigest(), time.time()))

    telegram = Telegram(root, dbpath, telegram_transport, telegram_worker)
    app.extensions['telegram'] = telegram

    @app.get('/telegram')
    def telegram_status():
        return jsonify(settings=telegram.settings(), messages=telegram.history())

    @app.post('/telegram/settings')
    def telegram_settings():
        try:
            result = telegram.save(request.get_json(silent=True))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        telegram.start()
        return jsonify(result)

    @app.post('/telegram/message')
    def telegram_message():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get('text'), str) or not 1 <= len(body['text'].strip()) <= 4096:
            return jsonify(error='Сообщение должно содержать от 1 до 4096 символов.'), 400
        try:
            ident = str(uuid.UUID(body.get('id', '')))
        except (ValueError, TypeError, AttributeError):
            return jsonify(error='Неверный ID сообщения.'), 400
        ident = 'custom:' + ident
        text = body['text'].strip()
        with lock, closing(sqlite3.connect(dbpath)) as db, db:
            old = db.execute('SELECT text FROM telegram_outbox WHERE id=?', (ident,)).fetchone()
            if old:
                return jsonify(id=ident, duplicate=True), 200 if old[0] == text else 409
            if not telegram.enqueue(db, ident, text):
                return jsonify(error='Сначала настройте и включите Telegram.'), 400
        telegram.start()
        return jsonify(id=ident, queued=True), 202

    def security_notice(ident, kind, message):
        with lock, closing(sqlite3.connect(dbpath)) as db, db:
            ip = request.remote_addr or ''
            recent = db.execute('SELECT id FROM notifications WHERE client_id=? AND kind=? AND ip=? AND created>?',
                                (ident,kind,ip,time.time()-60)).fetchone()
            if recent: return
            db.execute('INSERT INTO notifications(client_id,kind,ip,message,created) VALUES(?,?,?,?,?)',
                       (ident,kind,ip,message,time.time()))

    @app.get('/notifications')
    def notifications():
        with lock, closing(sqlite3.connect(dbpath)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('SELECT n.*,c.description,c.email FROM notifications n LEFT JOIN clients c ON c.id=n.client_id ORDER BY n.id DESC LIMIT 100').fetchall()
            count = db.execute('SELECT COUNT(*) FROM notifications WHERE unread=1').fetchone()[0]
        return jsonify(notifications=[dict(row) for row in rows],unread=count)

    @app.post('/notifications/read')
    def read_notifications():
        body = request.get_json(silent=True)
        ident = body.get('through_id') if isinstance(body,dict) else None
        if type(ident) is not int or ident < 0: return jsonify(error='Неверный номер уведомления.'),400
        with lock, closing(sqlite3.connect(dbpath)) as db, db:
            db.execute('UPDATE notifications SET unread=0 WHERE id<=?', (ident,))
        return jsonify(ok=True)

    def client_for_key(key):
        with closing(sqlite3.connect(dbpath)) as db:
            return db.execute('SELECT id FROM clients WHERE key_hash=? AND revoked=0 AND suspended=0',
                              (hashlib.sha256(key.encode()).hexdigest(),)).fetchone()

    def client_enabled(ident):
        with closing(sqlite3.connect(dbpath)) as db:
            return db.execute('SELECT id FROM clients WHERE id=? AND revoked=0 AND suspended=0', (ident,)).fetchone()


    def publisher_authorized():
        authorization = request.headers.get('Authorization','')
        try:
            if authorization.startswith('Basic '):
                user, secret = base64.b64decode(authorization[6:],validate=True).decode('utf-8').split(':',1)
                if user != 'publisher': return False
            elif authorization.startswith('Bearer '):
                secret = authorization[7:]
            else: return False
        except (ValueError,UnicodeError): return False
        with closing(sqlite3.connect(dbpath)) as db:
            password_hash = db.execute('SELECT password_hash FROM publisher_auth WHERE id=1').fetchone()[0]
        return check_password_hash(password_hash,secret)

    @app.before_request
    def authorize():
        if request.path in ('/', '/health') or request.path.startswith('/static/'):
            return
        authorization = request.headers.get('Authorization', '')
        if request.path == '/ws':
            row = client_for_key(authorization[7:]) if authorization.startswith('Bearer ') else None
            if row:
                g.client_id = row[0]
                return
        elif publisher_authorized():
            return
        return jsonify(error='Unauthorized'), 401

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get('/')
    def index():
        return send_from_directory(app.static_folder, 'index.html')

    @app.post('/publisher/password')
    def change_publisher_password():
        body = request.get_json(silent=True)
        password = body.get('new_password') if isinstance(body,dict) else None
        if not isinstance(password,str) or not 8 <= len(password) <= 256 or not password.strip():
            return jsonify(error='Пароль должен содержать от 8 до 256 символов.'),400
        with lock:
            if not publisher_authorized(): return jsonify(error='Unauthorized'),401
            with closing(sqlite3.connect(dbpath)) as db, db:
                db.execute('UPDATE publisher_auth SET password_hash=? WHERE id=1', (generate_password_hash(password),))
        return jsonify(changed=True)

    @app.get('/status')
    def status():
        with lock:
            return {'connected_clients': len(peers)}

    @app.get('/clients')
    def list_clients():
        with lock, closing(sqlite3.connect(dbpath)) as db:
            db.row_factory = sqlite3.Row
            result = []
            for row in db.execute('SELECT id,telegram,trading_account,email,description,revoked,suspended,created FROM clients ORDER BY created DESC'):
                item = dict(row)
                binding = db.execute('SELECT public_key,bound_at FROM device_bindings WHERE client_id=?',(item['id'],)).fetchone()
                item['device_fingerprint'] = hashlib.sha256(base64.b64decode(binding[0])).hexdigest()[:16] if binding else ''
                item['device_bound_at'] = binding[1] if binding else None
                item['connections'] = sum(p['client_id'] == item['id'] for p in peers.values())
                result.append(item)
            return {'clients': result}

    @app.post('/clients')
    def create_client():
        body = request.get_json()
        if isinstance(body, dict):
            body.setdefault('telegram', '')
        limits = {'id': 80, 'telegram': 80, 'trading_account': 80, 'email': 254, 'description': 1000}
        if not isinstance(body, dict) or set(body) != set(limits):
            return jsonify(error='Заполните поля клиента.'), 400
        for name, limit in limits.items():
            if not isinstance(body[name], str) or len(body[name].strip()) > limit:
                return jsonify(error='Неверное поле: ' + name), 400
            body[name] = body[name].strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', body['id']):
            return jsonify(error='ID: латинские буквы, цифры, дефис или подчёркивание.'), 400
        if not body['trading_account'] or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', body['email']):
            return jsonify(error='Укажите торговый счёт и корректный email.'), 400
        key = secrets.token_urlsafe(40)
        try:
            with lock, closing(sqlite3.connect(dbpath)) as db, db:
                db.execute('INSERT INTO clients(id,telegram,trading_account,email,description,key_hash,created) VALUES(?,?,?,?,?,?,?)',
                           (*[body[k] for k in limits], hashlib.sha256(key.encode()).hexdigest(), time.time()))
        except sqlite3.IntegrityError:
            return jsonify(error='Клиент с таким ID уже существует.'), 409
        return jsonify(client=body, key=key), 201

    @app.post('/clients/<ident>/description')
    def update_description(ident):
        body = request.get_json()
        if not isinstance(body, dict) or set(body) != {'description'} or not isinstance(body['description'], str) or len(body['description'].strip()) > 1000:
            return jsonify(error='Описание должно быть строкой не длиннее 1000 символов.'), 400
        description = body['description'].strip()
        with lock, closing(sqlite3.connect(dbpath)) as db, db:
            updated = db.execute('UPDATE clients SET description=? WHERE id=?', (description, ident))
            if not updated.rowcount:
                return jsonify(error='Клиент не найден.'), 404
        return jsonify(id=ident, description=description)

    @app.post('/clients/<ident>/<action>')
    def control_client(ident, action):
        if action not in ('disconnect', 'revoke', 'enable', 'reissue', 'reset-device'):
            return jsonify(error='Неизвестное действие.'), 404
        closing_peers = []
        new_key = secrets.token_urlsafe(40) if action in ('reissue','reset-device') else None
        with lock, closing(sqlite3.connect(dbpath)) as db, db:
            row = db.execute('SELECT revoked FROM clients WHERE id=?', (ident,)).fetchone()
            if row is None:
                return jsonify(error='Клиент не найден.'), 404
            if action == 'enable' and row[0]:
                return jsonify(error='Отозванный ключ восстановить нельзя.'), 409
            if action in ('reissue','reset-device'):
                if action == 'reset-device':
                    db.execute('DELETE FROM device_bindings WHERE client_id=?',(ident,))
                db.execute('UPDATE clients SET key_hash=?,revoked=0,suspended=0 WHERE id=?', (hashlib.sha256(new_key.encode()).hexdigest(), ident))
            elif action == 'revoke':
                db.execute('UPDATE clients SET revoked=1 WHERE id=?', (ident,))
            else:
                db.execute('UPDATE clients SET suspended=? WHERE id=?', (int(action == 'disconnect'), ident))
            if action != 'enable':
                for connection_id, peer in list(peers.items()):
                    if peer['client_id'] == ident:
                        closing_peers.append(peer)
                        del peers[connection_id]
        for peer in closing_peers:
            try:
                peer['ws'].close(reason=1008, message='Access disabled by administrator')
            except Exception:
                pass
        return {'ok': True, 'key': new_key} if new_key else {'ok': True}

    @app.post('/validate')
    def validate():
        try:
            event, _, _ = validate_event(request.get_json())
            return event
        except SignalError as exc:
            return jsonify(error=str(exc)), 400

    @app.get('/health')
    def health():
        return {'ok': True, 'service':'signaldesk-relay'}

    @app.get('/signals')
    def list_signals():
        with lock, closing(sqlite3.connect(dbpath)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('SELECT * FROM signals WHERE archived=0 ORDER BY created DESC').fetchall()
            rows += db.execute('SELECT * FROM signals WHERE archived=1 ORDER BY created DESC LIMIT 100').fetchall()
            signals = []
            for row in rows:
                item = dict(row)
                for field in ('payload', 'recipients', 'reports'): item[field] = json.loads(item[field])
                commands = []
                for command in db.execute('SELECT * FROM signal_commands WHERE signal_id=? ORDER BY created DESC LIMIT 5', (item['id'],)):
                    c = dict(command)
                    for field in ('payload','recipients','results'): c[field] = json.loads(c[field])
                    commands.append(c)
                item['commands'] = commands
                signals.append(item)
            return {'signals': signals}

    @app.get('/deliveries/<ident>')
    def delivery_status(ident):
        with lock, closing(sqlite3.connect(dbpath)) as db:
            row = db.execute('SELECT recipients FROM signal_commands WHERE id=?', (ident,)).fetchone()
            if row is None:
                row = db.execute('SELECT recipients FROM signals WHERE id=?', (ident,)).fetchone()
            if row is None:
                return jsonify(error='Запись доставки не найдена.'), 404
            results = {client: json.loads(report) for client, report in db.execute(
                'SELECT client_id,report FROM delivery_results WHERE command_id=?', (ident,))}
            return jsonify(recipients=json.loads(row[0]), results=results)

    @app.post('/signals/history/delete')
    def delete_history():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not (set(body) == {'id'} and isinstance(body['id'], str) and body['id'] or body == {'all': True}):
            return jsonify(error='Укажите ID сигнала или all: true.'), 400
        with lock, closing(sqlite3.connect(dbpath)) as db, db:
            if 'id' in body:
                row = db.execute('SELECT archived FROM signals WHERE id=?', (body['id'],)).fetchone()
                if row and not row[0]:
                    return jsonify(error='Можно удалять только сигналы из истории.'), 409
                ids = [body['id']] if row else []
            else:
                ids = [row[0] for row in db.execute('SELECT id FROM signals WHERE archived=1')]
            db.executemany('DELETE FROM signal_commands WHERE signal_id=?', [(ident,) for ident in ids])
            db.executemany('DELETE FROM signals WHERE id=? AND archived=1', [(ident,) for ident in ids])
            # Keep published IDs: deleting history must never allow duplicate trades.
        return jsonify(deleted=len(ids))

    @app.post('/signals')
    def publish():
        try:
            event, _, _ = validate_event(request.get_json())
        except SignalError as exc:
            return jsonify(error=str(exc)), 400
        body = event['event_body']
        ident = body['id']
        kind = event['event_type']
        digest = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
        with lock:
            with closing(sqlite3.connect(dbpath)) as db, db:
                old = db.execute('SELECT digest FROM published WHERE id=?', (ident,)).fetchone()
                if old:
                    return jsonify(id=ident, duplicate=True), 200 if old[0] == digest else 409
                destinations = list(peers.values())
                source = None
                if kind in CONTROL_TYPES:
                    source = db.execute('SELECT payload,recipients FROM signals WHERE id=?', (body['signal_id'],)).fetchone()
                    if source and body.get('tp_index',1) > len(json.loads(source[0])['event_body']['take_profits']):
                        return jsonify(error='Такого тейка нет у сигнала.'), 400
                    original = json.loads(source[1]) if source else []
                if any(p['queue'].full() for p in destinations):
                    return jsonify(error='Subscriber queue full; signal not accepted'), 503
                recipients = sorted({p['client_id'] for p in destinations})
                db.execute('INSERT INTO published VALUES(?,?,?)', (ident,digest,time.time()))
                if kind == 'open_order':
                    db.execute('INSERT INTO signals VALUES(?,?,?,?,?,0)',(ident,json.dumps(event),json.dumps(recipients),'{}',time.time()))
                else:
                    db.execute('INSERT INTO signal_commands VALUES(?,?,?,?,?,?,?)',
                               (ident,body['signal_id'],kind,json.dumps(event),json.dumps(recipients),'{}',time.time()))
                    if kind == 'terminate_signal':
                        db.execute('UPDATE signals SET archived=1 WHERE id=?', (body['signal_id'],))
                telegram.enqueue(db, ident, format_event(event, json.loads(source[0])['event_body'] if source else None))
            event['event_body']['expires_at'] = time.time() + 120
            raw = json.dumps(event)
            for peer in destinations: peer['queue'].put_nowait(raw)
            telegram.start()
            return jsonify(id=ident, queued_clients=len(destinations), executed=False,
                           unavailable_clients=[c for c in original if c not in recipients] if kind in CONTROL_TYPES else []), 202

    def receive_report(client_id, raw):
        try:
            packet = json.loads(raw)
            if packet.get('event_type') != 'signal_status': return
            body = packet['event_body'];ident = body['signal_id']
            if not isinstance(body,dict) or not isinstance(ident,str): return
            clean = {}
            if body.get('state') in ('active','closed'): clean['state'] = body['state']
            if isinstance(body.get('legs'),list) and len(body['legs']) <= 20:
                clean['legs'] = [dict(tp_index=l['tp_index'],state=l['state'],tp=str(l['tp'])[:32],orders=l.get('orders',0),positions=l.get('positions',0)) for l in body['legs'] if isinstance(l,dict) and type(l.get('tp_index')) is int and 1<=l['tp_index']<=20 and l.get('state') in ('pending','position','partial_fill','closed','not_opened','unknown') and isinstance(l.get('tp'),(str,int,float))]
            if body.get('command_state') in ('complete','partial','error','ignored'): clean['command_state'] = body['command_state']
            if isinstance(body.get('message'),str): clean['message'] = body['message'][:3000]
            clean['updated'] = time.time()
            with lock, closing(sqlite3.connect(dbpath)) as db, db:
                command_id = body.get('command_id')
                command = db.execute('SELECT recipients,results FROM signal_commands WHERE id=? AND signal_id=?',(command_id,ident)).fetchone()
                if command and client_id in json.loads(command[0]):
                    db.execute('INSERT OR REPLACE INTO delivery_results VALUES(?,?,?)', (command_id,client_id,json.dumps(clean)))
                    results=json.loads(command[1]);results[client_id]=clean
                    db.execute('UPDATE signal_commands SET results=? WHERE id=?',(json.dumps(results),command_id))
                row = db.execute('SELECT recipients,reports FROM signals WHERE id=?',(ident,)).fetchone()
                if not row or client_id not in json.loads(row[0]): return
                if command_id == ident:
                    db.execute('INSERT OR REPLACE INTO delivery_results VALUES(?,?,?)', (command_id,client_id,json.dumps(clean)))
                recipients = json.loads(row[0]);reports = json.loads(row[1])
                reports[client_id] = dict(reports.get(client_id,{}), **clean)
                archived = all(reports.get(c,{}).get('state') == 'closed' for c in recipients)
                db.execute('UPDATE signals SET reports=?,archived=MAX(archived,?) WHERE id=?',(json.dumps(reports),int(archived),ident))
        except (ValueError,KeyError,TypeError,AttributeError):
            return

    @sock.route('/ws')
    def subscribe(ws):
        connection_id = uuid.uuid4().hex
        peer = {'queue': queue.Queue(maxsize=20), 'ws': ws, 'client_id': g.client_id, 'protocol': 3}
        def deny(kind, message):
            security_notice(g.client_id, kind, message)
            try:
                ws.send(json.dumps({'event_type':'auth_error','event_body':{'code':kind,'message':message}}))
                ws.close(reason=1008,message=kind)
            except Exception: pass
        with lock:
            if any(p['client_id']==g.client_id for p in peers.values()):
                deny('duplicate_session','Клиент уже подключён. Вторая сессия заблокирована.')
                return
        if request.headers.get('X-SignalDesk-Protocol') != '3':
            deny('upgrade_required','Обновите Windows-клиент: требуется подтверждение устройства.')
            return
        nonce = secrets.token_urlsafe(32)
        ws.send(json.dumps({'event_type':'device_challenge','event_body':{'nonce':nonce,'client_id':g.client_id}}))
        try:
            proof = json.loads(ws.receive(timeout=10))
            if proof.get('event_type') != 'device_auth': raise ValueError()
            raw_key = base64.b64decode(proof['event_body']['public_key'],validate=True)
            signature = base64.b64decode(proof['event_body']['signature'],validate=True)
            Ed25519PublicKey.from_public_bytes(raw_key).verify(signature,('SignalDesk-device-v1\n'+g.client_id+'\n'+nonce).encode('utf-8'))
            public_key = base64.b64encode(raw_key).decode('ascii')
        except Exception:
            deny('invalid_device_proof','Не удалось подтвердить криптографический ключ устройства.')
            return
        with lock:
            if not client_for_key(request.headers.get('Authorization', '')[7:]):
                deny('access_revoked','Ключ был отозван или перевыпущен во время подключения.')
                return
            if any(p['client_id']==g.client_id for p in peers.values()):
                deny('duplicate_session','Клиент уже подключён. Вторая сессия заблокирована.')
                return
            if len(peers)>=20:
                deny('capacity','Достигнут лимит подключений сервера.')
                return
            with closing(sqlite3.connect(dbpath)) as db, db:
                binding = db.execute('SELECT public_key FROM device_bindings WHERE client_id=?',(g.client_id,)).fetchone()
                if binding and not hmac.compare_digest(binding[0],public_key):
                    deny('device_mismatch','Попытка входа с другого устройства заблокирована. Возможна передача ключа.')
                    return
                if not binding:
                    db.execute('INSERT INTO device_bindings VALUES(?,?,?)',(g.client_id,public_key,time.time()))
                email = db.execute('SELECT email FROM clients WHERE id=?', (g.client_id,)).fetchone()[0]
            ws.send(json.dumps({'event_type':'auth_success','event_body':{'client_id':g.client_id,'email':email,'protocol_version':3}}))
            peers[connection_id] = peer
        try:
            while ws.connected:
                incoming = ws.receive(timeout=0)
                if incoming is not None: receive_report(g.client_id, incoming)
                try:
                    raw = peer['queue'].get(timeout=0.5)
                except queue.Empty:
                    with lock:
                        if connection_id not in peers:
                            break
                    continue
                with lock:
                    if connection_id not in peers:
                        break
                    ws.send(raw)
        finally:
            with lock:
                peers.pop(connection_id, None)

    return app
