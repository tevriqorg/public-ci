"""#126 native RSS backfill/replay of a real public feed, no synthetic updates."""
import argparse,datetime,hashlib,json,os,shlex,subprocess,time,uuid,xml.etree.ElementTree as ET
from pathlib import Path
from p2_canary_guard import verify_live_canary, secure_canary_read
from urllib.request import Request,build_opener,HTTPRedirectHandler
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*a,**kw):return None

def run(root):
    state=verify_live_canary(root);assert state['clone_db'].startswith('aibox126_canary') and state['clone_db']!=state['original_db']
    kb=json.loads((root/'test-kb.json').read_text())['kb_id'];uuid.UUID(kb)
    feed=json.loads((root/'public-feed/manifest.json').read_text());url=feed['source_url'];assert url=='https://github.com/tailscale/tailscale/releases.atom'
    token=json.loads(secure_canary_read(root,root/'session.json'))['token'];opener=build_opener(NoRedirect())
    def call(route,body=None):
        q=Request('http://127.0.0.1:18082/api/v1'+route,data=None if body is None else json.dumps(body).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method='GET' if body is None else 'POST')
        with opener.open(q,timeout=25) as r:
            raw=r.read(2_000_001);assert len(raw)<=2_000_000;return json.loads(raw)
    marker=root/'continuous-datasource.json'
    if marker.exists():ds=json.loads(marker.read_text())
    else:
        candidates=call('/datasource?kb_id='+kb);assert isinstance(candidates,list)
        matches=[x for x in candidates if x.get('name')=='P2-126 Tailscale public Atom canary'];assert len(matches)<=1
        if matches:ds=matches[0]
        else:ds=call('/datasource',{'knowledge_base_id':kb,'name':'P2-126 Tailscale public Atom canary','type':'rss','status':'paused','sync_schedule':'','sync_mode':'incremental','sync_deletions':False,'config':{'type':'rss','credentials':{},'resource_ids':[url],'settings':{'feed_urls':url}}})
        assert ds.get('id');marker.write_text(json.dumps({'id':ds['id'],'source_url':url,'knowledge_base_id':kb},indent=2));marker.chmod(0o600)
    ident=ds['id'];uuid.UUID(ident)
    env={}
    for line in (root/'canary.env').read_text().splitlines():
        if '=' in line:
            k,v=line.split('=',1);a=shlex.split(v);env[k]=a[0] if a else ''
    assert env['DB_NAME']==state['clone_db']
    def rows():
        sql="SELECT coalesce(json_agg(row_to_json(k)), '[]') FROM (SELECT id,parse_status,metadata->>'external_id' AS external_id FROM knowledges WHERE metadata->>'datasource_id'='"+ident+"' AND deleted_at IS NULL ORDER BY id) k"
        x=subprocess.run(['docker','exec','aiboxacc107-postgres-1','psql','-U',env['DB_USER'],'-d',env['DB_NAME'],'-At','-c',sql],capture_output=True,text=True,check=True);return json.loads(x.stdout)
    def sync():
        log=call('/datasource/'+ident+'/sync',{});uuid.UUID(log['id']);deadline=time.monotonic()+180
        while True:
            data=call('/datasource/logs/'+log['id'])
            if data.get('status')!='running':break
            assert time.monotonic()<deadline,'native RSS sync timed out';time.sleep(2)
        p=root/('continuous-sync-'+log['id']+'.json');p.write_text(json.dumps(data,indent=2));p.chmod(0o600)
        assert data.get('status')=='success' and data.get('items_failed',0)==0,'native RSS sync did not succeed'
        return data
    first=sync();before=rows();assert before,'RSS backfill produced no Knowledge'
    expected={url+':'+x for x in feed['provider_ids']};actual={x['external_id'] for x in before}
    assert expected<=actual,'native provider IDs differ from frozen public feed snapshot'
    d1=call('/datasource/'+ident);cursor=d1.get('last_sync_cursor');assert cursor,'durable incremental cursor missing'
    second=sync();after=rows();assert {x['external_id']:x['id'] for x in before}=={x['external_id']:x['id'] for x in after},'unexpected source identity drift; inspect provider change before verdict'
    assert second.get('items_created',0)==0 and second.get('items_updated',0)==0,'provider change or replay reprocessed existing input'
    d2=call('/datasource/'+ident);assert d2.get('last_sync_cursor'),'cursor lost on replay'
    deadline=time.monotonic()+90
    while any(x.get('parse_status')!='completed' for x in after):
        assert time.monotonic()<deadline,'RSS Knowledge did not finish parsing/indexing'
        time.sleep(2);after=rows()
    atom=ET.fromstring((root/'public-feed'/(feed['sha256']+'.atom')).read_bytes());ns={'a':'http://www.w3.org/2005/Atom'}
    title=atom.find('a:entry/a:title',ns).text;assert title
    target=next(x['id'] for x in after if x['external_id']==url+':'+feed['provider_ids'][0])
    read=secure_canary_read(root,root/'read-key.token').strip()
    query=Request('http://127.0.0.1:18082/api/v1/knowledge-bases/'+kb+'/hybrid-search',data=json.dumps({'query_text':title,'match_count':20}).encode(),headers={'X-API-Key':read,'Content-Type':'application/json'},method='POST')
    with opener.open(query,timeout=25) as z:search=json.loads(z.read(2_000_000))
    def objects(x):
        if isinstance(x,dict):
            yield x
            for v in x.values():yield from objects(v)
        elif isinstance(x,list):
            for v in x:yield from objects(v)
    assert search.get('success') and any(x.get('knowledge_id')==target for x in objects(search.get('data'))),'real RSS retrieval missed the frozen provider entry'
    result={'result':'PASS_CANARY_CONTINUOUS_BACKFILL_REPLAY_ONLY','timestamp_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source_url':url,'source_snapshot_sha256':feed['sha256'],'datasource_id':ident,'knowledge_count':len(after),'indexed_knowledge_count':len(after),'real_search_hit':True,'provider_ids_verified':len(expected),'cursor_persisted':True,'knowledge_identity_stable':True,'replay_created':second.get('items_created',0),'replay_updated':second.get('items_updated',0),'real_content_update_tested':False,'explicit_withdrawal_tested':False,'temporal_tested':False,'formal_soak_started':False}
    p=root/'continuous-canary-result.json';p.write_text(json.dumps(result,indent=2));p.chmod(0o600);print(json.dumps(result))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--state-dir',type=Path,required=True);a=p.parse_args()
    try:run(a.state_dir)
    except Exception as e:print(json.dumps({'result':'FAIL_CANARY_CONTINUOUS','error_type':type(e).__name__,'reason':str(e)[:180] if isinstance(e,AssertionError) else 'private runtime diagnosis required'}));raise SystemExit(1)
