import base64
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from relay.server import create_app


def position():
    return dict(account='123',broker='Test',trade_mode=0,position_id='77',symbol='XAUUSD',side='SELL',volume=.03,
                entry_price=4185,close_price=4179,sl=4193,tp=4179,profit=18,commission=-1,swap=-.5,fee=0,
                net_profit=16.5,currency='USD',opened_at=1704060000,closed_at=1704063600,reason=5,magic=26101201,deal_ids=['10','11'])


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        for name,c in [('publisher','p'),('subscriber','s')]: (self.root/(name+'.token')).write_text(c*40)
        self.app=create_app(self.root,telegram_worker=False);self.http=self.app.test_client()
        self.admin={'Authorization':'Bearer '+'p'*40}
        client=self.http.post('/clients',headers=self.admin,json=dict(id='stats',telegram='',trading_account='123',email='test@example.com',description='Stats')).json
        self.token=client['key'];self.key=Ed25519PrivateKey.generate()
        with closing(sqlite3.connect(self.root/'published.sqlite3')) as db, db:
            db.execute('INSERT INTO device_bindings VALUES(?,?,?)',('stats',base64.b64encode(self.key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode(),time.time()))
    def tearDown(self): self.tmp.cleanup()
    def send(self,body=None,key=None,stamp=None):
        raw=json.dumps(body or position()).encode();stamp=stamp or str(int(time.time()))
        proof=('SignalDesk-report-v1\n'+stamp+'\n'+hashlib.sha256(raw).hexdigest()).encode()
        signature=base64.b64encode((key or self.key).sign(proof)).decode()
        return self.http.post('/api/positions/closed',data=raw,content_type='application/json',headers={'Authorization':'Bearer '+self.token,'X-Report-Time':stamp,'X-Device-Signature':signature})
    def test_auth_device_validation_and_revocation(self):
        self.assertEqual(self.http.post('/api/positions/closed',json=position()).status_code,401)
        self.assertEqual(self.send(key=Ed25519PrivateKey.generate()).status_code,403)
        self.assertEqual(self.send(stamp='1000').status_code,403)
        self.assertEqual(self.send(dict(position(),account='999')).status_code,400)
        self.assertEqual(self.send(dict(position(),net_profit=float('nan'))).status_code,400)
        self.assertEqual(self.send(dict(position(),email='spoof@example.com')).status_code,400)
        self.http.post('/clients/stats/revoke',headers=self.admin,json={})
        self.assertEqual(self.send().status_code,401)
    def test_dedup_search_date_boundaries_and_totals(self):
        self.assertEqual(self.send().status_code,201)
        self.assertTrue(self.send().json['duplicate'])
        self.assertEqual(self.send(dict(position(),tp=4170)).status_code,409)
        self.assertEqual(self.http.get('/api/statistics').status_code,401)
        data=self.http.get('/api/statistics?search=TEST@EXAMPLE&from=2024-01-01&to=2024-01-01',headers=self.admin).json
        self.assertEqual(data['total'],1);self.assertEqual(data['rows'][0]['email'],'test@example.com')
        self.assertEqual(data['totals'][0]['net_profit'],16.5)
        self.assertEqual(self.http.get('/api/statistics?search=123',headers=self.admin).json['total'],1)
        self.assertEqual(self.http.get('/api/statistics?to=2023-12-31',headers=self.admin).json['total'],0)
        self.assertEqual(self.http.get('/api/statistics?search=%25',headers=self.admin).json['total'],0)
        self.assertEqual(self.http.get('/api/statistics?from=bad',headers=self.admin).status_code,400)
        self.assertEqual(self.http.get('/api/statistics?from=2024-02-01&to=2024-01-01',headers=self.admin).status_code,400)
        restored=create_app(self.root,telegram_worker=False).test_client()
        self.assertEqual(restored.get('/api/statistics',headers=self.admin).json['total'],1)
        with self.http.get('/statistics') as r: self.assertEqual(r.status_code,200)
    def test_pagination_and_currency_separation(self):
        for i in range(101):
            self.assertEqual(self.send(dict(position(),position_id=str(i+1),currency='EUR' if i==100 else 'USD')).status_code,201)
        data=self.http.get('/api/statistics?page=2',headers=self.admin).json
        self.assertEqual(data['total'],101);self.assertEqual(len(data['rows']),1)
        self.assertEqual({x['currency'] for x in data['totals']},{'USD','EUR'})
