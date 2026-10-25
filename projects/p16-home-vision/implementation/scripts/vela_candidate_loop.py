import os, json, time, shutil, subprocess, uuid
from pathlib import Path
from datetime import datetime, timezone
root=Path('/home/ubuntu/p16-vela-uat')
source=root/'candidate-16603bf'
evidence=root/'candidate-evidence'
evidence.mkdir(exist_ok=True)
tmp=root/'candidate-tmp'
tmp.mkdir(exist_ok=True)
os.environ.update(TMPDIR=str(tmp),PYTHONDONTWRITEBYTECODE='1',HOME_VISION_ENVIRONMENT='synthetic-uat',HOME_VISION_ARCHIVE_AUTO_ENQUEUE='false',HOME_VISION_NVR_TIMEZONE_VERIFIED='false')
while True:
    run=uuid.uuid4().hex
    report=dict(run_id=run,source_sha='16603bf',timestamp_utc=datetime.now(timezone.utc).isoformat(),status='BLOCKED',day_credit=False,scope='candidate_known_business_regressions_not_complete_36_scenarios')
    size=sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
    if shutil.disk_usage(root).free < 6*1024**3 or size > 480*1024**2:
        report['reason']='RESOURCE_GUARD'
    else:
        with (evidence/(run+'.log')).open('xb') as log:
            try:
                result=subprocess.run(['/home/ubuntu/p16-vela-uat/venv/bin/python','-m','pytest','-q','tests/test_mock_nvr_acceptance.py','tests/test_auto_archive.py','tests/test_scan_nvr.py','tests/test_archive_worker_recovery.py','tests/test_archive_audit.py','tests/test_vela_scan_mutation.py'],cwd=source/'implementation',stdout=log,stderr=subprocess.STDOUT,timeout=240)
                report.update(status='PASS' if result.returncode==0 else 'FAIL',exit_code=result.returncode)
            except subprocess.TimeoutExpired:
                report.update(status='FAIL',reason='TEST_TIMEOUT')
            log.flush();os.fsync(log.fileno())
    with (evidence/(run+'.json')).open('x') as out:
        json.dump(report,out);out.flush();os.fsync(out.fileno())
    print(json.dumps(report),flush=True)
    time.sleep(900)
