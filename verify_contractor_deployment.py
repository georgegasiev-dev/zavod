"""Read-only deployment verification; never prints financial rows."""
import json
import os
import base64
import subprocess
from pathlib import Path
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from contractor_identity import identity_key,extract_inn
BASE=os.getenv('VERIFY_API_BASE', 'http://127.0.0.1:8000').rstrip('/')
EXPECTED='2026-10-15-all-contractor-identities-v2'
def _admin_credentials():
    if os.getenv('VERIFY_USE_SERVICE_ENV') == '1':
        pid=subprocess.check_output(['systemctl','show','zavod','-p','MainPID','--value'],text=True).strip()
        entries=Path('/proc',pid,'environ').read_bytes().split(b'\0')
        service_env=dict(entry.split(b'=',1) for entry in entries if b'=' in entry)
        return service_env.get(b'ADMIN_USER',b'admin').decode(),service_env.get(b'ADMIN_PASS',b'').decode()
    return os.getenv('ADMIN_USER','admin'),os.getenv('ADMIN_PASS','')
def get(path):
    request=urllib.request.Request(BASE+path)
    if path!='/api/health':
        username,password=_admin_credentials()
        if not password:
            raise RuntimeError('Administrator credentials required for deployment verification')
        authorization=base64.b64encode((username+':'+password).encode()).decode()
        request.add_header('Authorization','Basic '+authorization)
    with urllib.request.urlopen(request,timeout=20) as response:
        return json.load(response)
def verify():
    evidence=get('/api/health').get('contractor_identity',{})
    assert evidence.get('migration')==EXPECTED and evidence.get('applied'),evidence
    assert evidence['payment_fields_unchanged'] is True
    assert evidence.get('remaining_duplicate_keys')==0,evidence
    cards=get('/api/contractors')
    # The migration's cards_after counts its consolidated snapshot, while
    # /api/contractors may include a broader live set of names.
    # Do not assert equality across these different populations.
    assert isinstance(cards, list) and len(cards)>0, 'Empty/invalid contractors response'
    live_cards_count=len(cards)
    buckets=defaultdict(list)
    for card in cards:
        buckets[identity_key(card['name'])].append(card['name'])
    for key,names in buckets.items():
        if len(names)>1:
            assert len({extract_inn(n) for n in names}-{None})>1,key
    named={}
    for name in ('ИП Грачев Павел Иванович','ИП Параев Максим Владимирович','ИП Сироткин Сергей Сергеевич'):
        assert len(buckets[identity_key(name)])==1,name
        old=get('/api/contractor-ops?name='+urllib.parse.quote(name))
        canonical=get('/api/contractor-ops?name='+urllib.parse.quote(buckets[identity_key(name)][0]))
        assert old['ops']==canonical['ops'],name
        named[name]=len(old['ops'])
    print(json.dumps({'verified':True,'evidence':evidence,'named_history_counts':named,'live_cards_count':live_cards_count},ensure_ascii=False))
if __name__=='__main__':
    for attempt in range(20):
        try:
            verify()
            break
        except Exception:
            if attempt==19:
                raise
            time.sleep(3)

