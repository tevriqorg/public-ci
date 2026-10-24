#!/usr/bin/env python3
"""Operational supervisor of existing pinned PRECHECK systemd units. Never signs Gate0."""
import datetime, fcntl, hashlib, json, os, pathlib, socket, subprocess, time
ROOT = pathlib.Path(__file__).resolve().parent
UNITS = {
 'aibox-uat-soak126-l1-preflight.service': 300,
 'aibox126-canary-l2-preflight.service': 1800,
 'aibox126-canary-l3-preflight.service': 86400,
 'aibox126-canary-backup-restore.service': 86400,
}
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def save(name, data):
 p=ROOT/(name+'.tmp'); p.write_text(json.dumps(data,sort_keys=True)+'\n'); p.chmod(0o600)
 with p.open('r') as f: os.fsync(f.fileno())
 p.replace(ROOT/name)
def append(data):
 with (ROOT/'results.jsonl').open('a') as f:
  f.write(json.dumps(data,sort_keys=True)+'\n'); f.flush(); os.fsync(f.fileno())
 print(json.dumps(data,sort_keys=True),flush=True)
def notify(message):
 target=os.environ.get('NOTIFY_SOCKET')
 if target:
  if target.startswith('@'): target='\0'+target[1:]
  with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as s: s.sendto(message.encode(),target)
def pulse(current=None):
 save('heartbeat.json',dict(timestamp_utc=now(),pid=os.getpid(),current=current,mode='preparatory',trial_id=None,formal_soak_started=False))
 notify('WATCHDOG=1')
def ctl(*args):
 return subprocess.run(['systemctl','--user',*args],capture_output=True,text=True,timeout=15)
def flush_outbox(force=False):
 pending=state.setdefault('pending_alerts',{})
 for key,entry in list(pending.items()):
  if not force and time.time()-entry.get('last_attempt',0)<60: continue
  entry['last_attempt']=time.time();save('state.json',state)
  unit,status=entry['unit'],entry['status']
  body=f'P2 #126 continuous PRECHECK executor FAILURE: unit={unit}, status={status}, event={key}; hostname=vela; formal soak remains blocked. Evidence: continuous-executor/results.jsonl. No raw data or credentials included.'
  try:
   r=subprocess.run(['gh','api','repos/tevriqorg/P2-AIBOX/issues/126/comments','-f','body='+body,'--jq','.html_url'],capture_output=True,text=True,timeout=25)
   if r.returncode==0:
    state.setdefault('alerts',{})[key]=dict(timestamp_utc=now(),url=r.stdout.strip())
    del pending[key];save('state.json',state)
  except subprocess.TimeoutExpired: pass
  notify('WATCHDOG=1')
def alarm(unit, status):
 active=state.setdefault('active_alerts',{})
 key=active.setdefault(unit,hashlib.sha256((unit+status+now()).encode()).hexdigest()[:16])
 if key not in state.setdefault('alerts',{}):
  state.setdefault('pending_alerts',{}).setdefault(key,dict(unit=unit,status=status,created_utc=now()))
  save('state.json',state)
  flush_outbox(force=True)
def recovered(unit):
 key=state.setdefault('active_alerts',{}).pop(unit,None)
 if key: append(dict(event='unit_recovered',unit=unit,timestamp_utc=now(),alert_event=key,notification_pending=key in state.setdefault('pending_alerts',{}),formal_soak_started=False))
 # Pending events intentionally survive recovery and executor restart.
 save('state.json',state)
def main():
 global state
 os.umask(0o077)
 if socket.gethostname()!='vela': raise SystemExit('wrong host')
 lock=(ROOT/'executor.lock').open('a')
 try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError: raise SystemExit('another executor owns lock')
 state=json.loads((ROOT/'state.json').read_text()) if (ROOT/'state.json').exists() else {'last':{},'alerts':{}}
 append(dict(event='executor_started',timestamp_utc=now(),pid=os.getpid(),mode='preparatory',trial_id=None))
 notify('READY=1')
 while True:
  flush_outbox()
  for unit,interval in UNITS.items():
   pulse(unit)
   if unit == 'aibox126-canary-l3-preflight.service' and (ROOT/'l3-safety-block.json').exists():
    digest=hashlib.sha256((ROOT/'l3-safety-block.json').read_bytes()).hexdigest()
    if state.get('l3_block_digest') != digest:
     append(dict(event='test_safety_blocked',unit=unit,reason='UNREVIEWED_MUTABLE_MIGRATION_TREE',timestamp_utc=now(),block_sha256=digest,formal_soak_started=False))
     state['l3_block_digest']=digest;save('state.json',state)
    continue
   retry_interval = min(interval, 300) if state.get('failed', {}).get(unit) else interval
   if time.time()-state['last'].get(unit,0)<retry_interval: continue
   # Existing systemd serializes duplicate starts; never calls provisioning or a shell.
   before=ctl('show',unit,'-p','ExecMainStartTimestampMonotonic','--value').stdout.strip()
   started=now(); r=ctl('start','--no-block',unit)
   deadline=time.monotonic()+960
   while r.returncode==0:
    pulse(unit)
    q=ctl('show',unit,'-p','ActiveState','-p','SubState','-p','Result','-p','ExecMainStatus')
    props=dict(x.split('=',1) for x in q.stdout.splitlines() if '=' in x)
    if props.get('ActiveState') not in ('activating','active','deactivating'): break
    if time.monotonic()>deadline: break
    time.sleep(2)
   q=ctl('show',unit,'-p','ActiveState','-p','Result','-p','ExecMainStatus','-p','ExecMainStartTimestampMonotonic')
   props=dict(x.split('=',1) for x in q.stdout.splitlines() if '=' in x)
   ok=r.returncode==0 and props.get('Result')=='success' and props.get('ExecMainStatus')=='0' and props.get('ActiveState')=='inactive' and props.get('ExecMainStartTimestampMonotonic') not in (None, '', '0', before)
   event=dict(event='test_completed',unit=unit,started_utc=started,ended_utc=now(),systemd=props,result='PRECHECK_UNIT_SUCCESS' if ok else 'PRECHECK_UNIT_FAILURE',trial_id=None,formal_soak_started=False)
   append(event); state['last'][unit]=time.time(); state.setdefault('failed',{})[unit]=not ok
   if ok: recovered(unit)
   save('state.json',state)
   if not ok: alarm(unit,event['result'])
  pulse(); time.sleep(10)

if __name__ == "__main__":
 main()
