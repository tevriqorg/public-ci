import sys
import tempfile
import sqlite3
import unittest
import contextlib
import io
import ipaddress
import json
import os
import socket
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
            def fake_scenario(catalog, scenario_id, handlers, store, *, source_sha, guard):
                seen.append(scenario_id)
                self.assertGreaterEqual(len(catalog['scenarios']), 36)
                self.assertEqual(set(handlers), {'S014'})
                return {'results':[{'run_id':'replay-S014','scenario_id':'S014',
                                    'seed':'p16-g0-v1-S014','source_sha':source_sha,
                                    'status':'PASS'}], 'complete':True}
            with patch('p16_soak.registry', return_value={'S014':object()}), \
                 patch('p16_soak.run_scenario', side_effect=fake_scenario):
                self.assertEqual(main(['scenario','--scenario-id','S014','--state',d,
                                       '--source-sha','a'*40]), 0)
            self.assertEqual(seen, ['S014'])

    def test_s026_hostile_endpoint_and_bind_injection_stays_isolated(self):
        """Remote env values and insecure bind requests cannot escape the local runner."""
        attempts=[]
        violations=[]
        base_socket=socket.socket

        def check_loopback(operation, address):
            host=address[0] if isinstance(address,tuple) and address else address
            try:
                allowed=ipaddress.ip_address(host).is_loopback
            except (ValueError, TypeError):
                allowed=host == 'localhost'
            attempts.append((operation, str(host)))
            if not allowed:
                violations.append((operation, str(host)))
                raise AssertionError('NON_LOOPBACK_SOCKET_FORBIDDEN')

        class AuditedSocket(base_socket):
            def bind(self, address):
                check_loopback('bind', address)
                return super().bind(address)

            def connect(self, address):
                check_loopback('connect', address)
                return super().connect(address)

            def connect_ex(self, address):
                check_loopback('connect_ex', address)
                return super().connect_ex(address)

        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            rejected_state=root/'rejected-state'
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(['scenario','--scenario-id','S026','--state',str(rejected_state),
                          '--source-sha','a'*40,'--bind-host','0.0.0.0'])
            self.assertEqual(raised.exception.code,2)
            self.assertFalse(rejected_state.exists())

            # Mutation canary: prove the independent socket oracle rejects an
            # insecure bind before checking that the real CLI makes no such call.
            with patch('socket.socket',AuditedSocket):
                canary=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
                try:
                    with self.assertRaisesRegex(AssertionError,'NON_LOOPBACK_SOCKET_FORBIDDEN'):
                        canary.bind(('0.0.0.0',0))
                finally:
                    canary.close()
            self.assertEqual(violations,[('bind','0.0.0.0')])
            attempts.clear()
            violations.clear()

            state=root/'evidence'
            env={
                'P16_NVR_ENDPOINT':'http://198.51.100.23:8000',
                'P16_SOAK_ENDPOINT':'https://203.0.113.41:443',
                'P16_HTTP_BIND_HOST':'0.0.0.0',
                'NVR_URL':'http://192.0.2.17:80',
                'P16_API_BIND':'::',
            }
            handler=lambda _: {'status':'PASS','oracle':'socket_and_state_audit'}
            output=io.StringIO()
            with patch.dict(os.environ,env,clear=False), \
                 patch('socket.socket',AuditedSocket), \
                 patch('p16_soak.registry',return_value={'S026':handler}), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(main(['scenario','--scenario-id','S026','--state',str(state),
                                       '--source-sha','a'*40]),0)

            self.assertEqual(violations,[])
            self.assertEqual(attempts,[])
            result=json.loads(output.getvalue())
            self.assertEqual([r['scenario_id'] for r in result['results']],['S026'])
            self.assertEqual(result['results'][0]['status'],'PASS')
            self.assertFalse(result['day_credit'])
            evidence=list((state/'runs').glob('*.json'))
            self.assertEqual(len(evidence),1)
            record=json.loads(evidence[0].read_bytes())
            from vela_soak_runtime import digest
            self.assertEqual(evidence[0].stem,digest(record))
            self.assertEqual(record['source_sha'],'a'*40)
            self.assertEqual(record['status'],'PASS')
