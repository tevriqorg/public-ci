"""#126 real public-file Transit with a canary-only API outage and recovery."""
import argparse,datetime,hashlib,json,os,shlex,subprocess,sys,time,uuid
from pathlib import Path
from urllib.request import Request,urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from implementation.ingress.store import IngressStore
from implementation.ingress.live_handoff import WeKnoraHandoff,HandoffError

def run(root,inject):
    state=json.loads((root/'canary-state.json').read_text());assert state['clone_db'].startswith('aibox126_canary') and state['clone_db']!=state['original_db']
    oracle=json.loads((root/'at-rest-oracle-v1.json').read_text());kb=oracle['kb_id'];uuid.UUID(kb)
    source=Path('/home/ubuntu/runtime/aibox-0.8-uat/real-corpus/rfc9110.txt');original=source.read_bytes();digest=hashlib.sha256(original).hexdigest();assert digest==oracle['source_sha256']
    store=IngressStore(root/'storage/10000/aibox126/transit')
    session=store.create('public-rfc','p2-126:rfc9110-transit','https://www.rfc-editor.org/rfc/rfc9110.txt',mime='text/plain',analysis_profile='frozen-rfc-window',processor_version='rfc-text-window-v1')
    ident=session['ingest_id']
    if session['state'] in ('discovered','acquiring'):session=store.stage_file(ident,source)
    if session['state'] in ('staging','recovering'):session=store.recover_staged(ident)
    if session['state']=='staged':
        store.begin_analysis(ident)
        manifest=root/'transit-analysis.json'
        payload={'source_sha256':digest,'oracle_sha256':hashlib.sha256((root/'at-rest-oracle-v1.json').read_bytes()).hexdigest(),'markdown':original.decode()[oracle['start']:oracle['end']],'analysis_profile':'deterministic public text window'}
        fd=os.open(manifest,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as f:json.dump(payload,f);f.flush();os.fsync(f.fileno())
        store.mark_analyzed(ident,str(manifest));session=store.get(ident)
    if session['state'] in ('analyzed','committing'):session=store.commit(ident,'rfc9110.txt')
    final=store.final_path(ident);assert hashlib.sha256(final.read_bytes()).hexdigest()==digest
    if session['state']=='committed':store.mark_handoff_pending(ident)
    key=(root/'ingest-key.token').read_text().strip();client=WeKnoraHandoff(api_url='http://127.0.0.1:18082/api/v1',api_key=key,kb_id=kb,timeout=4)
    desc={'resource_path':'local://10000/aibox126/transit/final/rfc9110.txt','size':len(original),'sha256':digest,'filename':'rfc9110.txt','mime':'text/plain'}
    outage=False
    if inject:
        assert store.get(ident)['state']=='handoff_pending','fault only permitted before first completion'
        subprocess.run(['systemctl','--user','stop','aibox126-canary-api'],check=True)
        try:
            try:client.register_external(**desc)
            except HandoffError:outage=True
            assert outage,'real API outage was not detected'
            assert store.get(ident)['state']=='handoff_pending' and final.is_file()
            assert hashlib.sha256(final.read_bytes()).hexdigest()==digest
            p=root/'transit-outage-proof.json';p.write_text(json.dumps({'result':'EXPECTED_CANARY_OUTAGE_DETECTED','state':'handoff_pending','final_hash_verified':True,'staging_retained':bool(store.get(ident)['staging_ref']),'original_uat_stopped':False},indent=2));p.chmod(0o600)
        finally:subprocess.run(['systemctl','--user','start','aibox126-canary-api'],check=True)
        deadline=time.monotonic()+40
        while True:
            try:
                with urlopen('http://127.0.0.1:18082/health',timeout=3) as z:assert z.status==200
                break
            except Exception:
                assert time.monotonic()<deadline,'canary API failed to recover';time.sleep(1)
        store=IngressStore(root/'storage/10000/aibox126/transit')
        assert store.get(ident)['state']=='handoff_pending' and store.final_path(ident)==final
    revision='sha256:'+digest;obj='rfc9110-transit-excerpt';version='rfc-text-window-v1'
    env={'source_key':'p2-126:rfc9110-transit','source_revision_id':revision,'derived_object_id':obj,'title':'RFC9110 Transit GET canary','markdown':json.loads((root/'transit-analysis.json').read_text())['markdown'],'processor':'p2-rfc-text-window','processor_version':version,'capabilities':['metadata'],'produced_at':oracle['frozen_utc'],'analysis_status':'ready','idempotency_key':'sha256:'+hashlib.sha256(('aibox-derived-v1\n'+revision+'\n'+obj+'\n'+version).encode()).hexdigest(),'source_locators':[{'type':'text','start':oracle['start'],'end':oracle['end'],'source_hash':digest,'source_id':obj,'quote':oracle['expected_phrase']}]}
    receipts=[]
    for _ in range(3):
        ref=client.register_external(**desc);receipts.append(client.upsert_derived(env,resource_ref=ref))
    assert len({r.knowledge_id for r in receipts})==1 and len({r.resource_ref for r in receipts})==1
    knowledge=receipts[0].knowledge_id;uuid.UUID(knowledge)
    settings={}
    for line in (root/'canary.env').read_text().splitlines():
        if '=' in line:
            k,v=line.split('=',1);a=shlex.split(v);settings[k]=a[0] if a else ''
    assert settings['DB_NAME']==state['clone_db']
    sql="SELECT coalesce(json_agg(row_to_json(r)), '[]') FROM (SELECT native_version,locator,resource_ref,is_current FROM source_revisions WHERE knowledge_id='"+knowledge+"') r"
    z=subprocess.run(['docker','exec','aiboxacc107-postgres-1','psql','-U',settings['DB_USER'],'-d',settings['DB_NAME'],'-At','-v','ON_ERROR_STOP=1','-c',sql],capture_output=True,text=True,check=True)
    rows=json.loads(z.stdout);assert len(rows)==1 and rows[0]['is_current'] and rows[0]['native_version']==revision and rows[0]['locator']==env['source_key'] and rows[0]['resource_ref']==receipts[0].resource_ref
    if store.get(ident)['state']=='handoff_pending':store.complete_handoff(ident)
    store.cleanup_staging(ident);assert not store.get(ident)['staging_ref'] and not (store.staging_root/(ident+'.part')).exists()
    assert hashlib.sha256(final.read_bytes()).hexdigest()==digest
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest and source.stat().st_mtime_ns==oracle['source_mtime_ns']
    proof={'result':'PASS_CANARY_TRANSIT_HANDOFF_ONLY','timestamp_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'knowledge_id':knowledge,'resource_ref':receipts[0].resource_ref,'source_sha256':digest,'state':store.get(ident)['state'],'revision_count':len(rows),'staging_cleaned':True,'final_raw_verified':True,'replays':3,'actual_canary_outage_detected':outage,'formal_soak_started':False,'temporal_tested':False}
    p=root/'transit-canary-result.json';p.write_text(json.dumps(proof,indent=2));p.chmod(0o600);print(json.dumps(proof))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--state-dir',type=Path,required=True);p.add_argument('--inject-canary-api-outage',action='store_true');a=p.parse_args()
    try:run(a.state_dir,a.inject_canary_api_outage)
    except Exception as e:print(json.dumps({'result':'FAIL_CANARY_TRANSIT','error_type':type(e).__name__,'reason':str(e)[:160] if isinstance(e,AssertionError) else 'private runtime diagnosis required'}));raise SystemExit(1)
