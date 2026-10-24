#!/usr/bin/env python3
"""Office integration against built React and isolated state/terminal fixtures."""
import argparse
import copy
import json
import time
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect
from agent_navigation import NavigationFixture, fixture as navigation_fixture
from terminal_workspace import _session, ORIGIN, BRAVE, ARGS, wait_open

class OfficeFixture(NavigationFixture):
    def __init__(self, dist):
        super().__init__(dist)
        self.state=copy.deepcopy(navigation_fixture(dist).state)
        for row in self.state['sessions']:
            if row['env']=='work2': row['kind']='codex'
        self.stale=False
        self.stream=[]
        self.state_reads=0
    def _http(self,route):
        if urlsplit(route.request.url).path=='/api/state':
            self.state_reads+=1
            self.state['generated_at']=time.time()-(120 if self.stale else 0)
        super()._http(route)
    def install(self,page,screen=None):
        super().install(page,screen)
        def connect(ws):
            self.stream.append(ws)
            def receive(raw):
                d=json.loads(raw)
                if d.get('type')=='ping':ws.send(json.dumps({'type':'pong'}))
            ws.on_message(receive)
        page.route_web_socket('**/api/stream',connect)
    def update(self):
        self.stream[-1].send(json.dumps({'type':'changed','topics':['sessions']}))

def run(dist,out):
    out.mkdir(parents=True,exist_ok=True)
    results=[];errors=[]
    def record(text): results.append(text);print('PASS',text,flush=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=BRAVE,args=ARGS)
        ctx=browser.new_context(viewport={'width':1440,'height':1000},screen={'width':1440,'height':1000})
        page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)));fx=OfficeFixture(dist);fx.install(page,screen=(1440,1000))
        requested=[];page.on('request',lambda r:requested.append(r.url))
        page.goto(ORIGIN);expect(page.locator('.workspace-session-group')).to_have_count(2)
        assert not any('/OfficeView-' in url for url in requested)
        page.get_by_role('group',name='Agent view').get_by_role('button',name='Office',exact=True).click()
        office=page.get_by_role('region',name='Agent office')
        expect(office).to_be_visible();expect(office.locator('.office-floor')).to_have_count(2)
        expect(office.locator('.office-desk[data-session-id="work2-1"]')).to_have_attribute('data-state','working')
        assert sum(fx.connects.values())==0 and not fx.posts
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(out/'desktop-office.png'),full_page=True)
        record('Office loads on demand, renders actual fixture sessions in separate workspace floors, and opens no terminal or command on view.')
        initial={e.get_attribute('data-session-id'):e.get_attribute('data-desk') for e in office.locator('.office-desk').all()}
        fx.state['sessions'][0]['state']='idle';fx.state['sessions'][0]['pending']=None
        fx.state['sessions'].insert(0,_session('work1-new','agent0','claude','working',env='work1'))
        fx.update();expect(office.locator('[data-session-id="work1-1"].office-desk')).to_have_attribute('data-state','idle')
        for sid,slot in initial.items():expect(office.locator(f'.office-desk[data-session-id="{sid}"]')).to_have_attribute('data-desk',slot)
        fx.state['sessions']=[s for s in fx.state['sessions'] if s['id']!='work1-3'];fx.update()
        expect(office.locator('.office-desk[data-session-id="work1-3"]')).to_have_count(0)
        expect(office.locator('.office-desk[data-session-id="work1-4"]')).to_have_attribute('data-desk',initial['work1-4'])
        record('Stream state changes, insertion and removal preserve unrelated exact session desk slots.')
        expect(office.locator('.office-agent-row[data-session-id="work2-3"]')).to_contain_text('projects')
        changed=next(s for s in fx.state['sessions'] if s['id']=='work1-4');changed['state']='working';changed['pending']={'id':'new-request','source':'screen','tool':'shell','summary':'Review','detail':None,'match':[],'created_at':time.time(),'answerable':False};fx.update()
        expect(office.locator('.office-desk[data-session-id="work1-4"]')).to_have_attribute('data-state','needs_input')
        changed['pending']=None;changed['state']='unknown';fx.update()
        expect(office.locator('.office-desk[data-session-id="work1-4"]')).to_have_attribute('data-state','unknown')
        record('Pending requests take precedence over working; unknown stays distinct from idle; null project fields have a fallback.')
        fx.state['envs'][1]['available']=False
        stopped=[s for s in fx.state['sessions'] if s['env']=='work2' and s['status']=='live']
        for s in stopped:s.update(status='interrupted',state='exited',end_reason='container_stopped',ended_at=time.time())
        fx.update()
        expect(office.locator('.office-floor[data-env="work2"] .office-scene-overlay')).to_contain_text('Work 2 is offline')
        expect(office.locator('.office-floor[data-env="work2"] .office-desk')).to_have_count(0)
        assert office.locator('.office-floor[data-env="work2"] .office-agent-row').evaluate_all('(els)=>els.every(e=>e.disabled)')
        assert office.locator('.office-floor[data-env="work2"] .office-agent-row').count()==3
        page.reload();expect(office.locator('.office-floor[data-env="work2"] .office-agent-row')).to_have_count(3)
        record('Reload during a real container-stop state retains only the latest disabled session cohort.')
        for s in stopped:s.update(status='live',state='working',end_reason=None,ended_at=None)
        fx.state['envs'][1]['available']=True;fx.update();expect(office.locator('.office-desk[data-session-id="work2-1"]')).to_be_visible()
        fx.stale=True;fx.update();expect(office.locator('.office-summary')).to_contain_text('Updates paused')
        expect(office.locator('.office-desk[data-session-id="work2-1"]')).to_have_attribute('data-state','stale')
        fx.stale=False;fx.update();expect(office.locator('.office-desk[data-session-id="work2-1"]')).to_have_attribute('data-state','working')
        record('Offline floors disable access; expired snapshots stop presenting working state and recover after a fresh snapshot.')
        office.locator('.office-desk[data-session-id="work2-1"]').click();wait_open(page)
        assert page.url.endswith('#/s/work2-1');expect(page.locator('.chat-title h2')).to_contain_text('Work 2')
        page.get_by_role('button',name='Back to Office',exact=True).click();expect(office).to_be_visible()
        expect(office.locator('.office-agent-row[data-session-id="work2-1"]')).to_be_focused()
        record('Same-named Work 2 desk opens its exact existing terminal; desktop Back restores agent focus.')
        office.locator('.office-desk[data-session-id="work2-1"]').click();wait_open(page)
        page.locator('.pane-split').click();expect(page.locator('.dashboard.split')).to_be_visible()
        page.get_by_role('button',name='Back to Office',exact=True).click();expect(office).to_be_visible()
        expect(page.locator('.dashboard.split')).to_have_count(0)
        record('Back to Office exits a split workspace rather than leaving a blank pane.')
        import base64,urllib.parse
        href=office.get_by_role('link',name='License',exact=True).get_attribute('href')
        if href.startswith('data:'):
            header,body=href.split(',',1);notice=base64.b64decode(body).decode() if ';base64' in header else urllib.parse.unquote(body)
        else:notice=page.evaluate('(url)=>fetch(url).then(r=>r.text())',href)
        assert 'MIT License' in notice and 'Copyright (c) 2026 Ivan Wang' in notice
        record('The distributed build includes the upstream MIT artwork notice linked from Office.')
        # Count canvas writes directly, rather than trusting an implementation counter.
        page.evaluate('''() => {const original=CanvasRenderingContext2D.prototype.fillRect;window.officeWrites=0;CanvasRenderingContext2D.prototype.fillRect=function(...args){window.officeWrites++;return original.apply(this,args)}}''')
        page.wait_for_timeout(300);a=page.evaluate('window.officeWrites');page.wait_for_timeout(300);assert page.evaluate('window.officeWrites')>a
        page.get_by_role('button',name='Pause motion',exact=True).click();page.wait_for_timeout(100);a=page.evaluate('window.officeWrites');page.wait_for_timeout(300);assert page.evaluate('window.officeWrites')==a
        page.get_by_role('button',name='Resume motion',exact=True).click();page.emulate_media(reduced_motion='reduce');page.wait_for_timeout(100);a=page.evaluate('window.officeWrites');page.wait_for_timeout(300);assert page.evaluate('window.officeWrites')==a
        page.emulate_media(reduced_motion='no-preference')
        page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_timeout(100);a=page.evaluate('window.officeWrites');page.wait_for_timeout(300);assert page.evaluate('window.officeWrites')==a
        page.evaluate("delete document.hidden;document.dispatchEvent(new Event('visibilitychange'))")
        record('Real canvas writes stop for Pause, reduced motion and the hidden-document signal.')
        # Crowded pages use vacant slots without moving the previous desk assignments.
        fx.state['sessions'] += [_session(f'extra-{i}',f'agent{i+10}','claude','idle',env='work1') for i in range(10)];fx.update()
        expect(office.get_by_role('button',name='Next Work 1 desks')).to_be_enabled();office.get_by_role('button',name='Next Work 1 desks').click()
        target=office.locator('.office-floor[data-env="work1"] .office-desk').first.get_attribute('data-session-id')
        office.locator(f'.office-desk[data-session-id="{target}"]').click();wait_open(page);page.get_by_role('button',name='Back to Office',exact=True).click()
        expect(office.locator(f'.office-desk[data-session-id="{target}"]')).to_be_visible()
        record('Crowded floor pagination preserves the exact terminal and return page.')
        page.get_by_role('group',name='Agent view').get_by_role('button',name='List',exact=True).click()
        expect(page.locator('.workspace-session-group').first).to_be_visible()
        page.reload();expect(page.get_by_role('group',name='Agent view').get_by_role('button',name='List',exact=True)).to_have_attribute('aria-pressed','true')
        record('List remains available and the view choice survives reload.')
        ctx.close()
        ctx=browser.new_context(viewport={'width':390,'height':844},screen={'width':390,'height':844},is_mobile=True,has_touch=True)
        page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)));fx=OfficeFixture(dist);fx.install(page,screen=(390,844));page.goto(ORIGIN)
        page.get_by_role('group',name='Agent view').get_by_role('button',name='Office',exact=True).tap();office=page.get_by_role('region',name='Agent office');expect(office).to_be_visible()
        expect(office.locator('.office-floor:visible')).to_have_count(1)
        office.get_by_role('group',name='Office workspace').get_by_role('button',name='Work 2',exact=False).tap()
        expect(office.locator('.office-floor:visible')).to_have_attribute('data-env','work2')
        assert office.locator('.office-floor.chosen .office-desk').evaluate_all('(els)=>els.every(e=>{const r=e.getBoundingClientRect();return r.width>=44&&r.height>=44})')
        box=page.get_by_role('button',name='Pause motion',exact=True).bounding_box();assert box['width']>=44 and box['height']>=44
        page.screenshot(path=str(out/'phone-office.png'))
        record('Phone shows one workspace floor, with named 44px motion and desk controls.')
        def back():page.get_by_role('button',name='Back to Office',exact=True).tap();expect(office).to_be_visible()
        office.locator('.office-desk[data-session-id="work2-1"]').tap();wait_open(page);page.locator('.term-compose-field').fill('Work 2 draft');back()
        office.get_by_role('group',name='Office workspace').get_by_role('button',name='Work 1',exact=False).tap()
        office.locator('.office-desk[data-session-id="work1-1"]').tap();wait_open(page);expect(page.locator('.term-compose-field')).to_have_value('');page.locator('.term-compose-field').fill('Work 1 draft');back()
        office.get_by_role('group',name='Office workspace').get_by_role('button',name='Work 2',exact=False).tap()
        office.locator('.office-agent-row[data-session-id="work2-1"]').tap();wait_open(page);expect(page.locator('.term-compose-field')).to_have_value('Work 2 draft');back()
        assert not any(fx.frames(s['id']) for s in fx.state['sessions'])
        record('Phone desk/list navigation preserves distinct same-name session drafts without any terminal input frame.')
        # Ended rows remain readable through the existing panel and disappear from live desks.
        office.locator('.office-desk[data-session-id="work2-1"]').tap();wait_open(page)
        next(s for s in fx.state['sessions'] if s['id']=='work2-1').update(status='ended',state='exited',tmux=None,capabilities={'terminal':False,'prompt':False,'approve':False,'interrupt':False,'conversation':True,'resume':True,'end':False})
        fx.update();expect(page.locator('.saved-transcript')).to_be_visible();back()
        expect(office.locator('.office-desk[data-session-id="work2-1"]')).to_have_count(0)
        fx.state['sessions']=[s for s in fx.state['sessions'] if s['env']!='work2'];fx.update()
        expect(office.locator('.office-floor.chosen .office-scene-overlay')).to_contain_text('No live sessions in Work 2')
        record('An ended open terminal retains its existing transcript; ended desks disappear and empty floors show New session.')
        office.locator('.office-floor.chosen .office-scene-overlay').get_by_role('button',name='New session').tap()
        expect(page.get_by_label('Workspace',exact=True)).to_have_value('work2')
        page.locator('#new-name').fill('kept-draft')
        page.clock.install()
        reads=fx.state_reads
        page.clock.fast_forward(16000)
        page.wait_for_timeout(100)
        assert fx.state_reads==reads,'Office background refresh must pause while New session is open'
        expect(page.locator('#new-name')).to_have_value('kept-draft')
        page.keyboard.press('Escape')
        record('New session uses its Work 2 floor, and Office background refresh cannot reset an open form.')
        page.get_by_role('group',name='Agent view').get_by_role('button',name='List',exact=True).tap()
        page.get_by_role('group',name='Agent view').get_by_role('button',name='Office',exact=True).tap()
        page.reload();expect(page.get_by_role('region',name='Agent office')).to_be_visible()
        record('Office view choice survives a reload.')
        assert not errors,errors
        browser.close()
    (out/'office-results.json').write_text(json.dumps({'passed':True,'checks':results,'errors':errors},indent=2)+'\n')
    print(json.dumps({'passed':True,'checks':len(results)},indent=2))

if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--dist',type=Path,required=True);a.add_argument('--out',type=Path,required=True);args=a.parse_args();run(args.dist,args.out)
