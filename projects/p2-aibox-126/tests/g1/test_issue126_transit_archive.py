import concurrent.futures,hashlib,sys,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from implementation.ingress.store import IngressStore,IngressError
class ArchiveSafety(unittest.TestCase):
 def test_different_sessions_cannot_overwrite_same_target(self):
  with tempfile.TemporaryDirectory() as tmp:
   store=IngressStore(tmp);ids=[]
   for name,content in [('first',b'one'),('second',b'two')]:
    s=store.create('test',name,'public:'+name);ids.append(s['ingest_id']);store.stage_bytes(ids[-1],content);store.begin_analysis(ids[-1]);store.mark_analyzed(ids[-1],'durable:test')
   barrier=threading.Barrier(2);verify=IngressStore._verify
   def synchronized(path,*args):
    ok=verify(path,*args)
    if path.name.startswith('.commit-'):barrier.wait(timeout=5)
    return ok
   def commit(ingest):
    try:return store.commit(ingest,'shared.txt')['ingest_id']
    except IngressError:return None
   with patch.object(IngressStore,'_verify',side_effect=synchronized):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(commit,ids))
   winners=[x for x in results if x]
   self.assertEqual(len(winners),1,'both sessions claimed different archived bytes at one target')
   row=store.get(winners[0]);self.assertEqual(hashlib.sha256((Path(tmp)/'final/shared.txt').read_bytes()).hexdigest(),row['content_hash'])
 def test_file_uri_roundtrip_and_cleanup_for_escaped_names(self):
  with tempfile.TemporaryDirectory(prefix='root space ') as tmp:
   store=IngressStore(tmp);s=store.create('test','uri','public:uri');i=s['ingest_id'];store.stage_bytes(i,b'public bytes');store.begin_analysis(i);store.mark_analyzed(i,'durable:test');store.commit(i,'中文 space #.txt')
   self.assertEqual(store.final_path(i).read_bytes(),b'public bytes');self.assertTrue(store.can_cleanup_staging(i));store.cleanup_staging(i)
if __name__=='__main__':unittest.main()
