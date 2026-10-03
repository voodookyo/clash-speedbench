import io
import json
import unittest
from types import SimpleNamespace
import clash_speedbench as core
from speedbench_progress import ProgressEmitter, parse_record, publish_result, phase


class ProgressStreamTest(unittest.TestCase):
    def test_closed_progress_transport_does_not_abort_cleanup_or_retention(self):
        from speedbench_progress import ResultJournal,measure
        stream=io.StringIO();stream.close()
        emitter=ProgressEmitter('job_'+'a'*32,stream)
        journal=ResultJournal();args=SimpleNamespace(progress=emitter,_result_journal=journal)
        row=core.Result(name='safe',provider='',proto='ss',latency_ms=50,
                       speeds_mbps=[],median_mbps=None,best_mbps=None,status='ok')
        with measure(args,'cleanup'):publish_result(args,'node_probe',row)
        self.assertTrue(emitter.transport_failed);self.assertEqual(len(journal.snapshot()),1)

    def test_structured_record_can_be_read_without_parsing_human_logs(self):
        stream = io.StringIO()
        emitter = ProgressEmitter('job_'+'a'*32,stream)
        args = SimpleNamespace(progress=emitter)
        row = core.Result(name='节点',provider='',proto='ss',latency_ms=50,
                          speeds_mbps=[],median_mbps=None,best_mbps=None,status='ok')
        publish_result(args,'node_probe',row,phase_name='probing')
        record = parse_record(stream.getvalue(),'job_'+'a'*32)
        self.assertEqual(record['type'],'node_probe')
        self.assertEqual(record['payload']['result']['latency_ms'],50)
        self.assertTrue(record['node_id'].startswith('legacy_'))
        self.assertIsNone(parse_record('Phase 1 粗筛 [1/5]','job_'+'a'*32))

    def test_record_for_another_job_is_ignored(self):
        stream = io.StringIO()
        phase(SimpleNamespace(progress=ProgressEmitter('job_'+'a'*32,stream)),'probing')
        self.assertIsNone(parse_record(stream.getvalue(),'job_'+'b'*32))

    def test_env_validation_and_no_default_cli_output(self):
        self.assertIsNone(ProgressEmitter.from_environment({}))
        self.assertIsNone(ProgressEmitter.from_environment({'SPEEDBENCH_JOB_ID':'invalid'}))
        self.assertIsNotNone(ProgressEmitter.from_environment({'SPEEDBENCH_JOB_ID':'job_'+'a'*32}))
        publish_result(SimpleNamespace(),'node_probe',None)

    def test_result_whitelist_omits_connection_and_raw_secrets(self):
        stream = io.StringIO()
        emitter = ProgressEmitter('job_'+'a'*32,stream)
        emitter.emit('node_probe','probing','node',{'result':{'name':'safe','raw':'CANARY',
                     'password':'CANARY','ip':{'exit_ip':'1.1.1.1','api_key':'CANARY'}}})
        self.assertNotIn('CANARY',stream.getvalue())

    def test_seq_is_validated_for_duplicate_or_out_of_order_handling(self):
        stream = io.StringIO()
        emitter = ProgressEmitter('job_'+'a'*32,stream)
        emitter.emit('phase_started','probing')
        emitter.emit('phase_started','measuring')
        records = [parse_record(line,'job_'+'a'*32) for line in stream.getvalue().splitlines()]
        self.assertEqual([r['source_seq'] for r in records],[1,2])
