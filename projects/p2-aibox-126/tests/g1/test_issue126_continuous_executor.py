"""Failure delivery must never be fabricated; imports never start a daemon."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
P=Path(__file__).resolve().parents[2]/'scripts/issue126_continuous_executor.py'
spec=importlib.util.spec_from_file_location('executor_under_test',P)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Delivery(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name);self.ctx=patch.object(m,'ROOT',self.root);self.ctx.start();self.addCleanup(self.ctx.stop)
  m.state={'last':{},'alerts':{}}
 def test_failed_delivery_is_retried_not_acknowledged(self):
  with patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],1,stdout='')) as call:
   m.alarm('test.service','FAIL');m.alarm('test.service','FAIL')
   self.assertEqual(call.call_count,2);self.assertEqual(m.state['alerts'],{})
 def test_delivered_failure_deduplicates(self):
  with patch.object(m.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout='https://github.com/example/comment\n')) as call:
   m.alarm('test.service','FAIL');m.alarm('test.service','FAIL')
   self.assertEqual(call.call_count,1);self.assertEqual(len(m.state['alerts']),1)
   self.assertTrue((self.root/'state.json').exists())
 def test_delivery_timeout_never_claims_sent(self):
  with patch.object(m.subprocess,'run',side_effect=subprocess.TimeoutExpired('gh',25)):
   m.alarm('test.service','FAIL');self.assertEqual(m.state['alerts'],{})
if __name__=='__main__':unittest.main()
