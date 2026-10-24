"""A replica restart must never apply unreviewed working-tree migrations."""
from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import p2_canary_guard as guard
class MigrationSafety(unittest.TestCase):
 def test_enabled_startup_migration_is_rejected_before_fault_injection(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   env={'DB_NAME':'aibox126_canary_test','REDIS_DB':'14','REDIS_PREFIX':'aibox126-canary','WEKNORA_REDIS_NAMESPACE':'aibox126-canary','REDIS_ADDR':'127.0.0.1:56379','AIBOX_INGRESS_ALLOWED_RESOURCE_PREFIX':'local://10000/aibox126/','LOCAL_STORAGE_BASE_DIR':str(root/'storage'),'AUTO_MIGRATE':'true','DB_HOST':'127.0.0.1','DB_PORT':'55432'}
   state={'clone_db':'aibox126_canary_test','original_db':'original'}
   procs={role:dict(env,WEKNORA_RUNTIME_ROLE=role,SERVER_HOST='127.0.0.1',SERVER_PORT='18082' if role=='api' else '18083',WEKNORA_WORKER_POOLS='core') for role in ('api','worker')}
   with patch.object(guard,'_assert_dedicated_storage'):
    with self.assertRaises(ValueError):guard.verify_canary_runtime(root,state,env,procs)
if __name__=='__main__':unittest.main()
