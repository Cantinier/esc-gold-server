from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import uuid
import threading
from werkzeug.serving import make_server
from relay.server import create_app
from relay.telegram_inbox import parse_message
from signaldesk.core import SignalError
from test_relay import connect

TOKEN='123456789:'+'x'*35
TEXT='SELL LIMIT XAUUSD\n4185 - 4186\n\nTake profit 1: 4179.58\nTake profit 2: 4168.65\nстоп 4193.74'


class IncomingTests(unittest.TestCase):
    def test_limit_survives_telegram_and_wire(self):
        from signaldesk.wire import validate_event
        from relay.telegram import format_event
        event=parse_message(TEXT,str(uuid.uuid4()))
        self.assertTrue(event['event_body']['limit'])
        normalized,signal,text=validate_event(event)
        self.assertTrue(signal.limit)
        self.assertIn('SELL LIMIT XAUUSD',format_event(normalized))
        plain=parse_message(TEXT.replace(' LIMIT',''),str(uuid.uuid4()))
        self.assertFalse(validate_event(plain)[1].limit)
        event['event_body']['limit']='true'
        with self.assertRaises(SignalError):validate_event(event)

    def test_completion_phrase_in_text(self):
        ident=str(uuid.uuid4())
        for text in ('Ушли в БУ', 'Итоги: ушли в бу! Всем спасибо.', '✅ УШЛИ  В\nБУ — остатки закрываем.', TEXT+'\nПо итогу ушли в БУ.'):
            self.assertEqual(parse_message(text,ident),{'event_type':'terminate_all_signals','event_body':{'id':ident,'signal_id':ident}})
        for text in ('Поставить безубыток!', 'Ушли в бурю', 'Ушли в прибыль'):
            event=parse_message(text,ident)
            self.assertTrue(event is None or event['event_type']!='terminate_all_signals')

    def test_completion_global_deduplicated_without_echo(self):
        self.poll(self.post(1),self.post(2))
        update=self.post(3,'Завершили торговлю, ушли в БУ. До завтра!',reply=999)
        self.poll(update);self.poll(update)
        item=self.inbox.history()[0]
        self.assertEqual(item['state'],'accepted')
        self.assertEqual(json.loads(item['event'])['event_type'],'terminate_all_signals')
        self.assertEqual(len(self.inbox.history()),3)
        with closing(sqlite3.connect(self.inbox.dbpath)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM signals WHERE archived=0').fetchone()[0],0)
        self.assertEqual(self.telegram.history(),[])

    def test_extra_command_parsing(self):
        ident=str(uuid.uuid4())
        for text,kind in [('Закройте все ордера','cancel_all_orders'),('Закройте все позиции!','terminate_all_signals'),('Передвиньте стоп на 4124','modify_all_sl'),('Стоп на 90','modify_all_sl'),('Стоп на 90,5!','modify_all_sl')]:
            event=parse_message(text,ident)
            self.assertEqual(event['event_type'],kind)
        self.assertEqual(parse_message('Передвиньте стоп на 4124,50',ident)['event_body']['new_sl'],'4124.50')
        self.assertIsNone(parse_message('Передвиньте стоп на -1',ident))
        self.assertIsNone(parse_message('Передвиньте стоп на NaN',ident))
        with self.assertRaises(SignalError):parse_message('Передвиньте стоп на 0',ident)

    def test_global_commands_without_signal_and_stop_reply(self):
        self.poll(self.post(1,'Закройте все ордера'))
        self.assertEqual(self.inbox.history()[0]['state'],'accepted')
        self.poll(self.post(2),self.post(3))
        self.poll(self.post(4,'Передвиньте стоп на 4200',reply=2))
        event=json.loads(self.inbox.history()[0]['event'])
        self.assertEqual(event['event_type'],'modify_all_sl')
        self.assertEqual(event['event_body']['signal_id'],event['event_body']['id'])
        self.assertEqual(self.inbox.history()[0]['state'],'accepted')
        self.assertEqual(self.telegram.history(),[])
        self.poll(self.post(5,'Закройте все позиции'))
        self.assertEqual(self.inbox.history()[0]['state'],'accepted')
        with closing(sqlite3.connect(self.inbox.dbpath)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM signals WHERE archived=0').fetchone()[0],0)

    def test_breakeven_published_with_original_midpoint(self):
        self.poll(self.post(1))
        self.poll(self.post(2,'Поставить безубыток!'))
        with closing(sqlite3.connect(self.inbox.dbpath)) as db:
            row=db.execute('SELECT payload FROM signal_commands').fetchone()
        self.assertEqual(json.loads(row[0])['event_body']['signal_midpoint'],'4185.5')

    def test_global_stop_without_signal_or_valid_reply(self):
        self.poll(self.post(1,'Передвиньте стоп на 4124',reply=999))
        item=self.inbox.history()[0]
        self.assertEqual(item['state'],'accepted')
        event=json.loads(item['event'])
        self.assertEqual(event['event_type'],'modify_all_sl')
        self.assertEqual(event['event_body']['new_sl'],'4124')
        self.assertEqual(event['event_body']['signal_id'],event['event_body']['id'])
        self.assertEqual(self.telegram.history(),[])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        for n,c in [('publisher','p'),('subscriber','s')]: (self.root/(n+'.token')).write_text(c*40)
        self.app=create_app(self.root,telegram_worker=False);self.http=self.app.test_client()
        self.headers={'Authorization':'Bearer '+'p'*40};self.telegram=self.app.extensions['telegram'];self.inbox=self.telegram.inbox
        self.configure()
        self.since=time.time()-2
        with closing(sqlite3.connect(self.inbox.dbpath)) as db,db:db.execute('UPDATE telegram_settings SET monitor_since=?',(self.since,))
    def tearDown(self):self.tmp.cleanup()
    def configure(self,**extra):
        body=dict(enabled=True,monitor_enabled=True,token=TOKEN,channel='-100123');body.update(extra)
        r=self.http.post('/telegram/settings',json=body,headers=self.headers);self.assertEqual(r.status_code,200)
    def post(self,mid,text=TEXT,reply=None,**extra):
        msg=dict(message_id=mid,date=int(time.time()),chat=dict(id=-100123,type='channel'),text=text)
        if reply is not None:msg['reply_to_message']={'message_id':reply}
        msg.update(extra);return {'update_id':mid,'channel_post':msg}
    def poll(self,*updates):
        self.inbox.transport=lambda token,offset:list(updates);self.inbox.poll()
    def signals(self):return self.http.get('/signals',headers=self.headers).json['signals']
    def test_stickers_trigger_global_read_only_check_once_without_echo(self):
        for mid,flags in enumerate(({}, {'is_animated':True}, {'is_video':True}),1):
            update=self.post(mid,'',sticker=dict(file_id='sticker',**flags))
            self.poll(update);self.poll(update)
            item=self.inbox.history()[0]
            self.assertEqual(item['state'],'accepted')
            self.assertEqual(json.loads(item['event'])['event_type'],'check_take_profits')
        self.assertEqual(self.signals(),[])
        self.assertEqual(self.telegram.history(),[])
        with closing(sqlite3.connect(self.inbox.dbpath)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM signal_commands').fetchone()[0],3)
        self.poll(self.post(4,'',sticker={},date=int(time.time()-300)))
        self.assertEqual(self.inbox.history()[0]['state'],'ignored')

    def test_parser_optional_limit_variable_targets_and_entries(self):
        for text in [TEXT,TEXT.replace(' LIMIT',''),TEXT.replace('4185 - 4186','4185'),TEXT.replace('Take profit 2: 4168.65\n','')]:
            self.assertEqual(parse_message(text,str(uuid.uuid4()))['event_type'],'open_order')
        self.assertIsNone(parse_message('Обсудим SELL XAUUSD позже',str(uuid.uuid4())))
        with self.assertRaises(SignalError):parse_message(TEXT+'\nРеклама',str(uuid.uuid4()))
    def test_incoming_no_echo_dedup_and_reply_or_latest(self):
        self.poll(self.post(1));first=self.signals()[0]['id']
        self.poll(self.post(2,TEXT.replace('4185 - 4186','4185')));second=self.signals()[0]['id']
        self.poll(self.post(3,'Закройте первый тейк профит',reply=1),self.post(4,'Поставить безубыток!'))
        signals={s['id']:s for s in self.signals()}
        self.assertEqual(signals[first]['commands'][0]['kind'],'terminate_tp')
        self.assertEqual(signals[second]['commands'][0]['kind'],'breakeven_signal')
        self.assertEqual(self.telegram.history(),[])
        self.poll(self.post(1),self.post(3,'Закройте первый тейк профит',reply=1))
        self.assertEqual(len(self.signals()),2)
        self.assertEqual(len({s['id']:s for s in self.signals()}[first]['commands']),1)
    def test_ignore_other_chat_old_edits_own_posts_and_reject_unknown_reply(self):
        self.poll(self.post(1,chat=dict(id=-100999,type='channel')),self.post(2,date=int(time.time()-300)),
                  {'update_id':3,'edited_channel_post':self.post(3)['channel_post']},self.post(4,'Поставить безубыток!',reply=999))
        self.assertEqual(self.signals(),[])
        event=parse_message(TEXT,str(uuid.uuid4()))
        self.http.post('/signals',json=event,headers=self.headers)
        self.telegram.transport=lambda *a:('sent','ok',10);self.telegram.process_one()
        self.poll(self.post(10))
        self.assertEqual(len(self.signals()),1)
        self.poll(self.post(11,'Закройте второй тейк профит',reply=10))
        self.assertEqual(self.signals()[0]['commands'][0]['kind'],'terminate_tp')
        self.assertEqual(len(self.telegram.history()),1)
    def test_offsets_restart_and_invalid_tp_not_sent(self):
        self.poll(self.post(1))
        restored=create_app(self.root,telegram_worker=False).extensions['telegram'].inbox
        offsets=[];restored.transport=lambda token,offset:offsets.append(offset) or [self.post(1)]
        restored.poll();self.assertEqual(offsets,[2]);self.assertEqual(len(self.signals()),1)
        self.poll(self.post(2,'Закройте третий тейк профит'))
        self.assertEqual(len(self.signals()[0]['commands']),0)
        self.assertEqual(self.inbox.history()[0]['state'],'rejected')
    def test_retry_keeps_target_and_does_not_advance_offset(self):
        self.poll(self.post(1))
        original=self.inbox.publisher
        self.inbox.publisher=lambda *a,**k:({'error':'busy'},503)
        command=self.post(2,'Поставить безубыток!');self.poll(command)
        self.assertEqual(self.inbox.history()[0]['state'],'pending')
        self.inbox.publisher=original;self.poll(command)
        self.assertEqual(self.inbox.history()[0]['state'],'accepted')
        self.assertEqual(len(self.signals()[0]['commands']),1)
    def test_disabled_monitor_and_configuration_change_during_poll(self):
        self.configure(monitor_enabled=False)
        self.poll(self.post(1));self.assertEqual(self.signals(),[])
        self.configure()
        def request(token,offset):
            self.configure(channel='-100999')
            return [self.post(2)]
        self.inbox.transport=request;self.inbox.poll();self.assertEqual(self.signals(),[])
    def test_actual_websocket_receives_channel_event(self):
        server=make_server('127.0.0.1',0,self.app,threaded=True)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            with connect(f'ws://127.0.0.1:{server.server_port}/ws',additional_headers={'Authorization':'Bearer '+'s'*40},proxy=None) as ws:
                ws.recv(timeout=3)
                self.poll(self.post(1))
                event=json.loads(ws.recv(timeout=3))
                self.assertEqual(event['event_type'],'open_order')
                self.poll(self.post(2,'',sticker={'file_id':'test'}))
                event=json.loads(ws.recv(timeout=3))
                self.assertEqual(event['event_type'],'check_take_profits')
                self.assertGreater(event['event_body']['expires_at'],time.time())
                self.assertEqual(self.telegram.history(),[])
        finally:server.shutdown();server.server_close()
