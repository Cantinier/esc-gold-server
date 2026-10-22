from contextlib import closing
import json
import sqlite3
import time
import unittest
import test_telegram
TOKEN = test_telegram.TOKEN


class AdminTests(unittest.TestCase):
    setUp = test_telegram.TelegramTests.setUp
    tearDown = test_telegram.TelegramTests.tearDown
    send = test_telegram.TelegramTests.send
    post = test_telegram.TelegramTests.post
    signal = test_telegram.TelegramTests.signal
    def configure_admins(self, ids='12345, 67890', **extra):
        body=dict(enabled=False,monitor_enabled=False,token=TOKEN,channel='-100123',admin_ids=ids)
        body.update(extra)
        return self.post('/telegram/settings',body)

    def jobs(self):
        with closing(sqlite3.connect(self.telegram.dbpath)) as db:
            return [(r[0],r[1],json.loads(r[2]),r[3]) for r in db.execute('SELECT chat,method,body,state FROM telegram_admin_jobs ORDER BY created')]

    def message(self, uid, chat='12345', text='Активные сигналы', **extra):
        msg=dict(message_id=uid,chat=dict(id=int(chat),type='private'),date=int(time.time()),text=text)
        msg['from']={'id':int(chat)};msg.update(extra)
        return dict(update_id=uid,message=msg)

    def callback(self, uid, data, chat='12345', sender=None):
        return dict(update_id=uid,callback_query={'id':str(uid),'data':data,'from':{'id':int(sender or chat)},
                    'message':{'message_id':100,'chat':{'id':int(chat),'type':'private'}}})

    def poll(self,*updates):
        self.telegram.inbox.transport=lambda *args:list(updates)
        self.telegram.inbox.poll()

    def test_private_admin_sticker_id_only_and_deduplicated(self):
        self.configure_admins()
        for uid,flags in enumerate(({},dict(is_animated=True),dict(is_video=True)),1):
            update=self.message(uid,text='',sticker=dict(file_unique_id='unique_'+str(uid),file_id='download-id',**flags))
            self.poll(update);self.poll(update)
        self.assertEqual(len(self.jobs()),3)
        body=self.jobs()[-1][2]
        self.assertEqual(body['text'],'file_unique_id:\n<code>unique_3</code>')
        self.assertEqual(body['reply_parameters'],{'message_id':3})
        self.poll(self.message(4,chat='99999',sticker={'file_unique_id':'other'}),
                  self.message(5,date=0,sticker={'file_unique_id':'old'}))
        group=self.message(6,sticker={'file_unique_id':'group'})
        group['message']['chat']['type']='group'
        self.poll(group)
        self.assertEqual(len(self.jobs()),3)
        with closing(sqlite3.connect(self.telegram.dbpath)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM signal_commands').fetchone()[0],0)

    def test_admin_id_validation_and_preservation(self):
        self.assertEqual(self.configure_admins('12345, 67890,12345').json['admin_ids'],'12345, 67890')
        for ids in ['-12345','abc','12345,','0','1.2',12345]:
            self.assertEqual(self.configure_admins(ids).status_code,400)
        self.post('/telegram/settings',dict(enabled=False,channel='-100123'))
        self.assertEqual(self.telegram.settings()['admin_ids'],'12345, 67890')

    def test_notifications_fanout_dedup_and_channel_disabled(self):
        self.configure_admins();event=self.signal()
        self.assertEqual(self.post('/signals',event).status_code,202)
        self.post('/signals',event)
        self.assertEqual(len(self.jobs()),2)
        self.assertEqual({r[0] for r in self.jobs()},{'12345','67890'})
        self.assertTrue(all('Получателей: 0' in r[2]['text'] for r in self.jobs()))
        self.assertEqual(self.telegram.history(),[])
        sent=[];self.telegram.admin.transport=lambda *args:sent.append(args) or True
        self.telegram.admin.process_one();self.telegram.admin.process_one()
        self.assertEqual(len(sent),2)

    def test_menu_auth_pagination_deletion_and_no_trade_replay(self):
        self.configure_admins()
        events=[self.signal() for _ in range(4)]
        for event in events:self.post('/signals',event)
        initial=len(self.jobs())
        self.poll(self.message(1,chat='99999'),self.message(2,chat='12345'))
        # Only the configured administrator receives the list.
        self.assertEqual(len(self.jobs()),initial+1)
        page=self.jobs()[-1][2];self.assertIn('Страница 1 из 2',page['text'])
        rows=page['reply_markup']['inline_keyboard']
        self.assertEqual(len([r for r in rows if r[0]['text'].startswith('Удалить')]),3)
        next_data=rows[-2][0]['callback_data']
        self.poll(self.callback(3,next_data))
        page=[j[2] for j in self.jobs() if j[1]=='editMessageText'][-1]
        self.assertIn('Страница 2 из 2',page['text'])
        delete=page['reply_markup']['inline_keyboard'][0][0]['callback_data']
        # A second valid admin cannot use a button issued to the first, nor can a group sender impersonate them.
        self.poll(self.callback(4,delete,chat='67890'),self.callback(5,delete,sender='99999'))
        self.assertEqual(len(self.client.get('/signals',headers=self.headers).json['signals']),4)
        self.poll(self.callback(6,delete))
        self.assertEqual(len(self.client.get('/signals',headers=self.headers).json['signals']),3)
        page=[j[2] for j in self.jobs() if j[1]=='editMessageText'][-1]
        self.assertIn('Страница 1 из 1',page['text'])
        self.poll(self.callback(6,delete),self.callback(7,delete))
        self.assertEqual(len(self.client.get('/signals',headers=self.headers).json['signals']),3)
        # Deleted signal remains deduplicated, and no terminate command was generated.
        self.assertTrue(self.post('/signals',events[0]).json['duplicate'])
        with closing(sqlite3.connect(self.telegram.dbpath)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM signal_commands').fetchone()[0],0)

    def test_web_delete_auth_and_settings_revoke_pending_access(self):
        self.configure_admins();event=self.signal();self.post('/signals',event)
        ident=event['event_body']['id']
        self.assertEqual(self.client.post('/signals/delete',json={'id':ident}).status_code,401)
        self.assertEqual(self.post('/signals/delete',{'id':ident}).json['deleted'],1)
        self.assertEqual(self.post('/signals/delete',{'id':ident}).json['deleted'],0)
        self.poll(self.message(1,text='/start'))
        self.assertEqual(self.jobs()[-1][2]['reply_markup']['keyboard'][0][0]['text'],'Активные сигналы')
        self.configure_admins('67890')
        self.assertTrue(all(j[3]=='cancelled' for j in self.jobs()))
        before=len(self.jobs());self.poll(self.message(2))
        self.assertEqual(len(self.jobs()),before)
