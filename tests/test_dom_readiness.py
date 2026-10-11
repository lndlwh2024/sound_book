import json
from types import SimpleNamespace

import pytest

from src.publisher.dom_readiness import account_dom_state, target_dom_block_reason
from src.publisher.platforms import CHANNELS


def report(root, platform, account_id='a'*32, *, editor=True, **overrides):
    data = {'schema_version':1, 'platform':platform, 'account_id':account_id,
            'url':CHANNELS[platform].url, 'stage':'上传前', 'frames':[{'nodes':[
                {'tag':'input','type':'file','accept':'.mp4,.mov','visible':False},
                *([{'tag':'div','contentEditable':True,'visible':True}] if editor else [])]}]}
    data.update(overrides)
    path = root/platform/account_id/'session'/'capture.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data),encoding='utf-8-sig')
    return path


@pytest.mark.parametrize('platform',list(CHANNELS))
def test_capture_survives_restart_rename_and_unlocks_implemented_adapter(tmp_path,platform):
    path = report(tmp_path,platform)
    target = SimpleNamespace(platform=platform,account=SimpleNamespace(account_id='a'*32,account_name='新昵称'))
    state = account_dom_state(tmp_path,platform,target.account.account_id)
    assert state.publish_reports == (path,)
    reason = target_dom_block_reason([target],tmp_path)
    assert not reason


@pytest.mark.parametrize('override',[{'platform':'youtube'},{'account_id':'b'*32},
                                    {'url':'https://other.example/'},{'frames':[]}])
def test_mismatched_or_empty_reports_never_pass_gate(tmp_path,override):
    path=report(tmp_path,'kuaishou')
    data=json.loads(path.read_text(encoding='utf-8-sig'))
    data.update(override)
    path.write_text(json.dumps(data),encoding='utf-8')
    state = account_dom_state(tmp_path,'kuaishou','a'*32)
    assert not state.reports and state.invalid_count==1


def test_another_account_and_session_manifest_do_not_count(tmp_path):
    report(tmp_path,'xiaohongshu')
    path=tmp_path/'xiaohongshu'/('b'*32)/'session'/'session.json'
    path.parent.mkdir(parents=True)
    path.write_text('{}',encoding='utf-8')
    state=account_dom_state(tmp_path,'xiaohongshu','b'*32)
    assert not state.reports and state.invalid_count==0
    assert '尚未采集' in state.message


def test_partial_and_corrupt_reports_have_distinct_feedback(tmp_path):
    path=report(tmp_path,'kuaishou',editor=False)
    state=account_dom_state(tmp_path,'kuaishou','a'*32)
    assert state.reports and not state.publish_reports
    assert '已采集' in state.message and '尚缺' in state.message
    path.write_text('{broken',encoding='utf-8')
    state=account_dom_state(tmp_path,'kuaishou','a'*32)
    assert '报告无效' in state.message


def test_all_four_platforms_require_the_chosen_accounts_dom(tmp_path):
    targets=[SimpleNamespace(platform=p,account=SimpleNamespace(account_id='a'*32,account_name=p))
             for p in CHANNELS]
    reason = target_dom_block_reason(targets,tmp_path)
    assert all(CHANNELS[p].name in reason for p in CHANNELS)
    for p in CHANNELS:
        report(tmp_path,p)
    assert not target_dom_block_reason(targets,tmp_path)


def test_youtube_details_capture_is_valid_after_video_input_is_removed(tmp_path):
    path = report(tmp_path,'youtube',frames=[{'nodes':[
        {'tag':'div','contentEditable':True,'ariaLabel':'添加视频标题','visible':True},
        {'tag':'div','contentEditable':True,'ariaLabel':'视频说明','visible':True},
        {'tag':'input','type':'file','accept':'image/jpeg,image/png','visible':False}]}])
    assert account_dom_state(tmp_path,'youtube','a'*32).publish_reports == (path,)
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    for node in data['frames'][0]['nodes']:
        node['visible'] = False
    path.write_text(json.dumps(data),encoding='utf-8')
    assert not account_dom_state(tmp_path,'youtube','a'*32).publish_reports


def legacy_report(tmp_path, **overrides):
    root=tmp_path/'data'/'multi_platform'/'dom_reports'
    root.mkdir(parents=True,exist_ok=True)
    path=root.parent.parent/'douyin_dom_report_20000101_000000.json'
    data={'url':'https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page',
          'title':'抖音创作者中心','timestamp':'2000-01-01T00:00:00Z',
          'inputs':[{'tag':'input','type':'file','accept':'video/*,.mp4'},
                    {'tag':'input','type':'text','placeholder':'填写作品标题，为作品获得更多流量'}],
          'textareas':[{'tag':'div','placeholder':'添加作品简介'}],
          'buttons':[{'innerText':'发布'}],'modals':[]}
    data.update(overrides)
    path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8-sig')
    return root,path


def test_original_douyin_capture_survives_fresh_process_without_migration(tmp_path):
    import subprocess
    import sys
    root,path=legacy_report(tmp_path)
    original=path.read_bytes()
    state=account_dom_state(root,'douyin','01_5006c0')
    assert state.publish_reports==(path,) and state.legacy_reports==(path,)
    assert '兼容旧版' in state.message
    code=('from src.publisher.dom_readiness import account_dom_state; import sys; '
          "s=account_dom_state(sys.argv[1],'douyin','01_5006c0'); "
          'assert len(s.publish_reports)==1 and len(s.legacy_reports)==1')
    subprocess.run([sys.executable,'-c',code,str(root)],check=True)
    assert path.read_bytes()==original
    assert not (root/'douyin').exists()  # No fabricated account binding or migration files.


@pytest.mark.parametrize('overrides',[
    {'url':'https://creator.douyin.com/login'},
    {'url':'https://other.example/creator-micro/content/post/video'},
    {'url':'http://creator.douyin.com/creator-micro/content/post/video'},
    {'account_id':'other-account'}, {'platform':'kuaishou'}, {'schema_version':1},
    {'frames':[]}, {'inputs':[],'textareas':[]}, {'inputs':'invalid'},
])
def test_legacy_compatibility_rejects_wrong_origin_identity_and_format(tmp_path,overrides):
    root,path=legacy_report(tmp_path,**overrides)
    assert not account_dom_state(root,'douyin','01_5006c0').publish_reports


@pytest.mark.parametrize('platform',['xiaohongshu','kuaishou','youtube'])
def test_legacy_douyin_template_never_satisfies_other_platforms(tmp_path,platform):
    root,path=legacy_report(tmp_path)
    assert not account_dom_state(root,platform,'01_5006c0').reports


def test_legacy_incomplete_report_does_not_replace_valid_old_capture(tmp_path):
    root,path=legacy_report(tmp_path)
    broken=path.with_name('douyin_dom_report_20990101_000000.json')
    broken.write_text('{broken',encoding='utf-8')
    assert account_dom_state(root,'douyin','01_5006c0').publish_reports==(path,)
    data=json.loads(path.read_text(encoding='utf-8-sig'))
    data['inputs']=[{'tag':'input','type':'file','accept':'image/png'}]
    data['textareas']=[]
    broken.write_text(json.dumps(data),encoding='utf-8')
    assert account_dom_state(root,'douyin','01_5006c0').publish_reports==(path,)
