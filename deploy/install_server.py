"""Install only the dedicated signer route and service; preserve existing routes."""
from pathlib import Path
import shutil
import subprocess
from datetime import datetime

source=Path('/home/devuser/autosign')
caddy=Path('/etc/caddy/Caddyfile')
original=caddy.read_text()
before='''primeni.pro {
\tencode gzip
\treverse_proxy 127.0.0.1:8097
}'''
after='''primeni.pro {
\tencode gzip
\t@max_signer path /max/webhook
\thandle @max_signer {
\t\treverse_proxy 127.0.0.1:8098
\t}
\thandle {
\t\treverse_proxy 127.0.0.1:8097
\t}
}'''
if '@max_signer path /max/webhook' not in original:
    if original.count(before)!=1: raise SystemExit('Caddy route changed; review required')
    updated=original.replace(before,after,1)
    candidate=source/'deploy/Caddyfile.candidate'
    candidate.write_text(updated)
    subprocess.run(['caddy','validate','--config',str(candidate),'--adapter','caddyfile'],check=True,capture_output=True)
    backup=caddy.with_name('Caddyfile.before-autosign-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(caddy,backup)
    caddy.write_text(updated)
    try:
        subprocess.run(['systemctl','reload','caddy'],check=True)
    except Exception:
        shutil.copy2(backup,caddy)
        subprocess.run(['systemctl','reload','caddy'],check=True)
        raise
shutil.copy2(source/'deploy/primeni-podpis.service','/etc/systemd/system/primeni-podpis.service')
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable','--now','primeni-podpis'],check=True)
print('Dedicated service installed; existing site route preserved')
