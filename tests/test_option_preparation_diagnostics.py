from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import pytest
from modules.pmb_scan_worker import OptionPreparation

@pytest.mark.parametrize('response,reason', [
    ('{"error":"connect: TimeoutError"}', 'connect: TimeoutError'),
    ('{"option":null}', 'No option passed'),
    ('not-json', 'invalid or empty JSON'),
])
def test_failure_is_logged_and_published(response, reason):
    now=datetime.now(timezone.utc)
    p=OptionPreparation({})
    process=Mock(returncode=0)
    process.poll.return_value=0
    process.communicate.return_value=(response,None)
    p.running[0]=(process,'COIN:CALL',now,'boundary')
    with patch('bot_core.app_log') as log:
        p.poll(now)
    assert reason in p.errors['COIN:CALL']['reason']
    assert not p.options
    log.assert_called_once()
    assert not p.running


def test_timeout_and_recovery():
    now=datetime.now(timezone.utc)
    p=OptionPreparation({})
    process=Mock(returncode=-9)
    process.poll.return_value=None
    process.communicate.return_value=('',None)
    p.running[0]=(process,'COIN:CALL',now-timedelta(seconds=151),'boundary')
    with patch('bot_core.app_log'):
        p.poll(now)
    process.kill.assert_called_once()
    assert 'deadline' in p.errors['COIN:CALL']['reason']
    process.poll.return_value=0
    process.returncode=0
    process.communicate.return_value=('{"option":{"Type":"CALL"}}',None)
    p.running[0]=(process,'COIN:CALL',now,'boundary')
    p.poll(now)
    assert 'COIN:CALL' not in p.errors
    assert p.get('COIN','CALL',now)=={'Type':'CALL'}
