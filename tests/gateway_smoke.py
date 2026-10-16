"""Run only against a fresh, isolated Docker Compose test installation."""
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.sync.client import connect
from device_helpers import device_proof
from test_statistics import position

admin='http://127.0.0.1:4113'
client='http://127.0.0.1:4114'
password=subprocess.check_output(['docker','compose','exec','-T','relay','cat','/data/publisher.token'],text=True).strip()
headers={'Authorization':'Bearer '+password}
with httpx.Client(trust_env=False,timeout=10) as http:
    assert http.get(admin+'/').status_code==200
    assert http.get(admin+'/statistics').status_code==200
    assert http.get(admin+'/ws',headers=headers).status_code==404
    assert http.post(admin+'/api/positions/closed').status_code==404
    for path in ('/','/statistics','/signals','/clients','/api/statistics','/static/index.html'):
        assert http.get(client+path,headers=headers).status_code==404,path
    assert http.post(client+'/api/positions/closed').status_code==401
    registered=http.post(admin+'/clients',headers=headers,json=dict(id='gateway-test',telegram='',trading_account='123',email='gateway@example.com',description='Isolated test'))
    registered.raise_for_status()
    token=registered.json()['key'];key=Ed25519PrivateKey.generate()
    with connect('ws://127.0.0.1:4114/ws',additional_headers={'Authorization':'Bearer '+token,'X-SignalDesk-Protocol':'3'},proxy=None) as ws:
        challenge=json.loads(ws.recv(timeout=5))
        ws.send(json.dumps(device_proof(key,challenge)))
        hello=json.loads(ws.recv(timeout=5))
        assert hello['event_type']=='auth_success',hello
        assert hello['event_body']['trading_account']=='123'
        raw=json.dumps(position()).encode();stamp=str(int(time.time()))
        signature=base64.b64encode(key.sign(('SignalDesk-report-v1\n'+stamp+'\n'+hashlib.sha256(raw).hexdigest()).encode())).decode()
        response=http.post(client+'/api/positions/closed',content=raw,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','X-Report-Time':stamp,'X-Device-Signature':signature})
        assert response.status_code==201,response.text
        assert http.get(admin+'/api/statistics',headers=headers).json()['total']==1
print('PASS: admin/WS separation, real WebSocket upgrade, signed REST delivery through client port')
