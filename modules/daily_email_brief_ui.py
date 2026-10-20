"""Microsoft sign-in and daily email brief inside the stock scanner."""
from dataclasses import asdict
import os
import time
import pandas as pd
import streamlit as st
from .daily_email_brief import MailSettings,MicrosoftMailbox,load_brief,save_brief,BRIEF_FILE,safe_link


def render(app, settings_only=False):
    from bot_core import load_config,save_config
    st.subheader('Daily Brief')
    raw=app.get('daily_email_brief',{})
    settings=MailSettings(**raw) if raw else MailSettings(client_id=os.getenv('PULSETRADE_MS_CLIENT_ID',''))
    st.caption(f'Microsoft 365 · {settings.mailbox} · Read-only email connection')
    if settings_only:
        with st.expander('Email connection and matching rules',expanded=not settings.client_id):
            mailbox=st.text_input('Receiving mailbox',value=settings.mailbox)
            client_id=st.text_input('Microsoft Application (client) ID',value=settings.client_id,help='Public app ID from Microsoft Entra. No password or client secret is required.')
            tenant=st.text_input('Microsoft tenant',value=settings.tenant)
            sender=st.text_input('Sender contains',value=settings.sender_contains,help='For example openai. Leave blank to match any sender.')
            subject=st.text_input('Subject contains',value=settings.subject_contains,help='Use part of your daily brief subject to select the right email.')
            days=st.number_input('Search the last how many days?',min_value=1,max_value=90,value=settings.lookback_days)
            automatic=st.checkbox('Check for a new brief every 5 minutes while this view is open',value=settings.automatic)
            settings=MailSettings(mailbox,client_id.strip(),tenant.strip(),sender,subject,int(days),automatic)
            if st.button('Save email connection settings'):
                try:
                    settings.validate()
                    config=load_config();config['daily_email_brief']=asdict(settings);save_config(config)
                    st.session_state.pop('microsoft_brief_signin',None)
                    st.success('Email settings saved. Connect with Microsoft below.')
                except ValueError as exc:st.error(str(exc))
            with st.expander('One-time Microsoft setup'):
                st.markdown('1. Open Microsoft Entra → App registrations → New registration. Name it **PulseTrade Daily Brief** and use your organization’s directory.\n2. Add Microsoft Graph **delegated** permissions `Mail.Read` and `Mail.Read.Shared`.\n3. Under Authentication → Advanced settings, enable **Allow public client flows**.\n4. Copy the **Application (client) ID** here, save, and connect. If your organization requires admin consent, its Microsoft administrator must grant it.')
                st.link_button('Open Microsoft app registrations','https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade')
                st.link_button('Microsoft registration guide','https://learn.microsoft.com/en-us/graph/auth-register-app-v2')
                st.caption('For a shared hello@orora.me mailbox, sign in with the Microsoft account that has delegated access to that mailbox.')
        return
    st.caption('Configure the mailbox under Settings → Integrations.')
    client=None;connected=False
    try:
        settings.validate();client=MicrosoftMailbox(settings);connected=bool(client.store.read())
    except ValueError as exc:
        if settings.client_id:st.error(str(exc))
        else:st.info('Add your Microsoft Application (client) ID under Settings → Integrations to enable direct email loading.')
    if client:
        if not connected:
            if st.button('Connect Microsoft mailbox'):
                try:
                    flow=client.start_sign_in()
                    flow['settings_key']=settings.cache_key
                    st.session_state['microsoft_brief_signin']=flow
                except Exception as exc:
                    st.error(str(exc))
            flow=st.session_state.get('microsoft_brief_signin')
            if flow and flow.get('settings_key')==settings.cache_key:
                st.link_button('Sign in with Microsoft',flow['verification_uri'])
                st.code(flow['user_code'],language=None)
                st.caption('Open Microsoft sign-in, enter this code and approve read access. Then check sign-in below.')
                if st.button('I have signed in — check connection'):
                    try:
                        if client.poll_sign_in(flow):
                            st.session_state.pop('microsoft_brief_signin',None)
                            st.success('Microsoft sign-in connected. Load your daily brief below.')
                            connected=True
                        else:st.info('Waiting for Microsoft sign-in. Finish in your browser and check again shortly.')
                    except Exception as exc:
                        st.session_state.pop('microsoft_brief_signin',None);st.error(str(exc))
        else:st.success(f'Microsoft sign-in saved for the {settings.mailbox} connection.')
        if connected and st.button('Disconnect Microsoft and clear saved brief'):
            try:
                client.store.delete();BRIEF_FILE.unlink(missing_ok=True)
                st.session_state.pop('mail_brief_last_check',None)
                st.rerun()
            except Exception as exc:st.error(str(exc))
    @st.fragment(run_every=300 if connected and settings.automatic else None)
    def brief_panel():
        manual=st.button('Check inbox now',disabled=not connected)
        last=st.session_state.get('mail_brief_last_check',{})
        due=connected and settings.automatic and (last.get('key')!=settings.cache_key or time.time()-last.get('time',0)>=295)
        if manual or due:
            started=time.time()
            try:
                with st.spinner('Reading the latest matching email…'):
                    brief=client.latest_brief()
                if brief:save_brief(brief)
                else:st.info('No matching brief found. Check the sender, subject and Inbox folder rules in Settings → Integrations.')
            except Exception as exc:st.error(f'Email check could not finish: {exc}')
            finally:st.session_state['mail_brief_last_check']=dict(key=settings.cache_key,time=started)
        brief=load_brief()
        if not brief or brief.get('mailbox','').casefold()!=settings.mailbox.casefold():
            st.info('Your latest brief will appear here after Microsoft sign-in and an inbox check.')
            return
        st.write(brief['subject'])
        received=pd.Timestamp(brief['received_at'])
        st.caption(f"From {brief['sender']} · Received {received.tz_convert('Asia/Dubai').strftime('%Y-%m-%d %H:%M')} Dubai · Source: Microsoft Graph")
        st.text_area('Email brief',value=brief['body'],height=320,disabled=True,key='email_body_'+brief['message_id'])
        for i,url in enumerate(brief.get('links',[])):
            if safe_link(url):st.link_button('Open linked brief in ChatGPT' if 'chatgpt' in url or 'chat.openai' in url else 'Open email in Outlook',url)
        for code in brief.get('pine',[]):
            st.write(f"{code['name']} · {len(code['symbols'])} tickers · Levels dated {code['date']}")
            if st.button('Load this Pine into the scanner for review',key='mail_pine_'+code['hash']):
                st.session_state['daily_pine_draft']=code['source']
                st.session_state['daily_pine_source']=code['source']
                st.success('Loaded. Open Charts to preview, or Daily Plan to review and save for the engine.')
        if not brief.get('pine'):
            st.info('This email has no supported Pine code. If it only contains a ChatGPT notification link, open that link and paste the code under Daily Plan.')
        for rejected in brief.get('rejected',[]):st.warning(f"{rejected['name']}: {rejected['reason']}")
        st.caption('Incoming email never activates a trading plan. Imported code is validated and must be reviewed and saved through the existing stock settings.')
    brief_panel()
