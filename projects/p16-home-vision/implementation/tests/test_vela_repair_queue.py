import sys
import tempfile
import sqlite3
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from p16_soak import enqueue_failures

class RepairQueueTests(unittest.TestCase):
    def test_repeated_failures_deduplicate_task_preserve_observations_and_pass_does_not_close(self):
        with tempfile.TemporaryDirectory() as d:
            base=dict(scenario_id='S014',seed='fixture',source_sha='a'*40,status='FAIL')
            enqueue_failures(d,[dict(base,run_id='first'),dict(base,run_id='second')])
            enqueue_failures(d,[dict(base,run_id='first'),dict(base,run_id='green',status='PASS')])
            with sqlite3.connect(Path(d)/'repair-queue.sqlite3') as db:
                self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0],2)
                self.assertEqual(db.execute('SELECT status FROM tasks').fetchone()[0],'NEEDS_REPRO_AND_FIX')
            enqueue_failures(d,[dict(base,run_id='new-source',source_sha='b'*40)])
            with sqlite3.connect(Path(d)/'repair-queue.sqlite3') as db:
                self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],2)
