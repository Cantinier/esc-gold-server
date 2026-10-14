import json,tempfile,threading,time,unittest
from pathlib import Path
from werkzeug.serving import make_server
from websockets.sync.client import connect
from websockets.exceptions import InvalidStatus
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from relay.server import create_app
from device_helpers import device_proof

class DeviceSecurityTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  (self.root/'publisher.token').write_text('p'*40);(self.root/'subscriber.token').write_text('s'*40)
  self.app=create_app(self.root);self.http=self.app.test_client();self.admin={'Authorization':'Bearer '+'p'*40}
  self.server=make_server('127.0.0.1',0,self.app,threaded=True)
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
  self.url=f'ws://127.0.0.1:{self.server.server_port}/ws'
  self.a=Ed25519PrivateKey.generate();self.b=Ed25519PrivateKey.generate()
 def tearDown(self):
  self.server.shutdown();self.server.server_close();self.thread.join(2);self.tmp.cleanup()
 def socket(self,token='s'*40,protocol='3'):
  return connect(self.url,additional_headers={'Authorization':'Bearer '+token,'X-SignalDesk-Protocol':protocol},proxy=None)
 def authenticate(self,ws,key,proof=None):
  challenge=json.loads(ws.recv(timeout=2));self.assertEqual(challenge['event_type'],'device_challenge')
  proof=proof or device_proof(key,challenge);ws.send(json.dumps(proof))
  return json.loads(ws.recv(timeout=2)),proof
 def idle(self):
  for _ in range(50):
   if not self.http.get('/status',headers=self.admin).json['connected_clients']:return
   time.sleep(.03)
  self.fail('Session not cleaned up')
 def test_binding_duplicate_mismatch_replay_reset(self):
  with self.socket() as first:
   hello,proof=self.authenticate(first,self.a);self.assertEqual(hello['event_type'],'auth_success')
   with self.socket() as second:
    self.assertEqual(json.loads(second.recv(timeout=2))['event_body']['code'],'duplicate_session')
   self.assertEqual(self.http.get('/status',headers=self.admin).json['connected_clients'],1)
  self.idle()
  with self.socket() as ws:
   hello,_=self.authenticate(ws,self.b);self.assertEqual(hello['event_body']['code'],'device_mismatch')
  with self.socket() as ws:
   hello,_=self.authenticate(ws,self.a,proof);self.assertEqual(hello['event_body']['code'],'invalid_device_proof')
  with self.socket() as ws:
   hello,_=self.authenticate(ws,self.a);self.assertEqual(hello['event_type'],'auth_success')
   self.assertEqual(self.http.post('/clients/legacy/reset-device',json={}).status_code,401)
   new=self.http.post('/clients/legacy/reset-device',json={},headers=self.admin).json['key']
  with self.assertRaises(InvalidStatus):self.socket()
  with self.socket(new) as ws:
   hello,_=self.authenticate(ws,self.b);self.assertEqual(hello['event_type'],'auth_success')
  notices=self.http.get('/notifications',headers=self.admin).json
  self.assertTrue({'duplicate_session','device_mismatch','invalid_device_proof'} <= {n['kind'] for n in notices['notifications']})
  self.assertEqual(self.http.get('/notifications').status_code,401)
  self.http.post('/notifications/read',headers=self.admin,json={'through_id':notices['notifications'][0]['id']})
  self.assertEqual(self.http.get('/notifications',headers=self.admin).json['unread'],0)
  restored=create_app(self.root).test_client()
  self.assertTrue(restored.get('/clients',headers=self.admin).json['clients'][0]['device_fingerprint'])
 def test_old_client_is_rejected(self):
  with self.socket(protocol='2') as ws:self.assertEqual(json.loads(ws.recv(timeout=2))['event_body']['code'],'upgrade_required')

if __name__=='__main__':unittest.main()
