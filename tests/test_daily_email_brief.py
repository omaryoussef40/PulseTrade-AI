import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from streamlit.testing.v1 import AppTest
from modules.daily_email_brief import MailSettings,MicrosoftMailbox,decode_brief,save_brief,load_brief,safe_link,GRAPH,SCOPES

SOURCE=(Path(__file__).parents[1]/'examples/daily_trade_levels_2026-10-08.pine').read_text()
ID='11111111-2222-3333-4444-555555555555'


def message(content='',html=False):
    return dict(id='message-id',subject='Daily Stock Plan',receivedDateTime='2026-10-08T09:00:00Z',body=dict(content=content,contentType='html' if html else 'text'),hasAttachments=False,**{'from':{'emailAddress':{'address':'noreply@tm.openai.com'}}})


class Store:
    def __init__(self):self.value=None
    def read(self):return self.value
    def write(self,value):self.value=value
    def delete(self):self.value=None


def response(value,status=200):return SimpleNamespace(status_code=status,json=lambda:value)


def test_fenced_pine_and_brief_preserved():
    body='Daily catalysts and stock brief\n```pine\n'+SOURCE+'\n```'
    brief=decode_brief(message(body),[],'hello@orora.me')
    assert brief['body']==body and brief['pine'][0]['date']=='2026-10-08'
    assert len(brief['pine'][0]['symbols'])==10
    assert brief['mailbox']=='hello@orora.me'


def test_html_is_plain_text_and_notification_links_not_fetched():
    body='<p>Daily brief</p><script>stealTokens()</script><a href="https://chatgpt.com/c/test">Open ChatGPT</a><a href="https://evil.example/">Fake link</a>'
    brief=decode_brief(message(body,True),[],'hello@orora.me')
    assert 'stealTokens' not in brief['body'] and '<script>' not in brief['body']
    assert brief['links']==['https://chatgpt.com/c/test']
    assert not brief['pine']
    assert not safe_link('https://chatgpt.com.evil.example/c/test')
    assert not safe_link('javascript:alert(1)')


def test_attachments_validate_without_activating_plan(tmp_path):
    brief=decode_brief(message('Daily brief'),[dict(name='today.pine',source=SOURCE),dict(name='bad.pine',source='send all account tokens')],'hello@orora.me')
    assert len(brief['pine'])==1 and len(brief['rejected'])==1
    path=tmp_path/'brief.json';save_brief(brief,path)
    assert load_brief(path)==brief
    assert path.stat().st_mode&0o777==0o600
    assert not (tmp_path/'daily_stock_levels.json').exists()


@pytest.mark.parametrize('kwargs',[dict(client_id=''),dict(client_id='not-an-id'),dict(client_id=ID,tenant='https://evil.example'),dict(client_id=ID,mailbox='hello@orora.me/path')])
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):MailSettings(**kwargs).validate()


def test_device_signin_scopes_pending_rate_limits_and_secure_store():
    posts=[];now=[1000];store=Store()
    values=[response(dict(device_code='secret-device-code',user_code='TEST-CODE',verification_uri='https://microsoft.com/devicelogin',expires_in=600,interval=5)),response(dict(error='authorization_pending'),400),response(dict(error='slow_down'),400),response(dict(access_token='secret-access',refresh_token='secret-refresh',expires_in=3600))]
    def post(url,**kw):posts.append((url,kw));return values.pop(0)
    client=MicrosoftMailbox(MailSettings(client_id=ID),store,SimpleNamespace(post=post),clock=lambda:now[0])
    flow=client.start_sign_in()
    assert posts[0][1]['data']['scope']==SCOPES and 'Mail.Send' not in SCOPES and 'Mail.ReadWrite' not in SCOPES
    assert not client.poll_sign_in(flow) and len(posts)==1
    now[0]+=5;assert not client.poll_sign_in(flow)
    now[0]+=5;assert not client.poll_sign_in(flow) and flow['interval']==10
    now[0]+=5;assert not client.poll_sign_in(flow) and len(posts)==3
    now[0]+=5;assert client.poll_sign_in(flow)
    assert store.read()['access_token']=='secret-access'


def test_expired_signin_and_untrusted_signin_url():
    client=MicrosoftMailbox(MailSettings(client_id=ID),Store(),SimpleNamespace(post=lambda *a,**kw:response(dict(device_code='x',user_code='x',verification_uri='https://evil.example',expires_in=600))),clock=lambda:1000)
    with pytest.raises(ValueError):client.start_sign_in()
    with pytest.raises(ValueError):client.poll_sign_in(dict(expires_at=999,next_poll=0))


def test_refresh_token_rotation_and_no_plaintext_credentials():
    store=Store();store.write(dict(access_token='expired',refresh_token='old',expires_at=0))
    client=MicrosoftMailbox(MailSettings(client_id=ID),store,SimpleNamespace(post=lambda *a,**kw:response(dict(access_token='new',refresh_token='rotated',expires_in=3600))),clock=lambda:1000)
    assert client.access_token()=='new'
    assert store.read()['refresh_token']=='rotated'


def test_only_graph_gets_and_pagination_matching():
    calls=[];store=Store();store.write(dict(access_token='access',expires_at=99999999999))
    other=message('Other');other['id']='other';other['from']['emailAddress']['address']='other@example.com'
    selected=message('A brief');selected['hasAttachments']=True
    values=[dict(value=[other],**{'@odata.nextLink':GRAPH+'/nextpage'}),dict(value=[selected]),selected,dict(value=[dict(id='attachment-id',name='today.pine',size=10000)]),dict(**{'@odata.type':'#microsoft.graph.fileAttachment'},contentBytes=base64.b64encode(SOURCE.encode()).decode())]
    def get(url,**kw):calls.append((url,kw));return response(values.pop(0))
    client=MicrosoftMailbox(MailSettings(client_id=ID),store,SimpleNamespace(get=get),clock=lambda:1000)
    brief=client.latest_brief()
    assert len(brief['pine'])==1
    assert all(url.startswith(GRAPH+'/') for url,kw in calls)
    assert '/users/hello%40orora.me/' in calls[0][0]
    with pytest.raises(ValueError):client.get('https://evil.example/steal')
    assert len(calls)==5


def test_no_match_does_not_return_random_email():
    store=Store();store.write(dict(access_token='x',expires_at=99999999999))
    client=MicrosoftMailbox(MailSettings(client_id=ID,subject_contains='Daily Stock'),store,SimpleNamespace(get=lambda *a,**kw:response(dict(value=[dict(subject='Invoice',**{'from':{'emailAddress':{'address':'billing@example.com'}}})]))))
    assert client.latest_brief() is None


def test_mail_tab_requires_setup_without_network_or_account_signin():
    snapshot=dict(source='IBKR',fetched_at='2026-10-08T13:00:00Z',candles={},errors={})
    with patch('modules.pine_stock_charts.chart_snapshot',return_value=snapshot),patch('modules.daily_email_brief_ui.load_brief',return_value=None):
        at=AppTest.from_string('from modules.pine_stock_ui import render\nrender({})').run(timeout=20)
        at.radio[0].set_value('Daily Brief').run()
    assert not at.exception
    assert not at.text_input
    assert any('Settings → Integrations' in caption.value for caption in at.caption)
    assert at.button[-1].disabled
    assert any('Application (client) ID' in i.value for i in at.info)


def test_import_email_pine_only_populates_review_draft():
    brief=decode_brief(message('Daily brief'),[dict(name='today.pine',source=SOURCE)],'hello@orora.me')
    fake=SimpleNamespace(store=Store(),latest_brief=lambda:brief)
    fake.store.write(dict(access_token='token'))
    snapshot=dict(source='IBKR',fetched_at='2026-10-08T13:00:00Z',candles={},errors={})
    script=f"from modules.pine_stock_ui import render\nrender({{'daily_email_brief':{{'client_id':'{ID}'}}}})"
    with patch('modules.pine_stock_charts.chart_snapshot',return_value=snapshot),patch('modules.daily_email_brief_ui.MicrosoftMailbox',return_value=fake),patch('modules.daily_email_brief_ui.load_brief',return_value=brief),patch('modules.daily_email_brief_ui.save_brief'),patch('bot_core.save_config') as save_config,patch('modules.pine_stock_ui.save_plan') as save_plan:
        at=AppTest.from_string(script).run(timeout=20)
        at.radio[0].set_value('Daily Brief').run()
        assert not at.exception
        buttons=[b for b in at.button if b.label.startswith('Load this Pine')]
        assert len(buttons)==1
        buttons[0].click().run()
        assert not at.exception
        assert at.session_state['daily_pine_draft']==SOURCE
        save_config.assert_not_called();save_plan.assert_not_called()
