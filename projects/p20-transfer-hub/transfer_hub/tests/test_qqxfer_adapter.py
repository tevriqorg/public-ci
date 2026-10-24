import importlib.util
import time
from pathlib import Path


def load_adapter(tmp_path, monkeypatch):
    api = tmp_path / 'api.token'; api.write_text('api')
    ob = tmp_path / 'ob.token'; ob.write_text('ob')
    spool = tmp_path / 'spool'; spool.mkdir()
    monkeypatch.setenv('QQXFER_BASE', str(tmp_path))
    monkeypatch.setenv('QQXFER_SPOOL', str(spool))
    monkeypatch.setenv('QQXFER_API_TOKEN_FILE', str(api))
    monkeypatch.setenv('QQXFER_OB_TOKEN_FILE', str(ob))
    monkeypatch.setenv('QQXFER_FLASH_VERIFY_TIMEOUT', '2')
    monkeypatch.setenv('QQXFER_FLASH_VERIFY_INTERVAL', '0.01')
    path = Path('transfer_hub/qqxfer_adapter/server.py').resolve()
    spec = importlib.util.spec_from_file_location(f'qqxfer_test_{time.time_ns()}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, spool


def test_uploaded_notice_waits_for_remote_verification(tmp_path, monkeypatch):
    mod, spool = load_adapter(tmp_path, monkeypatch)
    payload = spool / 'payload.bin'; payload.write_bytes(b'x' * 17)
    file_set_id = 'fs-test'
    mod._register_flash(file_set_id, payload, {'title': 'payload.bin', 'share_link': 'https://qfile.qq.com/q/test'})
    calls = {'n': 0}
    def fake_public_sizes(_):
        calls['n'] += 1
        if calls['n'] < 2:
            return []
        return [17]
    mod._public_flash_file_sizes = fake_public_sizes
    mod._update_flash_event({'file_set_id': file_set_id, 'sub_type': 'uploaded', 'uploaded_size': 17, 'total_size': 17})
    assert mod.FLASH_STATUS[file_set_id]['sub_type'] == 'verifying'
    assert payload.exists()
    assert mod.FLASH_EVENTS[file_set_id].wait(2)
    assert mod.FLASH_STATUS[file_set_id]['sub_type'] == 'uploaded'
    assert mod.FLASH_STATUS[file_set_id]['remote_verified'] is True
    assert mod.FLASH_STATUS[file_set_id]['remote_verify_source'] == 'qfile_public'
    assert not payload.exists()
