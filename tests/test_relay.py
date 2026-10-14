import copy
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
import httpx
from werkzeug.serving import make_server
from websockets.sync.client import connect as raw_connect
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from device_helpers import device_proof

_test_device_key=Ed25519PrivateKey.generate()
def connect(*args,**kwargs):
    headers=dict(kwargs.get("additional_headers",{}));headers["X-SignalDesk-Protocol"]="3"
    kwargs["additional_headers"]=headers
    kwargs['legacy']=True
    ws=raw_connect(*args,**kwargs)
    challenge=json.loads(ws.recv(timeout=2))
    ws.send(json.dumps(device_proof(_test_device_key,challenge)))
    return ws

from websockets.exceptions import InvalidStatus
from relay.server import create_app
from signaldesk.wire import validate_event, decode_delivery, Inbox
from signaldesk.core import SignalError


def event():
    return {'event_type':'open_order','event_body':{'id':str(uuid.uuid4()),'side':'SELL','symbol':'XAUUSD','entry':[4185,4186],'take_profits':[4179.58,4168.65,4156.53],'stop_loss':4193.74}}


class ProtocolTests(unittest.TestCase):
    def test_single_entry_single_target_silver_buy(self):
        e=event();e['event_body'].update(side='BUY',symbol='XAGUSD',entry=30,take_profits=[31],stop_loss=29)
        _, s, _=validate_event(e)
        self.assertEqual(s.low,s.high)
        self.assertEqual(s.symbol,'XAGUSD')

    def test_reject_unsupported_and_invalid_prices(self):
        for change in ({'symbol':'EURUSD'},{'side':'BYU'},{'stop_loss':float('nan')},{'entry':[1,2,3]},{'take_profits':[]},{'volume':100},{'stop_loss':4100}):
            e=event();e['event_body'].update(change)
            with self.subTest(change=change), self.assertRaises(SignalError): validate_event(e)
        e=event();e['event_type']='close_order'
        with self.assertRaises(SignalError):validate_event(e)

    def test_expiry_and_inbox_survive_restart(self):
        e=event();e['event_body']['expires_at']=time.time()+120
        ident, _, _, _=decode_delivery(json.dumps(e))
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'inbox.db'
            self.assertTrue(Inbox(p).claim(ident))
            self.assertFalse(Inbox(p).claim(ident))
        e['event_body']['expires_at']=time.time()-1
        with self.assertRaises(SignalError):decode_delivery(json.dumps(e))


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        (root/'subscriber.token').write_text('s'*40);(root/'publisher.token').write_text('p'*40)
        self.app=create_app(root)
        self.client=self.app.test_client()
        self.headers={'Authorization':'Bearer '+'p'*40}

    def tearDown(self):self.tmp.cleanup()

    def test_delete_history_preserves_active_and_deduplication(self):
        packets=[event() for _ in range(103)]
        for packet in packets:
            self.client.post('/signals',json=packet,headers=self.headers)
        for packet in packets[:-1]:
            command={'event_type':'terminate_signal','event_body':{'id':str(uuid.uuid4()),'signal_id':packet['event_body']['id']}}
            self.client.post('/signals',json=command,headers=self.headers)
        endpoint='/signals/history/delete'
        self.assertEqual(self.client.post(endpoint,json={'all':True}).status_code,401)
        self.assertEqual(self.client.post(endpoint,json={'all':True},headers={'Authorization':'Bearer '+'s'*40}).status_code,401)
        self.assertEqual(self.client.post(endpoint,json={},headers=self.headers).status_code,400)
        self.assertEqual(self.client.post(endpoint,json={'id':packets[-1]['event_body']['id']},headers=self.headers).status_code,409)
        ident=packets[0]['event_body']['id']
        self.assertEqual(self.client.post(endpoint,json={'id':ident},headers=self.headers).json['deleted'],1)
        self.assertEqual(self.client.post(endpoint,json={'id':ident},headers=self.headers).json['deleted'],0)
        self.assertTrue(self.client.post('/signals',json=packets[0],headers=self.headers).json['duplicate'])
        self.assertEqual(self.client.post(endpoint,json={'all':True},headers=self.headers).json['deleted'],101)
        rows=self.client.get('/signals',headers=self.headers).json['signals']
        self.assertEqual([r['id'] for r in rows],[packets[-1]['event_body']['id']])

    def test_publisher_rotation_persists_and_revokes(self):
        new='n'*64
        self.assertEqual(self.client.post('/publisher/password',json={'new_password':new}).status_code,401)
        self.assertEqual(self.client.post('/publisher/password',json={'new_password':'short'},headers=self.headers).status_code,400)
        self.assertEqual(self.client.post('/publisher/password',json={'new_password':new},headers=self.headers).status_code,200)
        self.assertEqual(self.client.get('/status',headers=self.headers).status_code,401)
        headers={'Authorization':'Bearer '+new}
        self.assertEqual(self.client.get('/status',headers=headers).status_code,200)
        restored=create_app(self.tmp.name).test_client()
        self.assertEqual(restored.get('/status',headers=headers).status_code,200)
        self.assertEqual(restored.get('/status',headers=self.headers).status_code,401)

    def test_auth_and_offline(self):
        self.assertEqual(self.client.post('/signals',json=event()).status_code,401)
        self.assertEqual(self.client.post('/signals',json=event(),headers={'Authorization':'Bearer '+'s'*40}).status_code,401)
        r=self.client.post('/signals',json=event(),headers=self.headers)
        self.assertEqual(r.status_code,202)
        self.assertEqual(r.json['queued_clients'],0)

    def test_sender_page_and_validation_never_publish(self):
        with self.client.get('/') as response: self.assertEqual(response.status_code, 200)
        with self.client.get('/static/app.js') as response: self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/status').status_code, 401)
        self.assertEqual(self.client.post('/validate',json=event()).status_code, 401)
        e=event()
        response=self.client.post('/validate',json=e,headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['event_body']['id'],e['event_body']['id'])
        self.assertEqual(self.client.get('/status',headers=self.headers).json['connected_clients'],0)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(Path(self.tmp.name)/'published.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM published').fetchone()[0],0)

    def new_client(self, ident='client-01'):
        body=dict(id=ident, telegram='@tester', trading_account='123456', email='test@example.com', description='Test client')
        r=self.client.post('/clients',json=body,headers=self.headers)
        self.assertEqual(r.status_code,201,r.data)
        return r.json['key']

    def test_client_registry_auth_and_private_keys(self):
        self.assertEqual(self.client.get('/clients').status_code,401)
        self.assertEqual(self.client.post('/clients',json={}).status_code,401)
        key=self.new_client()
        rows=self.client.get('/clients',headers=self.headers).json['clients']
        self.assertNotIn(key,str(rows));self.assertNotIn('key_hash',str(rows))
        row=next(c for c in rows if c['id']=='client-01')
        self.assertEqual(row['telegram'],'@tester')
        self.assertEqual(row['trading_account'],'123456')
        self.assertEqual(self.client.get('/clients',headers={'Authorization':'Bearer '+key}).status_code,401)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(Path(self.tmp.name)/'published.sqlite3')) as db:
            stored=db.execute('SELECT key_hash FROM clients WHERE id=?',('client-01',)).fetchone()[0]
            self.assertNotEqual(stored,key)

    def test_disconnect_reenable_revoke_and_restart(self):
        key=self.new_client()
        server=make_server('127.0.0.1',0,self.app,threaded=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'ws://127.0.0.1:{server.server_port}/ws'
        auth={'Authorization':'Bearer '+key}
        from websockets.exceptions import ConnectionClosed
        try:
            for action in ('disconnect','revoke'):
                with connect(url,additional_headers=auth,proxy=None) as ws:
                    hello=json.loads(ws.recv(timeout=2))
                    for _ in range(50):
                        rows=self.client.get('/clients',headers=self.headers).json['clients']
                        if next(c for c in rows if c['id']=='client-01')['connections']:break
                        time.sleep(.01)
                    self.assertEqual(next(c for c in rows if c['id']=='client-01')['connections'],1)
                    r=self.client.post('/clients/client-01/'+action,json={},headers=self.headers)
                    self.assertEqual(r.status_code,200)
                    with self.assertRaises(ConnectionClosed):ws.recv(timeout=2)
                with self.assertRaises(InvalidStatus):connect(url,additional_headers=auth,proxy=None)
                r=self.client.post('/clients/client-01/enable',json={},headers=self.headers)
                self.assertEqual(r.status_code,200 if action=='disconnect' else 409)
            # Migrating again must not reactivate a revoked legacy key.
            self.client.post('/clients/legacy/revoke',json={},headers=self.headers)
            restarted=create_app(self.tmp.name).test_client()
            rows=restarted.get('/clients',headers=self.headers).json['clients']
            self.assertTrue(all(c['revoked'] for c in rows))
            self.assertEqual(restarted.get('/ws',headers={'Authorization':'Bearer '+'s'*40}).status_code,401)
        finally:
            server.shutdown();server.server_close();thread.join(2)

    def test_socket_delivery_and_duplicate_no_replay(self):
        server=make_server('127.0.0.1',0,self.app,threaded=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'ws://127.0.0.1:{server.server_port}/ws'
        try:
            with self.assertRaises(InvalidStatus):
                connect(url,additional_headers={'Authorization':'Bearer '+'p'*40},proxy=None)
            with connect(url,additional_headers={'Authorization':'Bearer '+'s'*40},proxy=None) as ws:
                self.assertEqual(json.loads(ws.recv(timeout=2))['event_type'],'auth_success')
                e=event()
                for _ in range(20):
                    response=self.client.post('/signals',json=e,headers=self.headers)
                    if response.status_code != 503:break
                    time.sleep(.01)
                self.assertEqual(response.status_code,202)
                ident, signal, _, _=decode_delivery(ws.recv(timeout=2))
                self.assertEqual(ident,e['event_body']['id']);self.assertEqual(len(signal.targets),3)
                self.assertEqual(self.client.post('/signals',json=e,headers=self.headers).status_code,200)
                changed=copy.deepcopy(e);changed['event_body']['stop_loss']=4194
                self.assertEqual(self.client.post('/signals',json=changed,headers=self.headers).status_code,409)
                with self.assertRaises(TimeoutError):ws.recv(timeout=.1)
        finally:
            server.shutdown();server.server_close();thread.join(2)

    def test_control_targeting_reports_and_history(self):
        from device_helpers import command
        key=self.new_client('first');key2=self.new_client('second')
        server=make_server('127.0.0.1',0,self.app,threaded=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'ws://127.0.0.1:{server.server_port}/ws'
        def socket(key):return connect(url,additional_headers={'Authorization':'Bearer '+key,'X-SignalDesk-Protocol':'2'},proxy=None)
        def rows():return self.client.get('/signals',headers=self.headers).json['signals']
        try:
            with socket(key) as first:
                hello=json.loads(first.recv(timeout=2));self.assertEqual(hello['event_body']['email'],'test@example.com')
                e=event();self.assertEqual(self.client.post('/signals',json=e,headers=self.headers).status_code,202)
                first.recv(timeout=2)
                with socket(key2) as second:
                    second.recv(timeout=2)
                    cmd=command(e['event_body']['id'],'terminate_signal')
                    self.assertEqual(self.client.post('/signals',json=cmd,headers=self.headers).status_code,202)
                    self.assertEqual(json.loads(first.recv(timeout=2))['event_type'],'terminate_signal')
                    self.assertEqual(json.loads(second.recv(timeout=2))['event_type'],'terminate_signal')
                    self.assertEqual(self.client.post('/signals',json=cmd,headers=self.headers).status_code,200)
                    with self.assertRaises(TimeoutError):first.recv(timeout=.1)
                    report=dict(event_type='signal_status',event_body=dict(signal_id=e['event_body']['id'],state='closed',command_id=cmd['event_body']['id'],command_state='complete',legs=[]))
                    second.send(json.dumps(report));time.sleep(.6)
                    self.assertTrue(rows()[0]['archived'])
                    first.send(json.dumps(dict(event_type='signal_status',event_body=dict(signal_id=e['event_body']['id'],state='active',command_id=cmd['event_body']['id'],command_state='error',message='Нет локальной привязки'))))
                    time.sleep(.6)
                    self.assertTrue(rows()[0]['archived'])
                    first.send(json.dumps(report))
                    for _ in range(100):
                        if rows()[0]['commands'][0]['results'].get('first',{}).get('command_state')=='complete':break
                        time.sleep(.02)
                    self.assertTrue(rows()[0]['archived'])
                    self.assertEqual(rows()[0]['commands'][0]['results']['first']['command_state'],'complete')
                    path='/deliveries/'+cmd['event_body']['id']
                    self.assertEqual(self.client.get(path).status_code,401)
                    delivery=self.client.get(path,headers=self.headers).json
                    self.assertEqual(set(delivery['recipients']),{'first','second'})
                    self.assertEqual(set(delivery['results']),{'first','second'})
                    self.assertTrue(all(r['command_state']=='complete' for r in delivery['results'].values()))
            self.assertEqual(self.client.post('/signals',json=command(e['event_body']['id'],'terminate_signal'),headers=self.headers).status_code,202)
            restored=create_app(self.tmp.name).test_client()
            self.assertTrue(restored.get('/signals',headers=self.headers).json['signals'][0]['archived'])
        finally:server.shutdown();server.server_close();thread.join(2)

    def test_optional_telegram_and_edit_description(self):
        for ident,telegram in [('omitted',{}),('empty',{'telegram':''})]:
            response=self.client.post('/clients',headers=self.headers,json=dict(id=ident,trading_account='123',email='test@example.com',description='Before',**telegram))
            self.assertEqual(response.status_code,201)
            self.assertEqual(response.json['client']['telegram'],'')
        path='/clients/omitted/description'
        self.assertEqual(self.client.post(path,json={'description':'After'}).status_code,401)
        self.assertEqual(self.client.post(path,headers=self.headers,json={'description':'After','revoked':1}).status_code,400)
        self.assertEqual(self.client.post(path,headers=self.headers,json={'description':'x'*1001}).status_code,400)
        before=next(c for c in self.client.get('/clients',headers=self.headers).json['clients'] if c['id']=='omitted')
        self.assertEqual(self.client.post(path,headers=self.headers,json={'description':' After '}).status_code,200)
        after=next(c for c in create_app(self.tmp.name).test_client().get('/clients',headers=self.headers).json['clients'] if c['id']=='omitted')
        self.assertEqual(after,{**before,'description':'After'})
        self.assertEqual(self.client.post('/clients/missing/description',headers=self.headers,json={'description':''}).status_code,404)

    def test_zero_recipients_persist_deduplicate_and_never_replay(self):
        from device_helpers import command
        e=event()
        r=self.client.post('/signals',json=e,headers=self.headers)
        self.assertEqual(r.status_code,202);self.assertEqual(r.json['queued_clients'],0)
        for kind,extra in [('terminate_signal',{}),('breakeven_signal',{}),('modify_tp',{'tp_index':1,'new_tp':4170}),('terminate_tp',{'tp_index':1})]:
            cmd=command(e['event_body']['id'],kind,**extra)
            r=self.client.post('/signals',json=cmd,headers=self.headers)
            self.assertEqual(r.status_code,202);self.assertEqual(r.json['queued_clients'],0)
            self.assertTrue(self.client.post('/signals',json=cmd,headers=self.headers).json['duplicate'])
        rows=create_app(self.tmp.name).test_client().get('/signals',headers=self.headers).json['signals']
        self.assertEqual(rows[0]['recipients'],[]);self.assertEqual(len(rows[0]['commands']),4)
        server=make_server('127.0.0.1',0,self.app,threaded=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with connect(f'ws://127.0.0.1:{server.server_port}/ws',additional_headers={'Authorization':'Bearer '+'s'*40},proxy=None) as ws:
                ws.recv(timeout=2)
                self.assertTrue(self.client.post('/signals',json=e,headers=self.headers).json['duplicate'])
                with self.assertRaises(TimeoutError):ws.recv(timeout=.2)
        finally:server.shutdown();server.server_close();thread.join(2)

    def test_reissue_invalidates_old_key_and_live_connection(self):
        from websockets.exceptions import ConnectionClosed
        old=self.new_client()
        path='/clients/client-01/reissue'
        self.assertEqual(self.client.post(path,json={}).status_code,401)
        server=make_server('127.0.0.1',0,self.app,threaded=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'ws://127.0.0.1:{server.server_port}/ws'
        def socket(key):return connect(url,additional_headers={'Authorization':'Bearer '+key},proxy=None)
        try:
            with socket(old) as ws:
                ws.recv(timeout=2)
                r=self.client.post(path,json={},headers=self.headers)
                self.assertEqual(r.status_code,200);new=r.json['key'];self.assertNotEqual(new,old)
                with self.assertRaises(ConnectionClosed):ws.recv(timeout=2)
            with self.assertRaises(InvalidStatus):socket(old)
            with socket(new) as ws:self.assertEqual(json.loads(ws.recv(timeout=2))['event_body']['email'],'test@example.com')
            self.client.post('/clients/client-01/revoke',json={},headers=self.headers)
            newest=self.client.post(path,json={},headers=self.headers).json['key']
            with self.assertRaises(InvalidStatus):socket(new)
            with socket(newest) as ws:ws.recv(timeout=2)
            rows=create_app(self.tmp.name).test_client().get('/clients',headers=self.headers).json['clients']
            row=next(c for c in rows if c['id']=='client-01')
            self.assertFalse(row['revoked']);self.assertFalse(row['suspended'])
            self.assertEqual(row['description'],'Test client');self.assertNotIn(newest,str(rows))
        finally:server.shutdown();server.server_close();thread.join(2)

    def test_terminate_archives_without_receivers_and_history_accepts_controls(self):
        from device_helpers import command
        e=event();self.client.post('/signals',json=e,headers=self.headers)
        signal_id=e['event_body']['id']
        self.assertFalse(self.client.get('/signals',headers=self.headers).json['signals'][0]['archived'])
        response=self.client.post('/signals',json=command(signal_id,'terminate_signal'),headers=self.headers)
        self.assertEqual(response.status_code,202)
        self.assertTrue(self.client.get('/signals',headers=self.headers).json['signals'][0]['archived'])
        for kind,extra in [('modify_tp',dict(tp_index=1,new_tp=4170)),('terminate_tp',dict(tp_index=1)),('breakeven_signal',{}),('terminate_signal',{})]:
            self.assertEqual(self.client.post('/signals',json=command(signal_id,kind,**extra),headers=self.headers).status_code,202)
        self.assertTrue(create_app(self.tmp.name).test_client().get('/signals',headers=self.headers).json['signals'][0]['archived'])
