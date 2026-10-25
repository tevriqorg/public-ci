import sys
import tempfile
import sqlite3
import unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from p16_soak import enqueue_failures, main

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

    def test_conflicting_reused_run_id_is_rejected_without_losing_original(self):
        with tempfile.TemporaryDirectory() as d:
            original=dict(run_id='same-run',scenario_id='S014',seed='fixture',
                          source_sha='a'*40,status='FAIL')
            enqueue_failures(d,[original])
            changed=dict(original,source_sha='b'*40)
            with self.assertRaisesRegex(ValueError,'REPAIR_OBSERVATION_CONFLICT'):
                enqueue_failures(d,[changed])
            with sqlite3.connect(Path(d)/'repair-queue.sqlite3') as db:
                rows=db.execute('SELECT task_key,evidence_hash FROM observations').fetchall()
                self.assertEqual(len(rows),1)
                self.assertEqual(rows[0][0],__import__('p16_soak').digest(['S014','fixture','a'*40]))

    def test_invalid_input_is_rejected_before_queue_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            invalid=dict(run_id='bad',scenario_id='S014',seed='fixture',
                         source_sha='not-a-commit',status='FAIL')
            with self.assertRaisesRegex(ValueError,'INVALID_FAILURE_SOURCE_SHA'):
                enqueue_failures(d,[invalid])
            self.assertFalse((Path(d)/'repair-queue.sqlite3').exists())

    def test_mid_batch_conflict_rolls_back_new_tasks_and_observations(self):
        with tempfile.TemporaryDirectory() as d:
            existing=dict(run_id='existing',scenario_id='S014',seed='fixture',
                          source_sha='a'*40,status='FAIL')
            enqueue_failures(d,[existing])
            fresh=dict(run_id='fresh',scenario_id='S015',seed='fixture-2',
                       source_sha='c'*40,status='FAIL')
            reused=dict(existing,source_sha='b'*40)
            with self.assertRaisesRegex(ValueError,'REPAIR_OBSERVATION_CONFLICT'):
                enqueue_failures(d,[fresh,reused])
            with sqlite3.connect(Path(d)/'repair-queue.sqlite3') as db:
                self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0],1)
                self.assertIsNone(db.execute("SELECT 1 FROM observations WHERE run_id='fresh'").fetchone())

    def test_conflicting_duplicate_ids_in_one_batch_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            first=dict(run_id='same-run',scenario_id='S014',seed='fixture',
                       source_sha='a'*40,status='FAIL')
            second=dict(first,source_sha='b'*40)
            with self.assertRaisesRegex(ValueError,'REPAIR_OBSERVATION_CONFLICT'):
                enqueue_failures(d,[first,second])
            self.assertFalse((Path(d)/'repair-queue.sqlite3').exists())

    def test_cli_rejects_scenario_filter_for_continuous_mode(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit) as raised:
                main(['continuous','--scenario-id','S014','--state',d,'--source-sha','a'*40])
            self.assertEqual(raised.exception.code,2)

    def test_cli_rejects_symlink_state_root(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/'target'
            target.mkdir()
            link=Path(d)/'link'
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(SystemExit) as raised:
                main(['once','--state',str(link),'--source-sha','a'*40])
            self.assertEqual(raised.exception.code,2)
            self.assertEqual(list(target.iterdir()),[])

    def test_cli_rejects_file_as_state_root(self):
        with tempfile.TemporaryDirectory() as d:
            state=Path(d)/'state-file'
            state.write_text('preserve')
            with self.assertRaises(SystemExit) as raised:
                main(['once','--state',str(state),'--source-sha','a'*40])
            self.assertEqual(raised.exception.code,2)
            self.assertEqual(state.read_text(),'preserve')

    def test_scenario_mode_passes_only_the_selected_case_to_cycle_runner(self):
        with tempfile.TemporaryDirectory() as d:
            seen=[]
            def fake_cycle(catalog, handlers, store, *, source_sha, guard):
                seen.extend(s['id'] for s in catalog['scenarios'])
                self.assertEqual(set(handlers), {'S014'})
                return {'results':[{'run_id':'replay-S014','scenario_id':'S014',
                                    'seed':'p16-g0-v1-S014','source_sha':source_sha,
                                    'status':'PASS'}], 'complete':True}
            with patch('p16_soak.registry', return_value={'S014':object()}), \
                 patch('p16_soak.run_cycle', side_effect=fake_cycle):
                self.assertEqual(main(['scenario','--scenario-id','S014','--state',d,
                                       '--source-sha','a'*40]), 0)
            self.assertEqual(seen, ['S014'])
