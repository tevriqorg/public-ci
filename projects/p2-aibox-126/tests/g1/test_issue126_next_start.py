import hashlib,json,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from p2_canary_guard import validate_next_start
class NextStart(unittest.TestCase):
 def setUp(self):
  self.root=Path('/private/canary');self.state={'source_sha':'a'*40}
  release=self.root/'releases'/self.state['source_sha'];binary=str(release/'weknora-server')
  self.props={'FragmentPath':'/units/worker.service','DropInPaths':'','ExecStart':'{ path='+binary+' ; argv[]='+binary+' ; }','WorkingDirectory':str(release),'EnvironmentFiles':'/private/canary/runtime.env (ignore_errors=no)','Environment':'WEKNORA_RUNTIME_ROLE=worker SERVER_HOST=127.0.0.1 SERVER_PORT=18083','NeedDaemonReload':'no'}
  self.files={'/units/worker.service':'[Service]\n','/private/canary/runtime.env':'AUTO_MIGRATE=false\n'}
  normalized=dict(self.props,ExecStart=binary)
  fp=hashlib.sha256(json.dumps({'properties':normalized,'files_sha256':{p:hashlib.sha256(v.encode()).hexdigest() for p,v in self.files.items()}},sort_keys=True,separators=(',',':')).encode()).hexdigest()
  self.state['launch_snapshot_sha256']={'worker':fp}
 def test_reviewed_next_launch_passes(self):self.assertTrue(validate_next_start(self.root,self.state,'worker',self.props,self.files))
 def test_env_edited_after_process_started_rejected(self):
  self.files['/private/canary/runtime.env']='AUTO_MIGRATE=true\n'
  self.assertRaises(ValueError,validate_next_start,self.root,self.state,'worker',self.props,self.files)
 def test_unit_exec_cwd_and_unreloaded_edits_rejected(self):
  for key,value in [('ExecStart','{ path=/wrong ; argv[]=/wrong ; }'),('WorkingDirectory','/mutable/worktree'),('NeedDaemonReload','yes')]:
   self.assertRaises(ValueError,validate_next_start,self.root,self.state,'worker',dict(self.props,**{key:value}),self.files)
 def test_runtime_pid_timestamp_fields_do_not_invalidate_same_launch(self):
  props=dict(self.props,ExecStart=self.props['ExecStart'].replace(' ; }',' ; ignore_errors=no ; start_time=123 ; pid=99 ; }'))
  self.assertTrue(validate_next_start(self.root,self.state,'worker',props,self.files))
 def test_new_unreviewed_dropin_rejected(self):
  props=dict(self.props,DropInPaths='/units/new.conf');files=dict(self.files,**{'/units/new.conf':'[Service]\nEnvironment=NEW=1\n'})
  self.assertRaises(ValueError,validate_next_start,self.root,self.state,'worker',props,files)
if __name__=='__main__':unittest.main()
