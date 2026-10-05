"""Owned localhost UI QA fixture. No controller/curl/paid API or user history.

Run explicitly (not unittest discovery): python -m tests.ui_fixture_server
All synthetic rows are labelled fixture. This is never a production fallback.
"""
import argparse
import faulthandler
import copy
import json
from contextlib import ExitStack
from datetime import datetime, timedelta
import sys
import tempfile
import threading
import time
from pathlib import Path
from http.server import ThreadingHTTPServer
import speedbench_web as web
from speedbench_tasks import resolve_config


class FixtureServer(ThreadingHTTPServer):
    # Wait for in-flight handlers to close fixture SQLite handles before the
    # enclosing TemporaryDirectory deletes its files (especially on Windows).
    daemon_threads=False
    block_on_close=True


def seed_history(path, nodes, source):
    """Synthetic raw input; all projections remain production DB/API code."""
    other='subscription_v2_'+'c'*32
    records=[]
    for index in range(3):
        when=datetime.now().astimezone()-timedelta(days=3-index)
        ts=when.isoformat(timespec='seconds');stamp=int(when.timestamp()*1000)
        name=nodes[0]['runtime_name'] if index==2 else 'fixture 旧名 <香港> " 🇭🇰'
        label='界面验证 fixture <订阅>' if index==2 else 'fixture 旧订阅名'
        address='203.0.113.10' if index<2 else '192.0.2.20'
        quality=90 if index!=1 else 20
        row=dict(nodes[0],identity_version=2,name=name,subscription_name=label,
            subscriptions=[dict(subscription_id=source,name=label,kind='subscription')],
            latency_ms=12+index,jitter_ms=2,median_mbps=50+index*10,score=65,network_score=65,
            status='ok',probe_attempts=3,probe_successes=3,probe_loss_pct=0,
            measurement_scope=dict(mode='quick',probe='completed',bandwidth='completed',intel='completed'),
            metric_updated_at=dict(network=stamp,ip_grade=stamp+10),measured_metric_count=6,
            exit_ipv4=address,exit_status=dict(ipv4='completed',ipv6='failed'),
            ip=dict(ok=True,exit_ip=address,country='fixture 美国',country_code='US',kind='fixture ISP'),
            ip_quality_score=quality,ip_grade='S' if quality==90 else 'D',
            intel_v4=dict(ip=address,ip_version=4,classification=dict(category='residential',confidence=90),
                ip_quality_score=quality,ip_grade='S' if quality==90 else 'D',ipqs_fraud_score=100-quality,
                provider_status={'fixture':'ok'},evidence=['fixture synthetic only']))
        ambiguous=dict(nodes[1],identity_version=2,name=nodes[1]['runtime_name'],source_status='ambiguous',
            subscription_ids=[source,other],subscription_name='',
            subscriptions=[dict(subscription_id=source,name=label,kind='subscription'),
                dict(subscription_id=other,name='fixture 第二来源',kind='subscription')],
            latency_ms=40,median_mbps=None,status='ok',probe_attempts=3,probe_successes=2,
            measurement_scope=dict(mode='ip',probe='completed',bandwidth='not_requested',intel='not_requested'),
            intel_v4={'provider_status':{'ipqs':'key_missing'}})
        unknown=dict(nodes[2],identity_version=2,name=nodes[2]['runtime_name'],source_status='unknown',
            subscription_ids=[],subscription_name='',subscriptions=[],status='timeout',latency_ms=None,
            probe_attempts=3,probe_successes=0,measurement_scope=dict(probe='failed',bandwidth='not_selected',intel='not_requested'))
        legacy=dict(name='fixture 旧格式 未知来源',provider='',proto='ss',latency_ms=50,status='ok')
        records.append(dict(ts=ts,mb=10,rounds=1,task=dict(mode='quick',target_profile='daily'),
                            results=[row,ambiguous,unknown,legacy]))
        if index:
            records[-1]['task'].update(job_id='job_'+str(index)*32,selected_node_count=4 if index==1 else 5,
                partial=index==2,status='completed' if index==1 else 'cancelled')
    path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records),encoding='utf-8')
    web.speedbench_db.import_jsonl(path.with_suffix('.db'),path)
    for index,record in enumerate(records[1:],1):
        task=record['task']
        web.speedbench_db.save_task(path.with_suffix('.db'),dict(job_id=task['job_id'],status=task['status'],
            config={'mode':'quick','target_profile':'daily'},started_at=record['ts'],finished_at=record['ts'],
            elapsed_ms=2500 if index==1 else 1750,results=record['results'],metrics={
                'download':dict(duration_ms=1000,attempts=1,successes=1,bytes=10000000 if index==1 else 500000)}))


LEAK_FIXTURE_JS="""'use strict';
browserExitIp=async url=>url.includes('api6.')?'2001:db8::10':'192.0.2.10';
collectWebRTCCandidates=async()=>({candidates:[],collection_complete:false,
  policy_blocked:true,collection_error:'fixture_webrtc_unavailable'});
document.getElementById('leak-environment').textContent='fixture 合成传输：无外部 IP/STUN 请求；此结果仅验证界面，不能判断实际泄漏。';
""".encode('utf-8')


def main():
    faulthandler.dump_traceback_later(4)
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8964)
    parser.add_argument('--desktop-preferences',action='store_true',
                        help='Simulate the WebView preference path in a browser; NOT native GUI acceptance')
    parser.add_argument('--release-status',choices=('ok','timeout'),default='ok',
                        help='Synthetic release metadata, never queries GitHub')
    parser.add_argument('--config-root-fixture',action='store_true',help='Print a synthetic local layout for root-selection UI QA')
    parser.add_argument('--history-import-fixture',action='store_true',help='Own temporary data and print a synthetic history source for import UI QA')
    parser.add_argument('--history-fixture',action='store_true',help='Seed three synthetic raw history rounds in this owned temporary directory')
    parser.add_argument('--controller-state',choices=('connected','disconnected','empty','timeout'),default='connected')
    parser.add_argument('--task-outcome',choices=('mixed','failed','partial'),default='mixed')
    parser.add_argument('--leak-fixture',action='store_true',help='Replace only browser leak transports with synthetic inputs; never use external IP/STUN')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='speedbench-ui-fixture-') as folder, ExitStack() as stack:
        web.DATA_HOME=Path(folder)
        web.HISTORY=Path(folder)/'fixture.jsonl'
        if args.history_import_fixture:
            from speedbench_owner import BackendLease
            from tests.test_history_transfer import ledger
            web.DATA_OWNER=stack.enter_context(BackendLease(web.DATA_HOME))
            web.HISTORY=web.DATA_HOME/'speedbench-history.jsonl'
            old=web.DATA_HOME/'旧历史 空格';old.mkdir()
            ledger(old,'2026-10-01T01:00:00',whitespace=True)
            print('Synthetic history source: '+str(old),flush=True)
        web.CANCEL_FILE=Path(folder)/'cancel-request'
        # Never use inherited provider credentials even for fixture status.
        web._provider_config=lambda:web.speedbench_ip_intel.ProviderConfig(ip_api_enabled=False)
        from speedbench_config import RootChoice
        web.CONFIG_ROOT=RootChoice()
        if args.config_root_fixture:
            from tests.test_config_root import layout
            fixture_root=layout(folder)
            print('Synthetic configuration layout: '+str(fixture_root),flush=True)
        if args.desktop_preferences: web.DESKTOP_IDENTITY={'version':'1.1.0-alpha.1'}
        class FixtureReleaseChecker:
            def check(self,current):
                result=web.speedbench_releases.local_info(current)
                comparison=None
                if args.release_status=='ok' and result['current']!='unknown':
                    here,pre=web.speedbench_releases._version(current)
                    latest=(1,0,1)
                    comparison='update_available' if latest>here or (latest==here and pre) else 'ahead' if here>latest else 'current'
                result.update(status=args.release_status, latest='v1.0.1' if args.release_status=='ok' else None,
                              comparison=comparison)
                return result
        web.RELEASE_CHECKER=FixtureReleaseChecker()
        source='subscription_v2_'+'a'*32
        nodes=[dict(node_id='node_v2_'+str(i)*32,runtime_name=name,proto='ss',identity_strength='strong',
                    source_status='verified',subscription_ids=[source],subscription_name='界面验证 fixture <订阅>')
               for i,name in enumerate(['fixture 长名称节点 <香港> & "仅用于界面验证" 🇭🇰','fixture 美国节点','fixture 失败节点'],1)]
        if args.history_fixture:seed_history(web.HISTORY,nodes,source)
        web.get_catalog=lambda *a,**k:dict(version=2,status='controller_unavailable' if args.controller_state=='disconnected' else 'ok',sources=[
            dict(subscription_id=source,name='界面验证 fixture <订阅>',loaded=True),
            dict(subscription_id='subscription_v2_'+'b'*32,name='fixture 未加载订阅',loaded=False)],
            nodes=[] if args.controller_state in ('empty','disconnected') else nodes)
        class FixtureController:
            def __init__(self):
                self.lock=threading.Lock()
                self.proxies={n['runtime_name']:dict(type='Shadowsocks') for n in nodes}
                self.proxies['GLOBAL']=dict(type='Selector',all=['fixture 策略组'])
                self.proxies['fixture 策略组']=dict(type='Selector',all=[n['runtime_name'] for n in nodes],now=nodes[0]['runtime_name'])
            def get(self,path):
                if path!='/proxies':raise ValueError('Fixture only supports /proxies')
                with self.lock:return dict(proxies=copy.deepcopy(self.proxies))
            def select(self,group,name):
                with self.lock:
                    if name not in self.proxies[group]['all']:raise ValueError('Invalid fixture selection')
                    self.proxies[group]['now']=name
        controller=FixtureController()
        web.get_current=lambda:dict(ok=args.controller_state not in ('disconnected','timeout'),
            now='' if args.controller_state=='empty' else controller.get('/proxies')['proxies']['fixture 策略组']['now'],group='fixture 策略组')
        def connect(*a,**k):
            if args.controller_state in ('disconnected','timeout'):raise ConnectionError('fixture controller unavailable')
            return controller
        web.connect_controller=connect
        web.LEAK_BASIC_LOOKUP=lambda ip:None
        run_threads=[]
        def run(params):
            run_threads.append(threading.current_thread())
            job=params['_job_id']
            try:
                if args.controller_state in ('disconnected','timeout','empty'):
                    web.JOBS.transition(job,'preparing');web.JOBS.transition(job,'failed')
                    return
                for phase in ['preparing','probing','measuring','enriching','finalizing']:
                    if web.STATE.get('cancel_requested'):
                        web.JOBS.transition(job,'cancelling');web.JOBS.transition(job,'cancelled');return
                    web.JOBS.transition(job,phase)
                    if phase=='probing':
                        for i,n in enumerate(nodes):
                            failed=args.task_outcome=='failed' or i==2
                            row=dict(n,name=n['runtime_name'],latency_ms=None if failed else 12+i*20,
                                     median_mbps=None,status='fixture',measurement_scope={'bandwidth':'pending'},
                                     exit_status={'ipv4':'pending','ipv6':'pending'})
                            if args.history_fixture or args.task_outcome!='mixed':
                                row.update(status='timeout' if failed else 'ok',probe_attempts=3,probe_successes=0 if failed else 3,
                                    probe_loss_pct=100 if failed else 0,measured_metric_count=1 if failed else 2,
                                    measurement_scope=dict(probe='failed' if failed else 'completed',bandwidth='not_selected' if failed else 'pending',intel='not_requested'),
                                    metric_updated_at=dict(network=int(time.time()*1000)))
                            web.JOBS.publish(job,'node_probe',phase=phase,node_id=n['node_id'],
                                             payload=dict(result=row,completed=i+1,total=3))
                    elif phase=='measuring' and args.task_outcome=='partial':
                        web.JOBS.transition(job,'cancelling');web.JOBS.transition(job,'cancelled');return
                    elif phase=='measuring' and params.get('mode')!='ip' and args.task_outcome!='failed':
                        for i,n in enumerate(nodes[:2]):
                            web.JOBS.publish(job,'node_measurement',phase=phase,node_id=n['node_id'],
                                payload=dict(result=dict(node_id=n['node_id'],name=n['runtime_name'],
                                    median_mbps=80-i*30,network_score=65,download_bytes=500000,
                                    measurement_scope={'bandwidth':'completed'},exit_status={'ipv4':'completed','ipv6':'failed'}),completed=i+1,total=2))
                        web.JOBS.publish(job,'phase_finished',payload={'metrics':{'download':dict(duration_ms=100,attempts=2,successes=2,bytes=1000000)}})
                    web.speedbench_db.save_task(web.db_path(),web.JOBS.snapshot(job))
                    time.sleep(.75)
                web.JOBS.transition(job,'completed')
            finally:
                web.speedbench_db.save_task(web.db_path(),web.JOBS.snapshot(job))
                with web.STATE_LOCK: web.STATE.update(running=False,proc=None,exit_code=0)
        web.run_benchmark=run
        class FixtureHandler(web.Handler):
            def _send(self,code,body,ctype):
                if args.desktop_preferences and ctype.startswith('text/html'):
                    body=body.replace(b'<script src="/static/tasks.js">',
                                      b'<script src="/static/fixture-environment.js"></script><script src="/static/tasks.js">')
                if args.leak_fixture and ctype.startswith('text/html'):
                    body=body.replace(b'<script src="/static/app.js"></script>',
                                      b'<script src="/static/app.js"></script><script src="/static/fixture-leak.js"></script>')
                    body=body.replace(b'<section id="view-leak" style="display:none">',
                                      '<section id="view-leak" style="display:none"><p class="card-sub" role="note">fixture 合成传输：无外部 IP/STUN 请求；不能判断实际泄漏。</p>'.encode('utf-8'))
                return super()._send(code,body,ctype)
            def do_GET(self):
                if self.path=='/api/catalog' and args.controller_state=='timeout':
                    if not self._check_host():return
                    return self._json({'ok':False,'msg':'fixture 目录超时，请确认 Verge 已运行后刷新订阅'},504)
                if self.path=='/static/fixture-leak.js' and args.leak_fixture:
                    if not self._check_host():return
                    return self._send(200,LEAK_FIXTURE_JS,'application/javascript; charset=utf-8')
                if args.desktop_preferences and self.path=='/static/fixture-environment.js':
                    if not self._check_host():return
                    return self._send(200,b"Object.defineProperty(window,'SPEEDBENCH_ENV',{value:Object.freeze({client:'webview'}),writable:false});",
                                      'application/javascript; charset=utf-8')
                return super().do_GET()
        server=FixtureServer(('127.0.0.1',args.port),FixtureHandler)
        faulthandler.cancel_dump_traceback_later()
        print('Isolated UI fixture http://127.0.0.1:'+str(server.server_port),flush=True)
        try: server.serve_forever()
        except KeyboardInterrupt: pass
        finally:
            server.server_close()
            # Owned synthetic run threads may still save their final snapshot.
            # Wait before deleting their database directory.
            for thread in run_threads:thread.join()


if __name__=='__main__':
    main()
