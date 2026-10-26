"""A selected-file attempt ends even if every account failed preflight."""
from unittest.mock import MagicMock

import pytest

from src.publisher.multi_deploy import MultiDeployWorker, MultiTargetLedger
from tests.test_multi_deploy import setup


@pytest.mark.parametrize('manual',[True,False])
def test_all_preflight_blocked_ends_manual_but_continuous_waits(tmp_path,manual):
    directory,targets=setup(tmp_path)
    path=directory/'a.mp4'
    factory=MagicMock()
    job=MultiDeployWorker(targets,root=tmp_path/'state',uploader_factory=factory,
        interval_seconds=0,file_paths=[path] if manual else None,
        initial_blocks={target.key:{'status':'AUTH_REQUIRED','message':'二维码登录页'} for target in targets})
    waits=[]
    def wait(seconds):
        waits.append((seconds,job.report['status']))
        job.stop()  # Bound the continuous-mode observation to one scan.
    job.controlled_wait=wait
    job.run()
    factory.assert_not_called()
    assert waits==([] if manual else [(job.poll_seconds,'WAITING_ACCOUNTS')])
    assert job.report['status']==('COMPLETED' if manual else 'STOPPED')
    assert not job.report['all_selected_completed']
    item=job.report['files'][0]
    assert item['archived_path'] is None and path.is_file()
    assert all(item['results'][target.key]['status']=='AUTH_REQUIRED' for target in targets)
    ledger=MultiTargetLedger(job.root)
    assert set(ledger.data['plans'][item['sha256']]['required'])=={target.key for target in targets}
    assert all(ledger.result(item['sha256'],target.key)['status']=='AUTH_REQUIRED' for target in targets)
