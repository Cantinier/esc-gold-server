import os
import secrets
from pathlib import Path
root = Path(os.environ.get('SIGNALDESK_RELAY_DATA', '/data'))
root.mkdir(parents=True, exist_ok=True)
os.umask(0o077)
for name in ('subscriber.token', 'publisher.token'):
    path = root / name
    if not path.exists():
        path.write_text(secrets.token_urlsafe(40))
        path.chmod(0o600)
print('Relay tokens configured; values are not printed.')
