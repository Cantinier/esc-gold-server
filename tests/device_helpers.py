import base64
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

def device_proof(key, challenge):
    body = challenge['event_body']
    message = ('SignalDesk-device-v1\n'+body['client_id']+'\n'+body['nonce']).encode()
    return {'event_type':'device_auth','event_body':{
        'public_key':base64.b64encode(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode(),
        'signature':base64.b64encode(key.sign(message)).decode()}}

import uuid
def command(ident, kind, **extra):
    return dict(event_type=kind,event_body=dict(id=str(uuid.uuid4()),signal_id=ident,**extra))
