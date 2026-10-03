"""Owned localhost UI QA fixture. No controller/curl/paid API or user history.

Run explicitly (not unittest discovery): python -m tests.ui_fixture_server
All synthetic rows are labelled fixture. This is never a production fallback.
"""
import argparse
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


def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8964)
    parser.add_argument('--desktop-preferences',action='store_true',
                        help='Simulate the WebView preference path in a browser; NOT native GUI acceptance')
    parser.add_argument('--release-status',choices=('ok','timeout'),default='ok',
                        help='Synthetic release metadata, never queries GitHub')
    parser.add_argument('--config-root-fixture',action='store_true',help='Print a synthetic local layout for root-selection UI QA')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='speedbench-ui-fixture-') as folder:
        web.DATA_HOME=Path(folder)
        web.HISTORY=Path(folder)/'fixture.jsonl'
        web.CANCEL_FILE=Path(folder)/'cancel-request'
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
                result.update(status=args.release_status, latest='v1.0.1' if args.release_status=='ok' else None,
                              comparison='ahead' if args.release_status=='ok' else None)
                return result
        web.RELEASE_CHECKER=FixtureReleaseChecker()
        source='subscription_v2_'+'a'*32
        nodes=[dict(node_id='node_v2_'+str(i)*32,runtime_name=name,proto='ss',identity_strength='strong',
                    source_status='verified',subscription_ids=[source],subscription_name='界面验证 fixture <订阅>')
               for i,name in enumerate(['fixture 长名称节点 <香港> & "仅用于界面验证" 🇭🇰','fixture 美国节点','fixture 失败节点'],1)]
        web.get_catalog=lambda:dict(version=2,status='ok',sources=[
            dict(subscription_id=source,name='界面验证 fixture <订阅>',loaded=True),
            dict(subscription_id='subscription_v2_'+'b'*32,name='fixture 未加载订阅',loaded=False)],nodes=nodes)
        web.get_current=lambda:dict(ok=True,now=nodes[0]['runtime_name'],group='fixture 策略组')
        web.connect_controller=lambda *a,**k:None
        web.LEAK_BASIC_LOOKUP=lambda ip:None
        web.switch_node=lambda name:dict(ok=True,now=name,group='fixture 策略组')
        def run(params):
            job=params['_job_id']
            try:
                for phase in ['preparing','probing','measuring','enriching','finalizing']:
                    if web.STATE.get('cancel_requested'):
                        web.JOBS.transition(job,'cancelling');web.JOBS.transition(job,'cancelled');return
                    web.JOBS.transition(job,phase)
                    if phase=='probing':
                        for i,n in enumerate(nodes):
                            row=dict(n,name=n['runtime_name'],latency_ms=12+i*20 if i<2 else None,
                                     median_mbps=None,status='fixture',measurement_scope={'bandwidth':'pending'},
                                     exit_status={'ipv4':'pending','ipv6':'pending'})
                            web.JOBS.publish(job,'node_probe',phase=phase,node_id=n['node_id'],
                                             payload=dict(result=row,completed=i+1,total=3))
                    elif phase=='measuring' and params.get('mode')!='ip':
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
                return super()._send(code,body,ctype)
            def do_GET(self):
                if args.desktop_preferences and self.path=='/static/fixture-environment.js':
                    if not self._check_host():return
                    return self._send(200,b"Object.defineProperty(window,'SPEEDBENCH_ENV',{value:Object.freeze({client:'webview'}),writable:false});",
                                      'application/javascript; charset=utf-8')
                return super().do_GET()
        server=FixtureServer(('127.0.0.1',args.port),FixtureHandler)
        print('Isolated UI fixture http://127.0.0.1:'+str(server.server_port),flush=True)
        try: server.serve_forever()
        except KeyboardInterrupt: pass
        finally: server.server_close()


if __name__=='__main__':
    main()
