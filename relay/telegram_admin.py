"""Private administrator menu and durable notifications, separate from channel posts."""
from contextlib import closing
import hashlib
import json
import re
import secrets
import sqlite3
import time
from urllib.request import Request, urlopen


def parse_admins(value):
    if not isinstance(value, str):
        raise ValueError('TG ID администраторов: положительные числа через запятую.')
    parts = [part.strip() for part in value.split(',')] if value.strip() else []
    if len(parts) > 50 or any(not re.fullmatch(r'[1-9][0-9]{0,15}', part) for part in parts):
        raise ValueError('TG ID администраторов: положительные числа через запятую (до 50).')
    return list(dict.fromkeys(parts))


def bot_request(token, method, body):
    request = Request('https://api.telegram.org/bot'+token+'/'+method,
                      data=json.dumps(body).encode(), headers={'Content-Type':'application/json'}, method='POST')
    try:
        with urlopen(request, timeout=8) as response:
            result = json.load(response)
        return bool(result.get('ok'))
    except Exception:
        # URLs in exceptions contain the token. Never log the exception itself.
        return False


class TelegramAdmin:
    def __init__(self, dbpath):
        self.dbpath = dbpath
        self.transport = bot_request
        self.delete_signal = None
        with closing(sqlite3.connect(dbpath)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS telegram_admin_jobs(id TEXT PRIMARY KEY, chat TEXT, method TEXT, body TEXT, state TEXT, created REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS telegram_admin_buttons(key TEXT PRIMARY KEY, chat TEXT, action TEXT, signal_id TEXT, page INTEGER, created REAL)')
            db.execute("UPDATE telegram_admin_jobs SET state='unknown' WHERE state='sending'")

    def settings(self):
        with closing(sqlite3.connect(self.dbpath)) as db:
            token, ids = db.execute('SELECT token,admin_ids FROM telegram_settings WHERE id=1').fetchone()
        return token, parse_admins(ids)

    def enqueue(self, db, key, chat, method, body):
        db.execute("INSERT OR IGNORE INTO telegram_admin_jobs VALUES(?,?,?,?,'pending',?)",
                   (key, chat, method, json.dumps(body), time.time()))

    def notify(self, db, key, text):
        token, ids = db.execute('SELECT token,admin_ids FROM telegram_settings WHERE id=1').fetchone()
        if not token: return
        for chat in parse_admins(ids):
            self.enqueue(db, 'notice:'+key+':'+chat, chat, 'sendMessage',
                         {'chat_id':chat, 'text':text[:4000], 'reply_markup':self.keyboard()})

    @staticmethod
    def keyboard():
        return {'keyboard':[[{'text':'Активные сигналы'}]], 'resize_keyboard':True}

    def page(self, db, chat, page):
        count = db.execute('SELECT COUNT(*) FROM signals WHERE archived=0').fetchone()[0]
        pages = max(1, (count+2)//3)
        page = min(max(0, page), pages-1)
        rows = db.execute('SELECT id,payload,created FROM signals WHERE archived=0 ORDER BY created DESC,id DESC LIMIT 3 OFFSET ?', (page*3,)).fetchall()
        text = [f'Активные сигналы · {count}\nСтраница {page+1} из {pages}']
        buttons = []
        db.execute('DELETE FROM telegram_admin_buttons WHERE created<?', (time.time()-86400,))

        def button(label, action, ident='', target=page):
            key = secrets.token_hex(12)
            db.execute('INSERT INTO telegram_admin_buttons VALUES(?,?,?,?,?,?)', (key,chat,action,ident,target,time.time()))
            return {'text':label, 'callback_data':'sd:'+key}

        for index, (ident, payload, created) in enumerate(rows, page*3+1):
            body = json.loads(payload)['event_body']
            entry = body['entry']; entry = ' — '.join(map(str,entry)) if isinstance(entry,list) else str(entry)
            text.append(f"{index}. {body['side']} {body['symbol']}\nВход: {entry}\nTP: " + ' / '.join(map(str,body['take_profits'])) + f"\nSL: {body['stop_loss']}")
            buttons.append([button(f'Удалить №{index} · {body["side"]} {body["symbol"]}', 'delete', ident)])
        nav = []
        if page: nav.append(button('← Назад', 'page', target=page-1))
        if page+1 < pages: nav.append(button('Далее →', 'page', target=page+1))
        if nav: buttons.append(nav)
        buttons.append([button('Обновить', 'page')])
        if not rows: text.append('Активных сигналов нет.')
        text.append('Удаление убирает запись из списка и не закрывает сделки в MT5.')
        return {'chat_id':chat, 'text':'\n\n'.join(text)[:4000], 'reply_markup':{'inline_keyboard':buttons}}

    def handle(self, update):
        token, admins = self.settings()
        callback = update.get('callback_query')
        message = callback.get('message', {}) if isinstance(callback,dict) else update.get('message', {})
        sender = callback.get('from', {}) if isinstance(callback,dict) else message.get('from', {})
        chat = str(message.get('chat',{}).get('id',''))
        if message.get('chat',{}).get('type') != 'private' or chat not in admins or str(sender.get('id','')) != chat:
            return
        prefix = hashlib.sha256(token.encode()).hexdigest()+':'+str(update['update_id'])
        if callback:
            data = callback.get('data','')
            if not isinstance(data,str) or not data.startswith('sd:'): return
            with closing(sqlite3.connect(self.dbpath)) as db:
                row = db.execute('SELECT action,signal_id,page FROM telegram_admin_buttons WHERE key=? AND chat=? AND created>?', (data[3:],chat,time.time()-86400)).fetchone()
            note = 'Кнопка устарела. Откройте «Активные сигналы» заново.'
            if row:
                action, ident, page = row
                note = 'Обновлено'
                if action == 'delete':
                    deleted = self.delete_signal(ident, active_only=True)
                    note = 'Сигнал удалён. Сделки в MT5 не изменены.' if deleted else 'Сигнал уже удалён или перемещён в историю.'
                with closing(sqlite3.connect(self.dbpath)) as db, db:
                    body = self.page(db,chat,page); body['message_id'] = message['message_id']
                    self.enqueue(db,prefix+':page',chat,'editMessageText',body)
            with closing(sqlite3.connect(self.dbpath)) as db, db:
                self.enqueue(db,prefix+':answer',chat,'answerCallbackQuery',{'callback_query_id':callback['id'],'text':note})
        else:
            text = message.get('text','').strip()
            # Do not process a backlog of private commands when a new admin is configured.
            if time.time()-message.get('date',0) > 120: return
            with closing(sqlite3.connect(self.dbpath)) as db, db:
                if text in ('/start','/menu'):
                    self.enqueue(db,prefix,chat,'sendMessage',{'chat_id':chat,'text':'Меню администратора','reply_markup':self.keyboard()})
                elif text in ('Активные сигналы','/signals'):
                    self.enqueue(db,prefix,chat,'sendMessage',self.page(db,chat,0))

    def process_one(self):
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id,chat,method,body,created FROM telegram_admin_jobs WHERE state='pending' ORDER BY CASE method WHEN 'answerCallbackQuery' THEN 0 WHEN 'editMessageText' THEN 1 ELSE 2 END,created LIMIT 1").fetchone()
            if not row: return False
            ident, chat, method, body, created = row
            token, ids = db.execute('SELECT token,admin_ids FROM telegram_settings WHERE id=1').fetchone()
            if not token or chat not in parse_admins(ids) or time.time()-created > 300:
                db.execute("UPDATE telegram_admin_jobs SET state='cancelled' WHERE id=?", (ident,)); return True
            db.execute("UPDATE telegram_admin_jobs SET state='sending' WHERE id=?", (ident,))
        try: ok = self.transport(token,method,json.loads(body))
        except Exception: ok = False
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute('UPDATE telegram_admin_jobs SET state=? WHERE id=?', ('sent' if ok else 'unknown',ident))
        return True

    def history(self):
        with closing(sqlite3.connect(self.dbpath)) as db:
            rows = db.execute("SELECT id,chat,body,state,created FROM telegram_admin_jobs ORDER BY created DESC LIMIT 30").fetchall()
        return [dict(id=r[0],channel=r[1],text=json.loads(r[2]).get('text',''),state=r[3],created=r[4],
                     detail='ЛС администратора · '+r[3]) for r in rows]
