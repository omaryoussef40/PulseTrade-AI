"""Read-only Microsoft 365 daily briefs; email content is data, never commands."""
import base64
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlparse
import requests
from .pine_levels import ROOT, parse_pine

BRIEF_FILE=ROOT/'data'/'daily_email_brief.json'
GRAPH='https://graph.microsoft.com/v1.0'
SCOPES='Mail.Read Mail.Read.Shared offline_access'
SERVICE='PulseTrade Microsoft daily brief'


@dataclass(frozen=True)
class MailSettings:
    mailbox: str='hello@orora.me'
    client_id: str=''
    tenant: str='orora.me'
    sender_contains: str='openai'
    subject_contains: str=''
    lookback_days: int=30
    automatic: bool=True

    def validate(self):
        if not re.fullmatch(r'[^\s@/]+@[^\s@/]+\.[^\s@/]+',self.mailbox):
            raise ValueError('Enter a valid mailbox address.')
        try:uuid.UUID(self.client_id)
        except ValueError:raise ValueError('Enter the Microsoft Application (client) ID from your app registration.') from None
        if not re.fullmatch(r'[A-Za-z0-9.-]+',self.tenant) or not 1<=self.lookback_days<=90:
            raise ValueError('Invalid Microsoft tenant or search period.')

    @property
    def authority(self):return f'https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0'
    @property
    def cache_key(self):return hashlib.sha256(f'{self.client_id}|{self.tenant}|{self.mailbox.lower()}'.encode()).hexdigest()


class TokenStore:
    def __init__(self,settings):self.key=settings.cache_key
    def read(self):
        import keyring
        try:
            value=keyring.get_password(SERVICE,self.key)
            return json.loads(value) if value else None
        except Exception:
            raise ValueError('Cannot read the system credential store. Unlock your keychain and try again.') from None
    def write(self,value):
        import keyring
        try:keyring.set_password(SERVICE,self.key,json.dumps(value))
        except Exception:raise ValueError('Cannot save Microsoft sign-in securely in the system keychain.') from None
    def delete(self):
        import keyring
        try:
            if keyring.get_password(SERVICE,self.key):keyring.delete_password(SERVICE,self.key)
        except Exception:raise ValueError('Cannot remove the Microsoft sign-in from the system keychain.') from None


def oauth_json(response):
    body=response.json()
    if response.status_code>=400:
        error=body.get('error')
        detail=error.get('message') if isinstance(error,dict) else error
        raise ValueError(body.get('error_description') or detail or 'Microsoft request failed.')
    return body


class MicrosoftMailbox:
    def __init__(self,settings,store=None,http=requests,clock=time.time):
        settings.validate()
        self.settings=settings;self.store=store if store is not None else TokenStore(settings)
        self.http=http;self.clock=clock

    def start_sign_in(self):
        response=self.http.post(self.settings.authority+'/devicecode',data=dict(client_id=self.settings.client_id,scope=SCOPES),timeout=20)
        flow=oauth_json(response)
        uri=flow.get('verification_uri','')
        if urlparse(uri).hostname not in ('microsoft.com','www.microsoft.com','login.microsoftonline.com') or not uri.startswith('https://'):
            raise ValueError('Microsoft returned an unexpected sign-in address.')
        flow['expires_at']=self.clock()+float(flow['expires_in'])
        flow['next_poll']=self.clock()+float(flow.get('interval',5))
        return flow

    def poll_sign_in(self,flow):
        if flow['expires_at']<=self.clock():raise ValueError('Microsoft sign-in expired. Start sign-in again.')
        if self.clock()<flow['next_poll']:return False
        flow['next_poll']=self.clock()+float(flow.get('interval',5))
        response=self.http.post(self.settings.authority+'/token',data=dict(client_id=self.settings.client_id,grant_type='urn:ietf:params:oauth:grant-type:device_code',device_code=flow['device_code']),timeout=20)
        value=response.json()
        if value.get('error') in ('authorization_pending','slow_down'):
            if value['error']=='slow_down':
                flow['interval']=float(flow.get('interval',5))+5
                flow['next_poll']=self.clock()+flow['interval']
            return False
        value=oauth_json(response)
        self.save_token(value)
        return True

    def save_token(self,value,old=None):
        if not value.get('access_token'):raise ValueError('Microsoft returned no access token.')
        refresh=value.get('refresh_token') or (old or {}).get('refresh_token')
        self.store.write(dict(access_token=value['access_token'],refresh_token=refresh,expires_at=self.clock()+float(value.get('expires_in',3600))))

    def access_token(self):
        value=self.store.read()
        if not value:raise ValueError('Connect your Microsoft mailbox first.')
        if value['expires_at']<=self.clock()+60:
            if not value.get('refresh_token'):raise ValueError('Microsoft sign-in expired. Connect again.')
            response=self.http.post(self.settings.authority+'/token',data=dict(client_id=self.settings.client_id,grant_type='refresh_token',refresh_token=value['refresh_token'],scope=SCOPES),timeout=20)
            self.save_token(oauth_json(response),value)
            value=self.store.read()
        return value['access_token']

    def get(self,path,params=None):
        url=path if path.startswith('https://') else GRAPH+path
        if urlparse(url).hostname!='graph.microsoft.com' or not url.startswith(GRAPH+'/'):
            raise ValueError('Unexpected mail service address.')
        response=self.http.get(url,params=params,headers={'Authorization':'Bearer '+self.access_token(),'Prefer':'outlook.body-content-type="text"'},timeout=25)
        return oauth_json(response)

    def latest_brief(self):
        root='/users/'+quote(self.settings.mailbox,safe='')+'/mailFolders/inbox/messages'
        since=datetime.fromtimestamp(self.clock(),tz=timezone.utc)-timedelta(days=self.settings.lookback_days)
        params={'$select':'id,subject,from,receivedDateTime,hasAttachments,webLink', '$filter':'receivedDateTime ge '+since.strftime('%Y-%m-%dT%H:%M:%SZ'),'$orderby':'receivedDateTime desc','$top':50}
        page=self.get(root,params)
        for _ in range(10):
            for message in page.get('value',[]):
                sender=message.get('from',{}).get('emailAddress',{}).get('address','')
                subject=message.get('subject','')
                if self.settings.sender_contains.casefold() not in sender.casefold() or self.settings.subject_contains.casefold() not in subject.casefold():continue
                path='/users/'+quote(self.settings.mailbox,safe='')+'/messages/'+quote(message['id'],safe='')
                full=self.get(path,{'$select':'id,subject,from,receivedDateTime,body,hasAttachments,webLink'})
                attachments=[]
                if full.get('hasAttachments'):
                    attached=self.get(path+'/attachments',{'$select':'id,name,size','$top':100})
                    for item in attached.get('value',[]):
                        if item.get('name','').lower().endswith('.pine') and item.get('size',0)<=256000:
                            detail=self.get(path+'/attachments/'+quote(item['id'],safe=''))
                            if detail.get('@odata.type')=='#microsoft.graph.fileAttachment':
                                raw=base64.b64decode(detail.get('contentBytes',''),validate=True)
                                if len(raw)>256000:raise ValueError('Pine attachment exceeds 256 KB.')
                                attachments.append(dict(name=item['name'],source=raw.decode('utf-8-sig')))
                return decode_brief(full,attachments,self.settings.mailbox)
            url=page.get('@odata.nextLink')
            if not url:return None
            page=self.get(url)
        raise ValueError('The search reached 500 messages. Narrow the sender, subject or search period.')


class PlainHTML(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.parts=[];self.skip=0;self.links=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.skip+=1
        if not self.skip and tag in ('p','div','br','pre','li','tr','h1','h2','h3'):self.parts.append('\n')
        if not self.skip and tag=='a':
            href=dict(attrs).get('href','')
            if safe_link(href):self.links.append(href)
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.skip=max(0,self.skip-1)
        if not self.skip and tag in ('p','div','pre','li','tr'):self.parts.append('\n')
    def handle_data(self,value):
        if not self.skip:self.parts.append(value)


def safe_link(url):
    p=urlparse(url)
    return p.scheme=='https' and p.hostname in ('chatgpt.com','chat.openai.com','outlook.office.com','outlook.office365.com')


def decode_brief(message,attachments,mailbox):
    body=message.get('body',{});text=body.get('content','');links=[]
    if len(text.encode())>2_000_000:raise ValueError('Email body is too large.')
    if body.get('contentType','').lower()=='html':
        parser=PlainHTML();parser.feed(text);text=''.join(parser.parts);links=parser.links
    links+= [url for url in re.findall(r'https://[^\s<>"\)]+',text) if safe_link(url)]
    sources=[dict(name=a['name'],source=a['source']) for a in attachments]
    for i,match in enumerate(re.findall(r'```(?:pine(?:script)?|pinescript|text)?\s*\n(.*?)```',text,re.S|re.I)):
        if '//@version' in match:sources.append(dict(name=f'Email code block {i+1}',source=match.strip()))
    if not sources and '//@version' in text:sources.append(dict(name='Email body',source=text[text.index('//@version'):].strip()))
    candidates=[];rejected=[]
    for source in sources:
        try:
            plan=parse_pine(source['source'])
            if not any(c['hash']==plan.source_hash for c in candidates):
                candidates.append(dict(name=source['name'],source=source['source'],date=str(plan.session_date),symbols=[l.symbol for l in plan.levels],hash=plan.source_hash))
        except ValueError as exc:rejected.append(dict(name=source['name'],reason=str(exc)))
    sender=message.get('from',{}).get('emailAddress',{}).get('address','')
    return dict(mailbox=mailbox,message_id=message['id'],subject=message.get('subject',''),sender=sender,received_at=message.get('receivedDateTime'),body=text,links=sorted(set(links)),pine=candidates,rejected=rejected,source='Microsoft Graph',fetched_at=datetime.now(timezone.utc).isoformat())


def save_brief(brief,path=BRIEF_FILE):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as handle:json.dump(brief,handle,indent=2)
    os.chmod(temp,0o600);temp.replace(path)


def load_brief(path=BRIEF_FILE):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return None
