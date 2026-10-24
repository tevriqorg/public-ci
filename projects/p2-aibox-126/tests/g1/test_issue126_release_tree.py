"""Exact binary alone is insufficient: reject drifted runtime files/schema."""
import hashlib,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from p2_canary_guard import verify_release_tree
class ReleaseTree(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)
  files={'weknora-server':b'binary','SOURCE_REVISION':b'a'*40,'config/config.yaml':b'config','migrations/versioned/000121_batch.up.sql':b'SELECT 1;'}
  for name,data in files.items():
   p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
  rows=[hashlib.sha256(data).hexdigest()+'  ./'+name for name,data in files.items()]
  p=self.root/'SHA256SUMS';p.write_text('\n'.join(rows)+'\n')
  self.state={'source_sha':'a'*40,'release_manifest_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'schema_version':121}
 def test_complete_reviewed_tree_passes(self):self.assertEqual(verify_release_tree(self.root,self.state),121)
 def test_modified_migration_fails(self):
  (self.root/'migrations/versioned/000121_batch.up.sql').write_text('DROP TABLE data;')
  self.assertRaises(ValueError,verify_release_tree,self.root,self.state)
 def test_unlisted_new_migration_fails(self):
  (self.root/'migrations/versioned/000122_unreviewed.up.sql').write_text('SELECT 2;')
  self.assertRaises(ValueError,verify_release_tree,self.root,self.state)
 def test_wrong_source_and_schema_fail(self):
  for k,v in [('source_sha','b'*40),('schema_version',120),('release_manifest_sha256','0'*64)]:
   self.assertRaises(ValueError,verify_release_tree,self.root,dict(self.state,**{k:v}))
 def test_symlink_config_fails(self):
  p=self.root/'config/config.yaml';p.unlink();p.symlink_to(self.root/'SOURCE_REVISION')
  self.assertRaises(ValueError,verify_release_tree,self.root,self.state)
if __name__=='__main__':unittest.main()
