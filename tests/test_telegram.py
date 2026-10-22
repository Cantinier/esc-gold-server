from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import uuid
from relay.server import create_app
from relay.telegram import format_event


TOKEN = '123456789:' + 'x' * 35


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name, char in [('publisher', 'p'), ('subscriber', 's')]:
            (self.root / (name+'.token')).write_text(char*40)
        self.sent = []
        self.app = create_app(self.root, telegram_transport=self.send, telegram_worker=False)
        self.client = self.app.test_client()
        self.headers = {'Authorization':'Bearer '+'p'*40}
        self.telegram = self.app.extensions['telegram']

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, token, channel, text):
        self.sent.append((token, channel, text))
        return 'sent', 'Опубликовано', len(self.sent)

    def post(self, path, body):
        return self.client.post(path, json=body, headers=self.headers)

    def configure(self):
        return self.post('/telegram/settings', {'enabled':True, 'token':TOKEN, 'channel':'-1001234567890'})

    def signal(self):
        return {'event_type':'open_order', 'event_body':{'id':str(uuid.uuid4()), 'side':'SELL', 'symbol':'XAUUSD',
                'entry':[4185,4186], 'take_profits':[4179.58,4168.65,4156.53], 'stop_loss':4193.74}}

    def test_settings_authentication_redaction_and_preservation(self):
        self.assertEqual(self.client.get('/telegram').status_code,401)
        self.assertEqual(self.client.post('/telegram/settings',json={}).status_code,401)
        self.assertEqual(self.client.post('/telegram/message',json={}).status_code,401)
        self.assertEqual(self.configure().status_code,200)
        response=self.client.get('/telegram',headers=self.headers)
        self.assertNotIn(TOKEN,response.text)
        self.assertTrue(response.json['settings']['configured'])
        self.assertEqual(self.post('/telegram/settings',{'enabled':True,'token':'','channel':'@other_channel'}).status_code,200)
        self.post('/telegram/message',{'id':str(uuid.uuid4()),'text':'<b>Текст</b>\nВторая строка'})
        self.telegram.process_one()
        self.assertEqual(self.sent[0],(TOKEN,'@other_channel','<b>Текст</b>\nВторая строка'))

    def test_signal_controls_and_duplicate_with_zero_clients(self):
        self.configure(); event=self.signal()
        self.assertEqual(self.post('/signals',event).status_code,202)
        self.assertEqual(self.post('/signals',event).status_code,200)
        self.assertEqual(len(self.telegram.history()),1)
        self.telegram.process_one()
        self.assertEqual(self.sent[0][2], 'SELL XAUUSD\n4185 - 4186\n\nTake profit 1: 4179.58\nTake profit 2: 4168.65\nTake profit 3: 4156.53\nстоп 4193.74')
        for kind, title in [('breakeven_signal','Стоп по середине сигнала'),('terminate_signal','Выход из сигнала')]:
            command={'event_type':kind,'event_body':{'id':str(uuid.uuid4()),'signal_id':event['event_body']['id']}}
            self.assertEqual(self.post('/signals',command).status_code,202)
            self.telegram.process_one()
            self.assertIn(title,self.sent[-1][2]); self.assertIn('SELL XAUUSD',self.sent[-1][2])

    def test_failures_expiration_and_custom_deduplication(self):
        self.configure(); body={'id':str(uuid.uuid4()),'text':'Тест'}
        self.assertEqual(self.post('/telegram/message',body).status_code,202)
        self.assertEqual(self.post('/telegram/message',body).status_code,200)
        self.assertEqual(self.post('/telegram/message',dict(body,text='Другой')).status_code,409)
        def fail(*args): raise RuntimeError(TOKEN)
        self.telegram.transport=fail
        self.telegram.process_one()
        self.assertEqual(self.telegram.history()[0]['state'],'unknown')
        self.assertNotIn(TOKEN,json.dumps(self.telegram.history()))
        self.assertFalse(self.telegram.process_one())
        self.assertEqual(self.post('/signals',self.signal()).status_code,202)
        with closing(sqlite3.connect(self.telegram.dbpath)) as db, db:
            db.execute('UPDATE telegram_outbox SET created=?',(time.time()-130,))
        self.telegram.process_one()
        self.assertIn('expired',[row['state'] for row in self.telegram.history()])

    def test_bootstrap_settings_cancel_pending_and_restart(self):
        (self.root/'telegram.json').write_text(json.dumps({'token':TOKEN,'channel':'-1001234567890'}))
        # Bootstrap must not override settings already saved in the admin UI.
        self.configure()
        self.post('/telegram/message',{'id':str(uuid.uuid4()),'text':'Проверка'})
        self.post('/telegram/settings',{'enabled':False,'channel':'-1001234567890'})
        app=create_app(self.root,telegram_worker=False)
        self.assertFalse(app.extensions['telegram'].settings()['enabled'])
        self.assertEqual(app.extensions['telegram'].history()[0]['state'],'cancelled')

    def test_invalid_settings_and_text(self):
        self.assertEqual(self.post('/telegram/settings',{'enabled':True,'token':'bad','channel':'bad'}).status_code,400)
        for value in ['', ' ' , 'a'*4097]:
            self.assertEqual(self.post('/telegram/message',{'id':str(uuid.uuid4()),'text':value}).status_code,400)
        self.assertEqual(self.post('/telegram/message',{'id':str(uuid.uuid4()),'text':'valid'}).status_code,400)
        self.assertIsNone(format_event({'event_type':'modify_tp','event_body':{}}))

    def test_bootstrap_import_and_interrupted_send_never_replays(self):
        with closing(sqlite3.connect(self.telegram.dbpath)) as db, db:
            db.execute('DELETE FROM telegram_settings')
        (self.root/'telegram.json').write_text(json.dumps({'token':TOKEN,'channel':'-1001234567890'}))
        app=create_app(self.root,telegram_worker=False)
        worker=app.extensions['telegram']
        self.assertTrue(worker.settings()['enabled'])
        self.post('/telegram/message',{'id':str(uuid.uuid4()),'text':'Текст'})
        with closing(sqlite3.connect(self.telegram.dbpath)) as db, db:
            db.execute("UPDATE telegram_outbox SET state='sending'")
        restored=create_app(self.root,telegram_worker=False).extensions['telegram']
        self.assertEqual(restored.history()[0]['state'],'unknown')
        self.assertFalse(restored.process_one())
