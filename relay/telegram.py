"""Durable Telegram outbox. Network calls never hold the trading relay lock."""
from contextlib import closing
import json
import re
import sqlite3
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from relay.telegram_inbox import TelegramInbox
from relay.telegram_admin import TelegramAdmin, parse_admins


def validate_settings(token, channel):
    if not isinstance(token, str) or not re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{20,100}', token):
        raise ValueError('Неверный формат токена Telegram.')
    if not isinstance(channel, str) or not re.fullmatch(r'-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31}', channel):
        raise ValueError('Укажите ID канала или @имя_канала.')


def send_message(token, channel, text):
    request = Request('https://api.telegram.org/bot'+token+'/sendMessage',
                      data=json.dumps({'chat_id':channel, 'text':text}).encode(),
                      headers={'Content-Type':'application/json'}, method='POST')
    try:
        with urlopen(request, timeout=8) as response:
            result = json.load(response)
        if result.get('ok'):
            return 'sent', 'Опубликовано в Telegram', result['result']['message_id']
        return 'error', 'Telegram отклонил сообщение. Проверьте настройки и права бота.', None
    except HTTPError as exc:
        messages = {401:'Неверный токен Telegram.', 403:'Бот не имеет права публикации в канале.',
                    400:'Telegram отклонил сообщение. Проверьте ID канала и права бота.',
                    429:'Превышен лимит Telegram. Сообщение не опубликовано.'}
        return ('error' if exc.code < 500 else 'unknown',
                messages.get(exc.code, 'Результат Telegram неизвестен. Проверьте канал перед повторной отправкой.'), None)
    except Exception:
        # Exceptions may contain the token in the request URL. Never expose them.
        return 'unknown', 'Нет подтверждения Telegram. Проверьте канал перед повторной отправкой.', None


def format_event(event, source=None):
    body = event['event_body']; kind = event['event_type']
    if kind == 'open_order':
        entry = body['entry']
        entry = ' - '.join(map(str, entry)) if isinstance(entry, list) else str(entry)
        return (f"{body['side']}{' LIMIT' if body.get('limit') else ''} {body['symbol']}\n{entry}\n\n" +
                '\n'.join(f'Take profit {i}: {price}' for i, price in enumerate(body['take_profits'], 1)) +
                f"\nстоп {body['stop_loss']}")
    titles = {'modify_all_sl':f"Стоп-лосс всех сигналов: {body.get('new_sl', '')}",'cancel_all_orders':'Отмена ордеров всех сигналов','terminate_all_signals':'Закрытие всех сигналов','cancel_orders':'Отмена ордеров сигнала', 'modify_sl':f"Стоп-лосс: {body.get('new_sl', '')}", 'breakeven_signal':'Стоп по середине сигнала', 'breakeven_tp':'Стоп по середине сигнала',
              'terminate_signal':'Выход из сигнала', 'terminate_tp':'Выход из части сигнала'}
    if kind not in titles:
        return None
    context = f"{source['side']} {source['symbol']}" if source else 'Сигнал'
    if source:
        entry = source['entry']
        context += '\nВход: ' + (' - '.join(map(str, entry)) if isinstance(entry, list) else str(entry))
    if kind in ('breakeven_signal','breakeven_tp') and body.get('signal_midpoint'):
        context+='\nSL: '+body['signal_midpoint']
    leg = f" · TP {body['tp_index']}" if 'tp_index' in body else ''
    return f"{titles[kind]}{leg}\n{context}"


class Telegram:
    def __init__(self, root, dbpath, transport=None, worker=True):
        self.dbpath = dbpath
        self.transport = transport or send_message
        self.worker_enabled = worker
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread = None
        self.guard = threading.Lock()
        with closing(sqlite3.connect(dbpath)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS telegram_settings(id INTEGER PRIMARY KEY CHECK(id=1), token TEXT, channel TEXT, enabled INTEGER)')
            columns={r[1] for r in db.execute('PRAGMA table_info(telegram_settings)')}
            if 'monitor_enabled' not in columns:
                db.execute('ALTER TABLE telegram_settings ADD COLUMN monitor_enabled INTEGER NOT NULL DEFAULT 0')
                db.execute('ALTER TABLE telegram_settings ADD COLUMN monitor_since REAL NOT NULL DEFAULT 0')
            if 'admin_ids' not in columns:
                db.execute("ALTER TABLE telegram_settings ADD COLUMN admin_ids TEXT NOT NULL DEFAULT ''")
            db.execute('CREATE TABLE IF NOT EXISTS telegram_outbox(id TEXT PRIMARY KEY, text TEXT, channel TEXT, state TEXT, detail TEXT, created REAL, message_id INTEGER)')
            if not db.execute('SELECT 1 FROM telegram_settings').fetchone():
                path = root / 'telegram.json'
                settings = json.loads(path.read_text()) if path.exists() else {}
                token, channel = settings.get('token', ''), settings.get('channel', '')
                if token or channel: validate_settings(token, channel)
                db.execute('INSERT INTO telegram_settings(id,token,channel,enabled) VALUES(1,?,?,?)', (token, channel, int(bool(token))))
            db.execute("UPDATE telegram_outbox SET state='unknown',detail='Сервер перезапущен во время отправки. Проверьте канал.' WHERE state='sending'")
        self.admin=TelegramAdmin(dbpath)
        self.inbox=TelegramInbox(dbpath,self.monitor_settings)
        self.inbox.admin=self.admin
        self.start()

    def monitor_settings(self):
        with closing(sqlite3.connect(self.dbpath)) as db:
            return db.execute('SELECT token,channel,monitor_enabled,monitor_since FROM telegram_settings WHERE id=1').fetchone()

    def set_publisher(self, publisher):
        self.inbox.publisher=publisher
        self.start()

    def settings(self):
        with closing(sqlite3.connect(self.dbpath)) as db:
            token, channel, enabled = db.execute('SELECT token,channel,enabled FROM telegram_settings WHERE id=1').fetchone()
        return {'configured':bool(token), 'channel':channel, 'enabled':bool(enabled),
                'admin_ids':', '.join(self.admin.settings()[1]),
                'monitor_enabled':bool(self.monitor_settings()[2])}

    def save(self, body):
        if not isinstance(body, dict) or type(body.get('enabled')) is not bool:
            raise ValueError('Укажите состояние Telegram.')
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            old = db.execute('SELECT token,channel,enabled FROM telegram_settings WHERE id=1').fetchone()
            monitor,since=db.execute('SELECT monitor_enabled,monitor_since FROM telegram_settings WHERE id=1').fetchone()
            enabled=body.get('monitor_enabled',bool(monitor))
            if type(enabled) is not bool:raise ValueError('Укажите состояние мониторинга.')
            token = body.get('token') or old[0]
            old_admins=db.execute('SELECT admin_ids FROM telegram_settings WHERE id=1').fetchone()[0]
            admins=', '.join(parse_admins(body.get('admin_ids',old_admins)))
            channel = body.get('channel', '')
            if token or channel or body['enabled'] or enabled: validate_settings(token, channel)
            if enabled and (not monitor or (token,channel)!=old[:2]):since=time.time()
            if (token, channel, int(body['enabled'])) != old:
                db.execute("UPDATE telegram_outbox SET state='cancelled',detail='Настройки Telegram изменены до отправки.' WHERE state='pending'")
            db.execute('UPDATE telegram_settings SET token=?,channel=?,enabled=? WHERE id=1', (token, channel, int(body['enabled'])))
            db.execute('UPDATE telegram_settings SET monitor_enabled=?,monitor_since=? WHERE id=1',(int(enabled),since))
            db.execute('UPDATE telegram_settings SET admin_ids=? WHERE id=1',(admins,))
            if token != old[0] or admins != old_admins:
                db.execute("UPDATE telegram_admin_jobs SET state='cancelled' WHERE state='pending'")
                db.execute('DELETE FROM telegram_admin_buttons')
        return self.settings()

    def enqueue(self, db, ident, text):
        token, channel, enabled = db.execute('SELECT token,channel,enabled FROM telegram_settings WHERE id=1').fetchone()
        if not text or not enabled or not token:
            return False
        db.execute("INSERT OR IGNORE INTO telegram_outbox VALUES(?,?,?,'pending','Ожидает отправки',?,NULL)", (ident, text, channel, time.time()))
        return True

    def history(self):
        with closing(sqlite3.connect(self.dbpath)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM telegram_outbox ORDER BY created DESC LIMIT 30')]

    def process_one(self):
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id,text,channel,created FROM telegram_outbox WHERE state='pending' ORDER BY created LIMIT 1").fetchone()
            if not row: return False
            ident, text, channel, created = row
            if time.time() - created > 120:
                db.execute("UPDATE telegram_outbox SET state='expired',detail='Срок отправки 120 секунд истёк.' WHERE id=?", (ident,))
                return True
            token, enabled = db.execute('SELECT token,enabled FROM telegram_settings WHERE id=1').fetchone()
            if not enabled:
                db.execute("UPDATE telegram_outbox SET state='cancelled',detail='Telegram выключен.' WHERE id=?", (ident,))
                return True
            db.execute("UPDATE telegram_outbox SET state='sending' WHERE id=?", (ident,))
        try:
            state, detail, message_id = self.transport(token, channel, text)
        except Exception:
            state, detail, message_id = 'unknown', 'Результат отправки неизвестен. Проверьте канал.', None
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute('UPDATE telegram_outbox SET state=?,detail=?,message_id=? WHERE id=?', (state, detail, message_id, ident))
        return True

    def start(self):
        settings=self.settings()
        if not self.worker_enabled or not (settings['enabled'] or settings['monitor_enabled'] or settings['admin_ids']): return
        with self.guard:
            if self.thread is None:
                self.thread = threading.Thread(target=self.run, daemon=True)
                self.thread.start()
        self.wake.set()

    def run(self):
        while not self.stop.is_set():
            try:
                processed = self.process_one()
                processed = self.admin.process_one() or processed
                self.inbox.poll()
            except Exception:
                processed = False
            if processed:
                self.stop.wait(1.1)
            else:
                self.wake.wait(3)
            self.wake.clear()
