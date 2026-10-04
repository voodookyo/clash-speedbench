import copy
import csv
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import clash_speedbench as core
from tests.test_resume_js import NODE
from speedbench_jobs import JobStore
from speedbench_tasks import resolve_config


def result(name, speed, score, **fields):
    return core.Result(name,'fixture','ss',80,[],speed,speed,'ok',score=score,**fields)


class TargetRecommendationTest(unittest.TestCase):
    def test_download_winner_follows_bandwidth_and_keeps_raw_overall(self):
        rows=[result('overall',20,95),result('download',180,30)]
        self.assertEqual(core.rank_results(rows)[0].name,'overall')
        self.assertEqual(core.rank_results(rows,target_profile='download')[0].name,'download')
        api=mock.Mock();proxies={'Main':{'type':'Selector','now':'overall','all':['overall','download']}}
        core.auto_switch_best(api,proxies,{'Main':['overall','download']},'GLOBAL',rows,
                              target_profile='download')
        api.select.assert_called_once_with('Main','download')
        self.assertEqual([r.score for r in rows],[95,30])

    def test_ip_mode_cannot_supply_a_download_winner(self):
        row=result('probe-only',None,85,ip=core.IpInfo(ok=True))
        api=mock.Mock()
        core.auto_switch_best(api,{'Main':{'type':'Selector'}},{'Main':['probe-only']},'GLOBAL',
                              [row],target_profile='download')
        api.select.assert_not_called()
        store=JobStore();job=store.create(resolve_config({'mode':'ip','target_profile':'download'}))
        store.publish(job,'node_intelligence',payload={'result':{'name':'fixture','ip_quality_score':85,'ip_grade':'A'}})
        self.assertNotIn('first_recommendation',store.snapshot(job)['milestones'])

    def test_ip_auto_switch_uses_only_observed_quality_not_legacy_neutral_flags(self):
        unknown=result('unknown',90,95,ip=core.IpInfo(ok=True))
        intel=mock.Mock();intel.to_dict.return_value={'ip_quality_score':50,'ip_grade':'C',
            'classification':{'category':'corporate','confidence':90}}
        known=result('observed',50,40,ip_quality_score=50,ip_grade='C',intel_v4=intel)
        api=mock.Mock();proxies={'Main':{'type':'Selector','all':['unknown','observed']}}
        core.auto_switch_best(api,proxies,{'Main':['unknown','observed']},'GLOBAL',[unknown,known],target_profile='ip')
        api.select.assert_called_once_with('Main','observed')

    def test_target_export_order_keeps_metrics_and_default_csv_compatible(self):
        rows=[result('overall',20,95),result('download',180,30)]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'result.csv'
            core.write_csv(rows,path)
            with path.open(encoding='utf-8-sig') as stream:legacy=list(csv.DictReader(stream))
            core.write_csv(rows,path,target_profile='download')
            with path.open(encoding='utf-8-sig') as stream:target=list(csv.DictReader(stream))
            self.assertNotIn('target_score',legacy[0]);self.assertEqual(legacy[0]['name'],'overall')
            self.assertEqual(target[0]['name'],'download');self.assertEqual(float(target[0]['score']),30)
            self.assertEqual(target[0]['target_profile'],'download')
            history=Path(folder)/'history.jsonl'
            core.append_history(rows,history,10,1,path,task={'target_profile':'download'})
            record=json.loads(history.read_text())
            self.assertEqual(record['results'][0]['name'],'download')
            self.assertEqual(record['results'][0]['median_mbps'],180)

    def test_new_network_mode_uses_coverage_before_target_score(self):
        measured=result('measured',10,25,measurement_scope={'mode':'quick','bandwidth':'completed'})
        pending=result('unmeasured',None,100,measurement_scope={'mode':'quick','bandwidth':'not_selected'})
        self.assertEqual(core.rank_results([pending,measured],target_profile='daily')[0],measured)

    def test_completed_and_retained_partial_bandwidth_rank_above_failed_download(self):
        completed=result('completed',10,20,measurement_scope={'mode':'deep','bandwidth':'completed'})
        partial=result('partial',100,30,measurement_scope={'mode':'deep','bandwidth':'partial'})
        failed=result('failed',None,90,measurement_scope={'mode':'deep','bandwidth':'failed'})
        self.assertEqual([r.name for r in core.rank_results([failed,partial,completed])],
                         ['completed','partial','failed'])

    def test_automatic_switch_rejects_changed_identity_and_invalid_fresh_group(self):
        identity='node_v2_'+'a'*32
        winner=result('download',180,30,origin={'node_id':identity,'identity_strength':'strong'})
        for nodes,group in (([],{'type':'Selector','all':['download']}),
                           ([{'node_id':identity,'runtime_name':'download','identity_strength':'strong'}],
                            {'type':'URLTest','all':['download']})):
            api=mock.Mock();api.get.return_value={'proxies':{'Main':group,'download':{'type':'ss'}}}
            with mock.patch.object(core.source_catalog,'discover_catalog',return_value={'nodes':nodes}):
                core.auto_switch_best(api,{}, {},'GLOBAL',[winner],group_override='Main',
                    target_profile='download',data_home='fixture')
            api.select.assert_not_called()

    def test_automatic_switch_tracks_verified_rename_without_rewriting_result(self):
        identity='node_v2_'+'a'*32
        winner=result('old-name',180,30,origin={'node_id':identity,'identity_strength':'strong'})
        api=mock.Mock();api.get.return_value={'proxies':{'Main':{'type':'Selector','all':['new-name']},
                                                       'new-name':{'type':'ss'}}}
        nodes=[{'node_id':identity,'runtime_name':'new-name','identity_strength':'strong'}]
        with mock.patch.object(core.source_catalog,'discover_catalog',return_value={'nodes':nodes}):
            core.auto_switch_best(api,{}, {},'GLOBAL',[winner],target_profile='download',data_home='fixture')
        api.select.assert_called_once_with('Main','new-name');self.assertEqual(winner.name,'old-name')


@unittest.skipUnless(NODE,'Node unavailable')
class ProfileParityTest(unittest.TestCase):
    def test_python_and_shared_js_score_and_rank_same_normalized_rows(self):
        import speedbench_profiles as profiles
        def intel(category,quality,**extra):
            return dict(classification=dict(category=category,confidence=90),ip_quality_score=quality,**extra)
        rows=[{},dict(ip={'ok':False}),dict(ip={'ok':True,'kind':'住宅','hosting':False}),
              dict(intel_v4={},ip={'ok':True}),dict(ip_intel={},ip={'ok':True,'hosting':False}),
              dict(ip={'ok':True,'proxy':True}),
              dict(latency_ms=80,jitter_ms=10,median_mbps=150,multi_mbps=250,score=55),
              dict(latency_ms=440,jitter_ms=None,median_mbps=50,score=99),
              dict(intel_v4=intel('residential',92),intel_v6=intel('datacenter',45,hosting=True)),
              dict(intel_v4=intel('residential',90,ipqs_fraud_score=96,recent_abuse=True)),
              dict(intel_v4={'classification':{'category':'unknown','confidence':0},'ip_grade':'S'}),
              dict(intel_v4={'classification':{'category':'corporate','confidence':85},
                            'ipqs':{'fraud_score':10},'scamalytics':{'blacklisted':True}})]
        for i,row in enumerate(rows):row.update(name=str(i),node_id='node_v2_'+format(i,'032x'))
        rows[-1]['measurement_scope']={'mode':'quick','bandwidth':'completed'}
        for name,state,speed in [('partial','partial',50),('failed','failed',None),('complete','completed',10),
                                 ('\ue000','not_selected',5),('📦','not_selected',5)]:
            rows.append(dict(name=name,score=45,latency_ms=100,median_mbps=speed,
                             measurement_scope={'mode':'quick','bandwidth':state}))
        module=Path(__file__).resolve().parents[1]/'web/profiles.js'
        script='const P=require('+json.dumps(str(module))+');const rows='+json.dumps(rows)+';'
        script+='console.log(JSON.stringify(Object.fromEntries(["balanced","daily","download","ip","residential"].map(p=>[p,{scores:rows.map(r=>P.score(r,p)),order:[...rows].sort((a,b)=>P.compare(a,b,p)).map(r=>r.name)}]))));'
        run=subprocess.run([NODE,'-e',script],capture_output=True,text=True,timeout=10)
        self.assertEqual(run.returncode,0,run.stderr);got=json.loads(run.stdout)
        before=copy.deepcopy(rows)
        for profile,actual in got.items():
            with self.subTest(profile=profile):
                expected=[profiles.score(row,profile) for row in rows]
                for a,b in zip(actual['scores'],expected):
                    if b is None:self.assertIsNone(a)
                    else:self.assertAlmostEqual(a,b,places=6)
                self.assertEqual(actual['order'],[r['name'] for r in profiles.rank(rows,profile)])
        self.assertEqual(rows,before)
