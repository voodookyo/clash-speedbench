"""Production serial/worker accounting; fake requests, temporary data only."""
import contextlib
import io
import json
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

import clash_speedbench as core
import speedbench_workers as workers
import speedbench_progress as progress
from speedbench_jobs import JobStore
from speedbench_tasks import resolve_config
from tests import test_cli_partial_history as fixtures


class MeasurementAccountingTest(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.CliPartialHistoryTest()
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.store=JobStore();self.job=self.store.create(resolve_config())
        self.stream=io.StringIO();self.emitter=progress.ProgressEmitter(self.job,self.stream)

    def metrics(self):
        for line in self.stream.getvalue().splitlines():
            event=progress.parse_record(line,self.job)
            if event:self.store.publish(self.job,event['type'],node_id=event['node_id'],payload=event['payload'])
        return self.store.snapshot(self.job)['metrics']

    def serial(self,samples,extra=(),csv_error=False):
        with mock.patch.object(core.ProgressEmitter,'from_environment',return_value=self.emitter), \
                mock.patch.object(core,'probe_latency',return_value=core.ProbeStats(20,0,3,2,1)), \
                mock.patch.object(core,'apply_path'),mock.patch.object(core,'restore_groups'), \
                mock.patch.object(core,'curl_speed',side_effect=samples),mock.patch.object(core.time,'sleep'):
            return self.fixture.invoke(None,serial=True,extra=extra,csv_error=csv_error)

    def test_serial_fixed_samples_have_probe_download_restore_and_whole_summary_spans(self):
        code,_,_=self.serial([(8.0,'ok',2.0,.5),(None,'http-500',None,.125)],['--mb','10','--rounds','2'])
        self.assertEqual(code,0);metrics=self.metrics()
        self.assertEqual(metrics['delay']['attempts'],3);self.assertEqual(metrics['delay']['successes'],2)
        self.assertEqual(metrics['download']['attempts'],2);self.assertEqual(metrics['download']['successes'],1)
        self.assertEqual(metrics['download']['bytes'],625000)
        self.assertIn('restore',metrics);self.assertIn('summary',metrics)
        self.assertEqual(self.fixture.records()[0]['results'][0]['download_bytes'],625000)

    @staticmethod
    def sample(proxy_url=None,download_url=None,*args,**options):
        url=download_url or args[0]
        size=.125 if 'warmup-' in url else .25 if 'multi-' in url else .5
        return 8.0,'ok',2.0,size

    def test_serial_adaptive_and_multi_count_all_reported_samples_not_budgets(self):
        code,_,_=self.serial(self.sample,['--multi'])
        self.assertEqual(code,0);metrics=self.metrics()
        self.assertEqual(metrics['warmup']['attempts'],1)
        self.assertEqual(metrics['warmup']['bytes'],125000)
        self.assertEqual(metrics['download']['attempts'],5)
        self.assertEqual(metrics['download']['bytes'],1500000)
        self.assertEqual(self.fixture.records()[0]['results'][0]['download_bytes'],1625000)

    def test_serial_interrupted_command_counts_attempt_not_success_or_guessed_bytes(self):
        code,_,_=self.serial([(8.0,'ok',2.0,.5),KeyboardInterrupt()],['--mb','10','--rounds','2'])
        self.assertEqual(code,130);metrics=self.metrics()
        self.assertEqual(metrics['download']['attempts'],2);self.assertEqual(metrics['download']['successes'],1)
        self.assertEqual(metrics['download']['bytes'],500000)
        self.assertIn('restore',metrics);self.assertIn('summary',metrics)

    def test_failed_summary_still_emits_duration_and_does_not_mask_status(self):
        # Older Windows monotonic clocks can legitimately return the same
        # tick for fast fixture exports. Control only this module's clock,
        # not JobStore/ownership clocks or the measurement implementation.
        ticks=iter(range(1000))
        with mock.patch.object(progress,'time',SimpleNamespace(monotonic=lambda:next(ticks)*.1)):
            code,_,_=self.serial([(8.0,'ok',2.0,.5)],['--mb','10'],csv_error=True)
        self.assertEqual(code,1);self.assertGreater(self.metrics()['summary']['duration_ms'],0)

    def worker(self,*,mb=10,rounds=1,multi=False,samples=None,emitter=True):
        result=fixtures.row()
        args=SimpleNamespace(settle=0,mb=mb,max_time=3,rounds=rounds,multi=multi,
            _result_journal=progress.ResultJournal(),progress=self.emitter if emitter else None)
        worker=SimpleNamespace(select=lambda name:None,proxy_url='http://fixture.invalid')
        # Both modules reference the same real sampling functions, but worker
        # single-stream calls its imported curl while warmup/multi call core.
        with mock.patch.object(workers,'cancel_requested',return_value=False), \
                mock.patch.object(workers,'curl_speed',side_effect=samples or self.sample), \
                mock.patch.object(core,'curl_speed',side_effect=samples or self.sample):
            try:workers._speed_node_in_worker(worker,result,args)
            except KeyboardInterrupt:return result,args,130
        return result,args,0

    def test_worker_without_progress_still_counts_warmup_and_multi_bytes(self):
        result,args,code=self.worker(mb=None,multi=True,emitter=False)
        self.assertEqual(code,0);self.assertEqual(result.download_bytes,1625000)
        self.assertEqual(args._result_journal.snapshot()[0].download_bytes,1625000)

    def test_worker_cancelled_round_counts_attempt_and_preserves_sample(self):
        result,_,code=self.worker(rounds=2,samples=[(8.0,'ok',2.0,.5),KeyboardInterrupt()])
        self.assertEqual(code,130);metric=self.metrics()['download']
        self.assertEqual(metric['attempts'],2);self.assertEqual(metric['successes'],1)
        self.assertEqual(metric['bytes'],500000);self.assertEqual(result.download_bytes,500000)

    def test_worker_warmup_interruption_is_partial_not_unselected(self):
        result,args,code=self.worker(mb=None,samples=[KeyboardInterrupt()])
        self.assertEqual(code,130);metric=self.metrics()['warmup']
        self.assertEqual(metric['attempts'],1);self.assertEqual(metric['successes'],0)
        self.assertEqual(metric['bytes'],0)
        self.assertEqual(args._result_journal.snapshot()[0].measurement_scope['bandwidth'],'partial')

    def test_worker_fallback_probe_has_its_own_counts(self):
        args=SimpleNamespace(delay_timeout=5000,probe_count=3,no_ip=True,progress=self.emitter)
        worker=SimpleNamespace(api=mock.Mock())
        worker.api.proxy_delay.side_effect=[None,20,None]
        result=workers._probe_node_in_worker(worker,'A','ss',args)
        self.assertEqual(result.probe_failures,2)
        self.assertEqual(self.metrics()['probe']['attempts'],3)

    def test_main_pool_completed_probe_counts_survive_later_pool_cancellation(self):
        real_pool=workers.run_pool
        def pool(api,names,timeout,**options):
            options['on_result']('A',core.ProbeStats(20,0,3,2,1),1,1)
            raise KeyboardInterrupt()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(core.ProgressEmitter,'from_environment',return_value=self.emitter))
            for name,value in [('find_mihomo_bin','fixture'),('find_config_file','fixture'),
                               ('extract_proxies',[{'name':'A','type':'ss','server':'a.invalid'}]),
                               ('physical_interface','en0'),('build_hosts',{})]:
                stack.enter_context(mock.patch.object(workers,name,return_value=value))
            stack.enter_context(mock.patch.object(workers,'probe_latency_pool',side_effect=pool))
            code,_,_=self.fixture.invoke(lambda *a,**k:real_pool(*a,**k),extra=['--mode','quick'])
        self.assertEqual(code,130);metric=self.metrics()['delay']
        self.assertEqual(metric['attempts'],3);self.assertEqual(metric['successes'],2)

    def test_sample_counter_ignores_invalid_sizes_without_guessing_budget(self):
        result=fixtures.row();counts=dict(attempts=0,successes=0,bytes=0)
        counter=progress.DownloadCounter(SimpleNamespace(),result,counts)
        for size in (float('nan'),float('inf'),-1,None,True,'1'):
            counter.start();counter.finish(None,size)
        self.assertEqual(counts,dict(attempts=6,successes=0,bytes=0))

    def test_worker_partial_multistream_retains_completed_other_streams(self):
        started=threading.Barrier(4)
        def sample(proxy_url,download_url,*args,**kwargs):
            if 'multi-' in download_url:
                started.wait(timeout=2)
                if download_url.endswith('-0'):raise KeyboardInterrupt()
            return self.sample(proxy_url,download_url,*args,**kwargs)
        result,args,code=self.worker(multi=True,samples=sample)
        self.assertEqual(code,130);metric=self.metrics()['download']
        self.assertEqual(metric['attempts'],5);self.assertEqual(metric['successes'],4)
        self.assertEqual(metric['bytes'],1250000)
        self.assertEqual(args._result_journal.snapshot()[0].download_bytes,1250000)


if __name__=='__main__':unittest.main()
