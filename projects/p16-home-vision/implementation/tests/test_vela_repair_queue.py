import sys
import tempfile
import sqlite3
import unittest
import contextlib
import io
import ipaddress
import json
import os
import re
import socket
import shutil
import subprocess
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from p16_soak import enqueue_failures, main, registry, verify_source_revision
from vela_soak_runtime import command_handler

class RepairQueueTests(unittest.TestCase):
    def test_source_revision_requires_matching_clean_code_tree(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            code=root/'implementation'/'scripts'/'runner.py'
            code.parent.mkdir(parents=True)
            code.write_text('print("fixture")\n')
            subprocess.run(['git','init','-q'],cwd=root,check=True)
            subprocess.run(['git','config','user.name','P16 fixture'],cwd=root,check=True)
            subprocess.run(['git','config','user.email','p16-fixture@example.invalid'],cwd=root,check=True)
            subprocess.run(['git','add','implementation/scripts/runner.py'],cwd=root,check=True)
            subprocess.run(['git','commit','-qm','fixture'],cwd=root,check=True)
            sha=subprocess.run(['git','rev-parse','HEAD'],cwd=root,check=True,
                               capture_output=True,text=True).stdout.strip()
            self.assertEqual(verify_source_revision(root,sha),sha)
            with self.assertRaisesRegex(ValueError,'SOURCE_SHA_DOES_NOT_MATCH_CHECKOUT'):
                verify_source_revision(root,'a'*40)
            code.write_text('print("mutated")\n')
            with self.assertRaisesRegex(ValueError,'SOURCE_TREE_DIRTY'):
                verify_source_revision(root,sha)

            # Canonical repository invocation has implementation/ directly
            # below the Git root, so its scenario catalog is at design/.
            canonical=Path(tempfile.mkdtemp(prefix='p16-canonical-',dir=os.environ.get('TMPDIR')))
            canonical_code=canonical/'implementation'/'scripts'/'runner.py'
            canonical_code.parent.mkdir(parents=True)
            canonical_code.write_text('print("canonical fixture")\n')
            canonical_catalog=canonical/'design'/'P16R2_VELA_SOAK_G0_SCENARIOS_v1.json'
            canonical_catalog.parent.mkdir(parents=True)
            canonical_catalog.write_text('{"scenarios":[]}\n')
            (canonical/'SOURCE_REVISION').write_text('c'*40+'\n')
            subprocess.run(['git','init','-q'],cwd=canonical,check=True)
            subprocess.run(['git','config','user.name','P16 fixture'],cwd=canonical,check=True)
            subprocess.run(['git','config','user.email','p16-fixture@example.invalid'],cwd=canonical,check=True)
            subprocess.run(['git','add','implementation','design','SOURCE_REVISION'],cwd=canonical,check=True)
            subprocess.run(['git','commit','-qm','canonical fixture'],cwd=canonical,check=True)
            self.assertEqual(verify_source_revision(canonical/'implementation','c'*40),'c'*40)
            canonical_catalog.write_text('{"scenarios":[{"id":"S024"}]}\n')
            with self.assertRaisesRegex(ValueError,'SOURCE_TREE_DIRTY'):
                verify_source_revision(canonical/'implementation','c'*40)
            shutil.rmtree(canonical)
            code.write_text('print("fixture")\n')
            extra=root/'implementation'/'tests'/'untracked.py'
            extra.parent.mkdir(parents=True)
            extra.write_text('pass\n')
            with self.assertRaisesRegex(ValueError,'SOURCE_TREE_DIRTY'):
                verify_source_revision(root,sha)
            extra.unlink()
            oddly_named=root/'implementation'/'tests'/'newline\nfile.py'
            oddly_named.write_text('pass\n')
            with self.assertRaisesRegex(ValueError,'SOURCE_TREE_DIRTY'):
                verify_source_revision(root,sha)

            mirror=root/'mirror'
            project=mirror/'projects'/'p16-home-vision'
            project_code=project/'implementation'/'scripts'/'runner.py'
            project_code.parent.mkdir(parents=True)
            project_code.write_text('print("mirrored fixture")\n')
            catalog=project/'design'/'P16R2_VELA_SOAK_G0_SCENARIOS_v1.json'
            catalog.parent.mkdir(parents=True)
            catalog.write_text('{"scenarios":[]}\n')
            (project/'SOURCE_REVISION').write_text('b'*40+'\n')
            subprocess.run(['git','init','-q'],cwd=mirror,check=True)
            subprocess.run(['git','config','user.name','P16 fixture'],cwd=mirror,check=True)
            subprocess.run(['git','config','user.email','p16-fixture@example.invalid'],cwd=mirror,check=True)
            subprocess.run(['git','add','projects/p16-home-vision'],cwd=mirror,check=True)
            subprocess.run(['git','commit','-qm','mirror fixture'],cwd=mirror,check=True)
            self.assertEqual(verify_source_revision(project,'b'*40),'b'*40)
            self.assertEqual(verify_source_revision(project/'implementation','b'*40),'b'*40)
            project_code.write_text('print("changed mirror")\n')
            with self.assertRaisesRegex(ValueError,'SOURCE_TREE_DIRTY'):
                verify_source_revision(project,'b'*40)
            with self.assertRaisesRegex(ValueError,'SOURCE_TREE_DIRTY'):
                verify_source_revision(project/'implementation','b'*40)
            project_code.write_text('print("mirrored fixture")\n')
            catalog.write_text('{"scenarios":[{"id":"S024"}]}\n')
            with self.assertRaisesRegex(ValueError,'SOURCE_TREE_DIRTY'):
                verify_source_revision(project/'implementation','b'*40)

    def test_source_revision_allows_ignored_hosted_frontend_outputs(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            code=root/'implementation'/'scripts'/'runner.py'
            code.parent.mkdir(parents=True)
            code.write_text('print("fixture")\n')
            ignore=root/'implementation'/'.gitignore'
            ignore.write_text(
                '.tmp/\n__pycache__/\n*.py[cod]\n.pytest_cache/\n'
                '*.db\n*.db-*\n'
                'frontend-r2/node_modules/\nfrontend-r2/package-lock.json\n'
                'frontend-r2/*.tsbuildinfo\n'
            )
            subprocess.run(['git','init','-q'],cwd=root,check=True)
            subprocess.run(['git','config','user.name','P16 fixture'],cwd=root,check=True)
            subprocess.run(['git','config','user.email','p16-fixture@example.invalid'],cwd=root,check=True)
            subprocess.run(['git','add','implementation'],cwd=root,check=True)
            subprocess.run(['git','commit','-qm','fixture'],cwd=root,check=True)
            sha=subprocess.run(['git','rev-parse','HEAD'],cwd=root,check=True,
                               capture_output=True,text=True).stdout.strip()
            outputs=[
                root/'implementation'/'.tmp'/'frontend-dist'/'index.html',
                root/'implementation'/'frontend-r2'/'node_modules'/'vite'/'index.js',
                root/'implementation'/'frontend-r2'/'package-lock.json',
                root/'implementation'/'frontend-r2'/'tsconfig.app.tsbuildinfo',
                root/'implementation'/'app'/'__pycache__'/'config.cpython-312.pyc',
                root/'implementation'/'.pytest_cache'/'v'/'cache'/'nodeids',
                root/'implementation'/'home-vision.db',
                root/'implementation'/'home-vision.db-wal',
            ]
            for output in outputs:
                output.parent.mkdir(parents=True,exist_ok=True)
                output.write_text('synthetic hosted output')
            self.assertEqual(verify_source_revision(root,sha),sha)

    def test_cli_refuses_source_mismatch_before_state_writes(self):
        with tempfile.TemporaryDirectory() as d:
            state=Path(d)/'state'
            with patch('p16_soak.verify_source_revision',
                       side_effect=ValueError('SOURCE_SHA_DOES_NOT_MATCH_CHECKOUT')):
                with self.assertRaises(SystemExit) as raised:
                    main(['once','--state',str(state),'--source-sha','a'*40])
            self.assertEqual(raised.exception.code,2)
            self.assertFalse(state.exists())

    def test_cycle_rechecks_source_and_records_no_results_after_change(self):
        with tempfile.TemporaryDirectory() as d:
            state=Path(d)/'state'
            with patch('p16_soak.verify_source_revision',
                       side_effect=['a'*40, ValueError('SOURCE_TREE_DIRTY')]):
                with self.assertRaisesRegex(ValueError,'SOURCE_TREE_DIRTY'):
                    main(['once','--state',str(state),'--source-sha','a'*40])
            self.assertEqual(list((state/'runs').glob('*.json')),[])

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

    def test_repair_queue_rejects_symlinked_state_and_database_without_touching_target(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            outside=root/'outside'
            outside.mkdir()
            state_link=root/'state-link'
            state_link.symlink_to(outside, target_is_directory=True)
            record=dict(run_id='attempt',scenario_id='S014',seed='fixture',
                        source_sha='a'*40,status='FAIL')
            with self.assertRaisesRegex(ValueError,'REPAIR_STATE_SYMLINK_FORBIDDEN'):
                enqueue_failures(state_link,[record])
            self.assertEqual(list(outside.iterdir()),[])

            state=root/'state'
            state.mkdir()
            sentinel=outside/'sentinel.sqlite3'
            sentinel.write_bytes(b'preserve-exact-bytes')
            (state/'repair-queue.sqlite3').symlink_to(sentinel)
            with self.assertRaisesRegex(ValueError,'REPAIR_QUEUE_SYMLINK_FORBIDDEN'):
                enqueue_failures(state,[record])
            self.assertEqual(sentinel.read_bytes(),b'preserve-exact-bytes')

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

    def test_pass_fail_reuse_of_run_id_is_rejected_in_either_order(self):
        failed=dict(run_id='same-run',scenario_id='S014',seed='fixture',
                    source_sha='a'*40,status='FAIL')
        passed=dict(failed,status='PASS')
        for batch in ([failed,passed],[passed,failed]):
            with self.subTest(first_status=batch[0]['status']), tempfile.TemporaryDirectory() as d:
                with self.assertRaisesRegex(ValueError,'REPAIR_OBSERVATION_CONFLICT'):
                    enqueue_failures(d,batch)
                self.assertFalse((Path(d)/'repair-queue.sqlite3').exists())

    def test_persisted_failure_run_id_cannot_be_replayed_as_pass_or_blocked(self):
        failed=dict(run_id='same-run',scenario_id='S014',seed='fixture',
                    source_sha='a'*40,status='FAIL')
        for status in ('PASS', 'BLOCKED'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as d:
                enqueue_failures(d,[failed])
                contradictory=dict(failed,status=status)
                with self.assertRaisesRegex(ValueError,'REPAIR_OBSERVATION_CONFLICT'):
                    enqueue_failures(d,[contradictory])
                with sqlite3.connect(Path(d)/'repair-queue.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0],1)

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
                with patch('p16_soak.verify_source_revision',return_value='a'*40):
                    main(['once','--state',str(link),'--source-sha','a'*40])
            self.assertEqual(raised.exception.code,2)
            self.assertEqual(list(target.iterdir()),[])

    def test_cli_rejects_file_as_state_root(self):
        with tempfile.TemporaryDirectory() as d:
            state=Path(d)/'state-file'
            state.write_text('preserve')
            with self.assertRaises(SystemExit) as raised:
                with patch('p16_soak.verify_source_revision',return_value='a'*40):
                    main(['once','--state',str(state),'--source-sha','a'*40])
            self.assertEqual(raised.exception.code,2)
            self.assertEqual(state.read_text(),'preserve')

    def test_command_handler_marks_nested_wrapper_child(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            output=root/'child-marker'
            script=root/'check-child.py'
            script.write_text(
                'import os\nfrom pathlib import Path\n'
                f'Path({str(output)!r}).write_text(os.environ.get("P16_SOAK_WRAPPER_CHILD", ""))\n'
            )
            result=command_handler([sys.executable,str(script)],cwd=root,timeout_seconds=5)({})
            self.assertEqual(result['status'],'PASS')
            self.assertEqual(output.read_text(),'1')

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
                 patch('p16_soak.run_scenario', side_effect=fake_scenario), \
                 patch('p16_soak.verify_source_revision',return_value='a'*40):
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
                 patch('p16_soak.verify_source_revision',return_value='a'*40), \
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

    def test_s026_registered_handler_executes_the_injection(self):
        outcome=registry()['S026']({'id':'S026'})
        self.assertEqual(outcome['status'],'PASS')
        self.assertEqual(outcome['exit_code'],0)
        self.assertFalse(outcome['timed_out'])

    def test_s029_low_disk_guard_blocks_before_handler_and_preserves_sibling(self):
        from types import SimpleNamespace
        from p16_soak import shutil
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            state=root/'uat-state'
            state.mkdir()
            sibling=root/'other-service-sentinel.bin'
            sibling.write_bytes(b'other-service-state')
            import hashlib
            before=hashlib.sha256(sibling.read_bytes()).hexdigest()
            calls=[]
            handler=lambda _: (calls.append('ran') or {'status':'PASS'})
            output=io.StringIO()
            low_disk=lambda _path: SimpleNamespace(total=10,used=5,free=6*1024**3-1)
            with patch('p16_soak.registry',return_value={'S029':handler}), \
                 patch.object(shutil,'disk_usage',side_effect=low_disk), \
                 patch('p16_soak.verify_source_revision',return_value='a'*40), \
                 contextlib.redirect_stdout(output):
                code=main(['scenario','--scenario-id','S029','--state',str(state),
                           '--source-sha','a'*40])

            self.assertEqual(code,1)
            self.assertEqual(calls,[])
            result=json.loads(output.getvalue())
            self.assertFalse(result['complete'])
            self.assertEqual(result['results'][0]['status'],'BLOCKED')
            self.assertEqual(result['results'][0]['reason'],'RESOURCE_GUARD:OSError')
            records=list((state/'runs').glob('*.json'))
            self.assertEqual(len(records),1)
            from vela_soak_runtime import digest
            record=json.loads(records[0].read_bytes())
            self.assertEqual(records[0].stem,digest(record))
            self.assertEqual(record['source_sha'],'a'*40)
            self.assertEqual(record['status'],'BLOCKED')
            self.assertEqual(hashlib.sha256(sibling.read_bytes()).hexdigest(),before)

    def test_s029_system_resource_matrix_is_not_promoted_by_low_disk_only(self):
        self.assertNotIn('S029', registry())

    def test_s030_learning_loop_is_not_promoted_by_local_ledger_test(self):
        self.assertNotIn('S030', registry())

    def test_incomplete_catalog_matrices_remain_unregistered(self):
        handlers = registry()
        blocked = {'S019', 'S022', 'S027', 'S028', 'S029', 'S030', 'S031', 'S032', 'S035', 'S036'}
        self.assertEqual(
            {scenario for scenario in blocked if scenario not in handlers}, blocked,
        )
        setup = Path(__file__).resolve().parents[1] / 'frontend-r2/tests/soak-scenarios.global-setup.js'
        source = setup.read_text(encoding='utf-8')
        match = re.search(r'const expectedBlocked = new Set\(\[(.*?)\]\)', source, re.S)
        self.assertIsNotNone(match)
        self.assertEqual(set(re.findall(r"'([^']+)'", match.group(1))), blocked)

    def test_browser_replay_reuse_is_limited_to_hosted_matrix_children(self):
        config = Path(__file__).resolve().parents[1] / 'frontend-r2/playwright.config.js'
        config_source = config.read_text(encoding='utf-8')
        self.assertRegex(config_source,
                         r"reuseExistingServer:\s*process\.env\.P16_SOAK_MATRIX_CHILD === '1'")
        setup = Path(__file__).resolve().parents[1] / 'frontend-r2/tests/soak-scenarios.global-setup.js'
        setup_source = setup.read_text(encoding='utf-8')
        self.assertIn("P16_SOAK_MATRIX_CHILD: '1'", setup_source)
