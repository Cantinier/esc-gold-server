"""Channel-only Telegram intake with durable offsets and deterministic event IDs."""
from contextlib import closing
import hashlib
import json
import re
import sqlite3
import time
import uuid
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from signaldesk.core import parse_signal, SignalError
from signaldesk.wire import validate_event, number


def get_updates(token, offset):
    request=Request('https://api.telegram.org/bot'+token+'/getUpdates',
                    data=json.dumps({'offset':offset,'limit':100,'timeout':0,
                                     'allowed_updates':['channel_post','message','callback_query']}).encode(),
                    headers={'Content-Type':'application/json'},method='POST')
    try:
        with urlopen(request,timeout=8) as response: data=json.load(response)
        if data.get('ok') and isinstance(data.get('result'),list):return data['result']
        raise ValueError('Telegram не вернул обновления.')
    except HTTPError as exc:
        raise ValueError({409:'Конфликт getUpdates: отключите webhook или другую копию мониторинга этого бота.',
                          401:'Неверный токен Telegram.',429:'Лимит Telegram. Повторим позже.'}.get(exc.code,'Telegram недоступен. Повторим позже.')) from None
    except ValueError:raise
    except Exception:raise ValueError('Нет связи с Telegram. Повторим позже.') from None


ORDINALS=dict(zip(('первый','второй','третий','четвертый','пятый','шестой','седьмой','восьмой','девятый','десятый'),range(1,11)))


def parse_message(text, ident):
    text=text.strip().replace('\u00a0',' ')
    normalized=' '.join(text.lower().replace('ё','е').split()).rstrip('!. ')
    # Completion phrase takes precedence over other text in the message.
    if re.search(r'\bушли в бу\b',normalized):
        return {'event_type':'terminate_all_signals','event_body':{'id':ident,'signal_id':ident}}
    close=re.fullmatch(r'закройте (\d+|'+ '|'.join(ORDINALS)+r') тейк[ -]профит',normalized)
    if close:
        value=close[1];index=int(value) if value.isdigit() else ORDINALS[value]
        if not 1<=index<=20:raise SignalError('Номер тейка должен быть от 1 до 20.')
        return {'event_type':'terminate_tp','event_body':{'id':ident,'tp_index':index}}
    if normalized=='поставить безубыток':
        return {'event_type':'breakeven_signal','event_body':{'id':ident}}
    if normalized=='закройте все ордера':
        return {'event_type':'cancel_all_orders','event_body':{'id':ident,'signal_id':ident}}
    if normalized=='закройте все позиции':
        return {'event_type':'terminate_all_signals','event_body':{'id':ident,'signal_id':ident}}
    stop=re.fullmatch(r'передвиньте стоп на ([0-9]+(?:[.,][0-9]+)?)',normalized)
    if stop:
        return {'event_type':'modify_all_sl','event_body':{'id':ident,'signal_id':ident,'new_sl':number(stop[1].replace(',','.'))}}
    text=re.sub(r'^новый сигнал\s*:\s*','',text,flags=re.I)
    if not re.match(r'^(BUY|SELL)\s+',text,re.I):return None
    signal=parse_signal(text)
    body=dict(id=ident,side=signal.side,symbol=signal.symbol,
              entry=str(signal.low) if signal.low==signal.high else [str(signal.low),str(signal.high)],
              take_profits=list(map(str,signal.targets)),stop_loss=str(signal.stop))
    return validate_event({'event_type':'open_order','event_body':body})[0]


class TelegramInbox:
    def __init__(self, dbpath, settings, transport=None):
        self.dbpath=dbpath;self.settings=settings;self.transport=transport or get_updates
        self.publisher=None;self.admin=None;self.detail='Мониторинг выключен.'
        with closing(sqlite3.connect(dbpath)) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS telegram_offsets(bot TEXT PRIMARY KEY,offset INTEGER)')
            db.execute('''CREATE TABLE IF NOT EXISTS telegram_inbox(
                bot TEXT,update_id INTEGER,chat TEXT,message_id INTEGER,event TEXT,state TEXT,detail TEXT,created REAL,
                PRIMARY KEY(bot,update_id))''')

    def history(self):
        with closing(sqlite3.connect(self.dbpath)) as db:
            db.row_factory=sqlite3.Row
            return [dict(r) for r in db.execute('SELECT update_id,chat,message_id,event,state,detail,created FROM telegram_inbox ORDER BY created DESC,update_id DESC LIMIT 30')]

    def target(self, db, bot, chat, channel, reply_id):
        # The most recent channel signal includes signals published from the UI.
        rows=[]
        for msg,event in db.execute("SELECT message_id,event FROM telegram_inbox WHERE bot=? AND chat=? AND state='accepted'",(bot,chat)):
            packet=json.loads(event) if event else {}
            if packet.get('event_type')=='open_order':rows.append((msg,packet['event_body']['id']))
        rows.extend(db.execute("SELECT o.message_id,s.id FROM telegram_outbox o JOIN signals s ON s.id=o.id WHERE o.channel IN (?,?) AND o.state='sent'",(chat,channel)))
        if reply_id is not None:rows=[r for r in rows if r[0]==reply_id]
        if not rows:raise SignalError('Не найден сигнал для ответа.' if reply_id is not None else 'В этом канале ещё нет принятого сигнала.')
        ident=max(rows)[1]
        source=db.execute('SELECT archived FROM signals WHERE id=?',(ident,)).fetchone()
        if not source or source[0]:raise SignalError('Указанный сигнал уже в истории или удалён; команда не отправлена.')
        return ident

    def process_update(self, update, token, channel, since):
        msg=update.get('channel_post')
        if not isinstance(msg,dict):return True  # Edits never open or modify trades.
        chat_data=msg.get('chat',{});chat=str(chat_data.get('id',''))
        if chat_data.get('type')!='channel':return True
        if channel!=chat and channel.lower()!='@'+str(chat_data.get('username','')).lower():return True
        bot=hashlib.sha256(token.encode()).hexdigest();uid=update['update_id'];mid=msg['message_id']
        ident=str(uuid.uuid5(uuid.NAMESPACE_URL,f'signaldesk:telegram:{bot}:{chat}:{mid}'))
        with closing(sqlite3.connect(self.dbpath)) as db,db:
            old=db.execute('SELECT event,state FROM telegram_inbox WHERE bot=? AND update_id=?',(bot,uid)).fetchone()
            if old and old[1]!='pending':return True
            event=json.loads(old[0]) if old and old[0] else None
            if not old:
                state='pending';detail='Ожидает рассылки'
                text=msg.get('text',msg.get('caption',''))
                try:
                    if type(msg.get('date')) is not int or msg['date']<since or time.time()-msg['date']>120 or msg['date']>time.time()+30:
                        state='ignored';detail='Старое сообщение: исполнение пропущено.'
                    elif db.execute("SELECT 1 FROM telegram_outbox WHERE channel IN (?,?) AND (message_id=? OR (state IN ('pending','sending','unknown') AND text=? AND created>?))",(chat,channel,mid,text,time.time()-180)).fetchone():
                        state='ignored';detail='Собственная публикация бота: повторная рассылка не нужна.'
                    else:
                        event=parse_message(text,ident)
                        if event is None:state='ignored';detail='Текст не соответствует фильтру.'
                        elif event['event_type'] in ('cancel_all_orders','terminate_all_signals','modify_all_sl'):
                            event=validate_event(event)[0]
                        elif event['event_type']!='open_order':
                            reply=msg.get('reply_to_message')
                            reply_id=reply.get('message_id') if isinstance(reply,dict) else None
                            event['event_body']['signal_id']=self.target(db,bot,chat,channel,reply_id)
                            event=validate_event(event)[0]
                except (SignalError,ValueError,TypeError):
                    state='rejected';detail='Неверный сигнал, номер тейка или ссылка на активный сигнал. Команда не отправлена.'
                db.execute('INSERT INTO telegram_inbox VALUES(?,?,?,?,?,?,?,?)',(bot,uid,chat,mid,json.dumps(event) if event else None,state,detail,time.time()))
                if state!='pending':return True
        # Re-check age on retries. Do not execute a stale command after an outage.
        if time.time()-msg['date']>120:
            result,status={'error':'Срок сообщения истёк.'},400
        else:
            result,status=self.publisher(event,from_telegram=True)
        if status>=500:return False
        accepted=status in (200,202)
        detail=('Разослано в WebSocket; получателей: '+str(result.get('queued_clients',0))) if accepted else str(result.get('error','Команда отклонена.'))
        with closing(sqlite3.connect(self.dbpath)) as db,db:
            db.execute('UPDATE telegram_inbox SET state=?,detail=? WHERE bot=? AND update_id=?',('accepted' if accepted else 'rejected',detail,bot,uid))
        return True

    def poll(self):
        token,channel,enabled,since=self.settings()
        admins=self.admin.settings()[1] if self.admin else []
        if not (enabled or admins) or not token or self.publisher is None:
            self.detail='Мониторинг выключен.';return
        bot=hashlib.sha256(token.encode()).hexdigest()
        with closing(sqlite3.connect(self.dbpath)) as db:
            row=db.execute('SELECT offset FROM telegram_offsets WHERE bot=?',(bot,)).fetchone()
        offset=row[0] if row else 0
        try:
            updates=self.transport(token,offset)
            # A token/channel switch while the request was running invalidates its result.
            if self.settings()!=(token,channel,enabled,since):return
            for update in sorted(updates,key=lambda u:u['update_id']):
                if update['update_id']<offset:continue
                if self.settings()!=(token,channel,enabled,since):return
                if self.admin:self.admin.handle(update)
                if enabled and not self.process_update(update,token,channel,since):
                    self.detail='Очередь клиентов занята. Повторим рассылку.';return
                offset=update['update_id']+1
                with closing(sqlite3.connect(self.dbpath)) as db,db:
                    db.execute('INSERT OR REPLACE INTO telegram_offsets VALUES(?,?)',(bot,offset))
            self.detail=('Мониторинг работает.' if enabled else 'Чтение канала выключено. Меню администраторов работает.')+' Проверено: '+time.strftime('%H:%M:%S',time.gmtime())+' UTC'
        except ValueError as exc:self.detail=str(exc)
        except Exception:self.detail='Ошибка мониторинга. Повторим проверку; токен не выводится.'
