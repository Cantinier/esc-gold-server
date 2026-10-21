from pathlib import Path
import shutil

root=Path('/backends/proxy')
for name in ('Caddyfile','compose.yaml'):
    backup=root/(name+'.before-mttrader')
    if not backup.exists():shutil.copy2(root/name,backup)
compose=root/'compose.yaml'
text=compose.read_text()
if '4115:4115' not in text:
    text=text.replace('      - "443:443/udp"','      - "443:443/udp"\n      - "4115:4115"\n      - "4116:4116"\n      - "4117:4117"')
compose.write_text(text)
conf=root/'Caddyfile'
text=conf.read_text()
if 'admin.130.49.163.172.sslip.io' not in text:
    text+='''
admin.130.49.163.172.sslip.io:4115 {
    encode zstd gzip
    reverse_proxy mttrader-gateway:8080
}
ws.130.49.163.172.sslip.io:4116 {
    reverse_proxy mttrader-gateway:8081
}
farm.130.49.163.172.sslip.io:4117 {
    encode zstd gzip
    reverse_proxy 172.30.90.1:14117
}
'''
conf.write_text(text)
