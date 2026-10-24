#!/usr/bin/env python3
"""Candidate Office UI against the real private backend, using only owned shells.

--dist overrides static files inside this isolated browser context. The installed
frontend stays unchanged. API and WebSockets use the existing HTTPS route.
"""
import argparse
import json
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from playwright.sync_api import sync_playwright, expect
from live_e2e import read_default_token, BROWSER_EXECUTABLE, BROWSER_ARGS

def run(base,dist,out):
    out.mkdir(parents=True,exist_ok=True)
    token=read_default_token();created=[];checks=[];cleanup=[]
    report={'passed':False,'base':base,'scope':'Candidate frontend with real HTTPS state, stream and task-owned shells. Installed UI unchanged.','checks':checks,'cleanup':cleanup}
    client=httpx.Client(base_url=base,headers={'Authorization':'Bearer '+token},timeout=30)
    try:
        state=client.get('/api/state').raise_for_status().json()
        assert {e['id'] for e in state['envs'] if e['available']} >= {'work1','work2'}
        installed_html=client.get('/').raise_for_status().text
        installed_entry=re.search(r'/assets/(index-[^" ]+\.js)',installed_html).group(1)
        candidate_entry=re.search(r'/assets/(index-[^" ]+\.js)',(dist/'index.html').read_text()).group(1)
        installed_build=installed_entry==candidate_entry
        if installed_build:
            report['scope']='Installed frontend with real HTTPS state, stream and task-owned shells. No static substitution.'
        report['static_alias']={'installed_entry':installed_entry,'candidate_entry':candidate_entry,'note':'Only this browser substitutes assets. Shared-chunk entry references use the installed entry alias to avoid a second React root. API, document and WebSockets are unmodified.'}
        report['static_alias']['enabled']=not installed_build
        if installed_build:report['static_alias']['note']='Entry identities match. All assets use the installed HTTPS route without interception.'
        name='office-proof-'+secrets.token_hex(4)
        for env in ('work1','work2'):
            row=client.post('/api/sessions',json={'env':env,'kind':'shell','name':name}).raise_for_status().json();created.append(row)
        ids={s['id'] for s in created};by_env={s['env']:s for s in created}
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=BROWSER_EXECUTABLE,args=BROWSER_ARGS)
            context=browser.new_context(viewport={'width':390,'height':844},screen={'width':390,'height':844},is_mobile=True,has_touch=True)
            response=context.request.post(base+'/api/auth/login',data={'token':token},headers={'Origin':base});assert response.status==204
            context.add_init_script("localStorage.setItem('relay.agentView','office')")
            page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            def static(route):
                name=Path(urlsplit(route.request.url).path).name
                file=dist/'assets'/name
                if name.startswith('index-') and name.endswith('.js'):file=dist/'assets'/candidate_entry
                elif name.startswith('index-') and name.endswith('.css'):
                    candidate_css=re.search(r'/assets/(index-[^" ]+\.css)',(dist/'index.html').read_text()).group(1)
                    file=dist/'assets'/candidate_css
                if file.is_file() and file.suffix=='.js':
                    route.fulfill(content_type='text/javascript',body=file.read_text().replace('./'+candidate_entry,'./'+installed_entry))
                elif file.is_file():route.fulfill(path=str(file))
                else:route.continue_()
            if not installed_build:page.route(base+'/assets/**',static)
            outputs={sid:[] for sid in ids};terminal_ids=[]
            def opened(ws):
                m=re.search(r'/api/sessions/([^/]+)/terminal',ws.url)
                if not m:return
                sid=m.group(1);terminal_ids.append(sid)
                assert sid in ids,'This check may attach only to task-owned shells'
                def received(raw):
                    if isinstance(raw,bytes):outputs[sid].append(raw.decode('utf8','replace'))
                ws.on('framereceived',received)
            page.on('websocket',opened);page.goto(base)
            office=page.get_by_role('region',name='Agent office');expect(office).to_be_visible(timeout=20000)
            expect(office.locator('.office-summary')).not_to_contain_text('Updates paused',timeout=20000)
            assert not terminal_ids
            checks.append('Candidate Office renders real production sessions through HTTPS without attaching to an existing agent.')
            def open_owned(env):
                office.get_by_role('group',name='Office workspace').get_by_role('button',name='Work '+env[-1],exact=False).tap()
                sid=by_env[env]['id'];floor=office.locator('.office-floor.chosen')
                for _ in range(30):
                    target=floor.locator('.office-desk[data-session-id="'+sid+'"]')
                    if target.count():break
                    nextpage=floor.get_by_role('button',name='Next Work '+env[-1]+' desks',exact=True)
                    if not nextpage.is_enabled():raise AssertionError('Owned session missing from its exact office floor')
                    nextpage.tap()
                expect(target).to_be_visible();target.tap()
                assert page.url.endswith('#/s/'+sid)
                return sid
            def send_marker(env):
                sid=open_owned(env);marker='RELAY_OFFICE_'+env.upper()+'_'+secrets.token_hex(5)
                assert sid in ids and page.url.endswith('#/s/'+sid)
                page.locator('.term-compose-field').fill("printf '%s\\n' '"+marker+"'")
                expect(page.locator('.term-compose-send')).to_be_enabled(timeout=20000)
                page.locator('.term-compose-send').tap()
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    raw=''.join(outputs[sid]);clean=re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)','',raw)
                    clean=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',clean).replace('\r','')
                    if marker in [line.strip() for line in clean.split('\n')]:break
                    page.wait_for_timeout(100)
                else:raise AssertionError('No standalone execution marker from owned '+env+' shell')
                page.locator('.term-compose-field').fill('Unsent '+env+' Office draft')
                page.get_by_role('button',name='Back to Office',exact=True).tap();expect(office).to_be_visible()
                expect(office.locator('.office-floor:visible')).to_have_attribute('data-env',env)
                checks.append('Desk reaches exact '+env+' shell, executes its marker and returns to the same floor.')
            send_marker('work1');send_marker('work2')
            open_owned('work1');expect(page.locator('.term-compose-field')).to_have_value('Unsent work1 Office draft')
            page.get_by_role('button',name='Back to Office',exact=True).tap()
            open_owned('work2');expect(page.locator('.term-compose-field')).to_have_value('Unsent work2 Office draft')
            page.reload();expect(page.locator('.term-compose-field')).to_have_value('Unsent work2 Office draft',timeout=20000)
            checks.append('Same-named real workspace shells keep independent drafts across Office navigation and reload.')
            page.get_by_role('button',name='Back to Office',exact=True).tap();expect(office).to_be_visible()
            expect(office.locator('.office-floor:visible')).to_have_attribute('data-env','work2')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert set(terminal_ids)<=ids and not errors,errors
            checks.append('Office persists after reload, fits the phone viewport and never attaches to a user agent.')
            browser.close()
        report['passed']=True
    finally:
        for row in created:
            tmux=row['tmux'];r=client.post('/api/sessions/'+row['id']+'/end',json={'tmux_session_id':tmux['session_id'],'tmux_created':tmux['created'],'pane_count':tmux['pane_count']})
            cleanup.append({'id':row['id'],'env':row['env'],'end_status':r.status_code})
        current=client.get('/api/state').raise_for_status().json()['sessions']
        remaining=[s['id'] for s in current if s['id'] in {r['id'] for r in created} and s['status']=='live']
        report['owned_live_remaining']=remaining
        (out/'live-office-results.json').write_text(json.dumps(report,indent=2)+'\n');client.close()
        assert not remaining,'Task-owned shell cleanup failed'
    print(json.dumps(report,indent=2))

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--base-url',required=True);a.add_argument('--dist',type=Path,required=True);a.add_argument('--out',type=Path,required=True);args=a.parse_args();run(args.base_url.rstrip('/'),args.dist,args.out)
