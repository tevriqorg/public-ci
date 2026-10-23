"""#126: bounded real-RFC canary. Does not authorize or start formal soak."""
import argparse,datetime,hashlib,json,os,shlex,subprocess,sys,time,uuid
from pathlib import Path
from p2_canary_guard import verify_live_canary, secure_canary_read
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from implementation.ingress.live_handoff import WeKnoraHandoff
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def run(root):
    oracle=json.loads((root/'at-rest-oracle-v1.json').read_text())
    state=verify_live_canary(root)
    assert state['clone_db'].startswith('aibox126_canary') and state['clone_db']!=state['original_db']
    for unit,expected in (('aibox126-canary-api','api'),('aibox126-canary-worker','worker')):
        pid=subprocess.check_output(['systemctl','--user','show',unit,'-p','MainPID','--value'],text=True).strip();assert pid.isdecimal() and int(pid)>0,'canary service not running'
        effective=dict(x.split('=',1) for x in Path('/proc/'+pid+'/environ').read_bytes().decode().split('\0') if '=' in x)
        assert effective.get('WEKNORA_RUNTIME_ROLE')==expected,'effective runtime role is wrong'
        if expected=='worker':assert 'core' in effective.get('WEKNORA_WORKER_POOLS','').split(','),'no core consumer'
    kb=oracle['kb_id'];uuid.UUID(kb)
    read=secure_canary_read(root,root/'read-key.token').strip();ingest=secure_canary_read(root,root/'ingest-key.token').strip()
    api='http://127.0.0.1:18082/api/v1';opener=build_opener(NoRedirect())
    def call(route,body=None,key=read,jwt=False,method=None):
        headers={'Content-Type':'application/json',('Authorization' if jwt else 'X-API-Key'):('Bearer '+key if jwt else key)}
        req=Request(api+route,data=None if body is None else json.dumps(body).encode(),headers=headers,method=method or ('GET' if body is None else 'POST'))
        with opener.open(req,timeout=20) as response:
            data=response.read(2_000_001);assert len(data)<=2_000_000
            return json.loads(data)
    info=call('/knowledge-bases/'+kb);assert info['success']
    if not info['data'].get('embedding_model_id'):
        template=Path('/home/ubuntu/work/P2-AIBOX-acceptance/acceptance/vela-realistic/runtime/kb-model-config.json')
        session=json.loads(secure_canary_read(root,root/'session.json'))['token']
        configured=call('/initialization/config/'+kb,json.loads(template.read_text()),key=session,jwt=True,method='PUT');assert configured['success']
    raw=Path('/home/ubuntu/runtime/aibox-0.8-uat/real-corpus/rfc9110.txt');alias=root/'storage/10000/aibox126/rfc9110.txt'
    source=raw.read_bytes();assert hashlib.sha256(source).hexdigest()==oracle['source_sha256']
    assert raw.stat().st_mtime_ns==oracle['source_mtime_ns'] and os.path.samefile(raw,alias)
    revision='sha256:'+oracle['source_sha256'];object_id='rfc9110-get-excerpt';processor='rfc-text-window-v1'
    envelope={'source_key':'p2-126:rfc9110-at-rest','source_revision_id':revision,'derived_object_id':object_id,'title':'RFC9110 GET canary','markdown':source.decode()[oracle['start']:oracle['end']],'processor':'p2-rfc-text-window','processor_version':processor,'capabilities':['metadata'],'analysis_status':'ready','produced_at':oracle['frozen_utc'],'idempotency_key':'sha256:'+hashlib.sha256(('aibox-derived-v1\n'+revision+'\n'+object_id+'\n'+processor).encode()).hexdigest(),'source_locators':[{'type':'text','start':oracle['start'],'end':oracle['end'],'source_id':object_id,'source_hash':oracle['source_sha256'],'quote':oracle['expected_phrase']}]}
    descriptor={'resource_path':'local://10000/aibox126/rfc9110.txt','size':len(source),'sha256':oracle['source_sha256'],'name':'rfc9110.txt','mime':'text/plain'}
    route='/knowledge-bases/'+kb+'/knowledge/external-resource'
    for key,payload,status in ((read,descriptor,403),(ingest,dict(descriptor,resource_path='local://10000/aibox126/.'),400),(ingest,dict(descriptor,sha256='z'*64),400)):
        try:call(route,payload,key=key)
        except HTTPError as e:assert e.code==status,'unexpected negative-case response'
        else:raise AssertionError('server accepted forbidden descriptor/write')
    client=WeKnoraHandoff(api_url=api,api_key=ingest,kb_id=kb)
    receipts=[]
    for _ in range(3):
        ref=client.register_external(resource_path='local://10000/aibox126/rfc9110.txt',size=len(source),sha256=oracle['source_sha256'],filename='rfc9110.txt',mime='text/plain')
        receipts.append(client.upsert_derived(envelope,resource_ref=ref))
    assert len({x.knowledge_id for x in receipts})==1 and len({x.resource_ref for x in receipts})==1
    knowledge=receipts[0].knowledge_id;uuid.UUID(knowledge)
    def strings(x):
        if isinstance(x,str):yield x
        elif isinstance(x,dict):
            for v in x.values():yield from strings(v)
        elif isinstance(x,list):
            for v in x:yield from strings(v)
    deadline=time.monotonic()+90
    while True:
        chunks=call('/chunks/'+knowledge)
        if chunks.get('success') and any(oracle['expected_phrase'] in x for x in strings(chunks.get('data'))):break
        assert time.monotonic()<deadline,'indexed content missing'
        time.sleep(2)
    def objects(x):
        if isinstance(x,dict):
            yield x
            for v in x.values():yield from objects(v)
        elif isinstance(x,list):
            for v in x:yield from objects(v)
    assert any(x.get('type')=='text' and x.get('start')==oracle['start'] and x.get('source_hash')==oracle['source_sha256'] for x in objects(chunks.get('data'))),'persisted source locator missing'
    search=call('/knowledge-bases/'+kb+'/hybrid-search',{'query_text':oracle['expected_phrase'],'match_count':5})
    assert search.get('success') and any(oracle['expected_phrase'] in x for x in strings(search.get('data'))),'real search did not return oracle'
    env={}
    for line in (root/'canary.env').read_text().splitlines():
        if '=' in line:
            k,v=line.split('=',1);val=shlex.split(v);env[k]=val[0] if val else ''
    assert env['DB_NAME']==state['clone_db']
    sql="SELECT coalesce(json_agg(row_to_json(r)), '[]') FROM (SELECT revision_seq,native_version,locator,resource_ref,is_current FROM source_revisions WHERE knowledge_id='"+knowledge+"' ORDER BY revision_seq) r"
    result=subprocess.run(['docker','exec','aiboxacc107-postgres-1','psql','-U',env['DB_USER'],'-d',env['DB_NAME'],'-At','-v','ON_ERROR_STOP=1','-c',sql],capture_output=True,text=True,check=True)
    revisions=json.loads(result.stdout);assert len(revisions)==1 and revisions[0]['is_current'] and revisions[0]['native_version']==revision
    assert revisions[0]['locator']==envelope['source_key'] and revisions[0]['resource_ref']==receipts[0].resource_ref
    resource_sql="SELECT coalesce(json_agg(row_to_json(r)), '[]') FROM (SELECT ownership,content_hash,size FROM resources WHERE physical_path='local://10000/aibox126/rfc9110.txt' AND deleted_at IS NULL) r"
    stored=subprocess.run(['docker','exec','aiboxacc107-postgres-1','psql','-U',env['DB_USER'],'-d',env['DB_NAME'],'-At','-v','ON_ERROR_STOP=1','-c',resource_sql],capture_output=True,text=True,check=True)
    resources=json.loads(stored.stdout);assert len(resources)==1 and resources[0]['ownership']=='external' and resources[0]['content_hash']==oracle['source_sha256'] and resources[0]['size']==len(source)
    assert hashlib.sha256(raw.read_bytes()).hexdigest()==oracle['source_sha256'] and raw.stat().st_mtime_ns==oracle['source_mtime_ns']
    proof={'result':'PASS_CANARY_AT_REST_ONLY','timestamp_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'knowledge_id':knowledge,'resource_ref':receipts[0].resource_ref,'replays':3,'revision_count':len(revisions),'raw_bytes_copied':0,'same_inode':True,'source_sha256':oracle['source_sha256'],'oracle_sha256':hashlib.sha256((root/'at-rest-oracle-v1.json').read_bytes()).hexdigest(),'resource_count':len(resources),'resource_ownership':'external','live_negative_cases':3,'effective_runtime_roles_verified':True,'formal_soak_started':False,'temporal_tested':False}
    path=root/'at-rest-canary-result.json';path.write_text(json.dumps(proof,indent=2));path.chmod(0o600)
    print(json.dumps(proof))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--state-dir',type=Path,required=True);a=p.parse_args()
    try:run(a.state_dir)
    except Exception as e:print(json.dumps({'result':'FAIL_CANARY','error_type':type(e).__name__,'reason':str(e)[:160] if isinstance(e,AssertionError) else 'see private runtime diagnosis'}));raise SystemExit(1)
