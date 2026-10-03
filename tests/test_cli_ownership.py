import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import clash_speedbench as core
import speedbench_owner as owner


class CliOwnershipTest(unittest.TestCase):
    def test_active_backend_refuses_direct_cli_before_controller_or_history_write(self):
        with tempfile.TemporaryDirectory() as folder:
            history=Path(folder)/'h.jsonl';history.write_bytes(b'fixture original\n')
            with owner.BackendLease(folder),mock.patch.dict(os.environ,{},clear=True), \
                 mock.patch('sys.argv',['clash_speedbench.py','--yes','--history',str(history)]), \
                 mock.patch.object(core,'connect_controller',side_effect=core.ApiError('fixture')) as connect, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(core.main(),2);connect.assert_not_called()
            self.assertEqual(history.read_bytes(),b'fixture original\n')

    def test_cli_holds_lease_before_even_reading_controller_and_releases_on_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            def connect(*a,**k):
                with self.assertRaises(owner.LeaseError):owner.BackendLease(folder).acquire()
                raise core.ApiError('fixture')
            with mock.patch.dict(os.environ,{},clear=True), \
                 mock.patch('sys.argv',['clash_speedbench.py','--yes','--history',str(Path(folder)/'h.jsonl')]), \
                 mock.patch.object(core,'connect_controller',side_effect=connect),contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(core.main(),1)
            with owner.BackendLease(folder):pass

    def test_standalone_cli_coordinates_identity_home_and_explicit_history_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder)/'home';history=Path(folder)/'history'/'h.jsonl'
            with mock.patch.dict(os.environ,{'SPEEDBENCH_HOME':str(home)},clear=True), \
                 owner.benchmark_ownership(history):
                for directory in (home,history.parent):
                    with self.assertRaises(owner.LeaseError):owner.BackendLease(directory).acquire()
            for directory in (home,history.parent):
                with owner.BackendLease(directory):pass

    def test_fake_child_flag_frame_and_environment_cannot_bypass_ownership(self):
        with tempfile.TemporaryDirectory() as folder:
            history=Path(folder)/'h.jsonl'
            for data in (b'',b'{"key":"CANARY"}\n',b'x'*32769+b'\n'):
                with self.assertRaises(owner.LeaseError) as error:
                    with owner.benchmark_ownership(history,delegated=True,stream=io.BytesIO(data)):pass
                self.assertNotIn('CANARY',str(error.exception))

    def test_live_delegated_child_blocks_restart_after_parent_lease_release_and_observes_eof(self):
        program='''
import sys,time
import clash_speedbench as core
from speedbench_owner import parent_disconnected
def execute(args,config):
    print('ready',flush=True)
    deadline=time.monotonic()+5
    while not parent_disconnected() and time.monotonic()<deadline:time.sleep(.01)
    assert parent_disconnected()
    return 0
core._execute_benchmark=execute
sys.argv=['clash_speedbench.py','--yes','--backend-child','--history',sys.argv[1]]
assert core.main()==0
print('done',flush=True)
'''
        with tempfile.TemporaryDirectory() as folder:
            history=Path(folder)/'h.jsonl';lease=owner.BackendLease(folder).acquire();process=None
            try:
                process=subprocess.Popen([sys.executable,'-u','-c',program,str(history)],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                process.stdin.write(lease.delegation(history));process.stdin.flush()
                import queue,threading
                ready=queue.Queue();threading.Thread(target=lambda:ready.put(process.stdout.readline()),daemon=True).start()
                self.assertEqual(ready.get(timeout=5).strip(),b'ready')
                lease.close()
                with self.assertRaises(owner.LeaseError):owner.BackendLease(folder).acquire()
                process.stdin.close();process.stdin=None
                stdout,stderr=process.communicate(timeout=7)
                self.assertEqual(process.returncode,0,stderr);self.assertEqual(stdout.strip(),b'done')
                with owner.BackendLease(folder):pass
                self.assertFalse(history.exists())
            finally:
                lease.close()
                if process:
                    if process.poll() is None:process.kill();process.communicate(timeout=3)
                    for stream in (process.stdin,process.stdout,process.stderr):
                        if stream:stream.close()

    def test_benchmark_error_is_not_relabelled_as_an_ownership_error(self):
        with tempfile.TemporaryDirectory() as folder,mock.patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(ValueError,'fixture body'):
                with owner.benchmark_ownership(Path(folder)/'h.jsonl'):raise ValueError('fixture body')

    def test_parent_pid_instance_root_and_kernel_lease_are_all_required(self):
        with tempfile.TemporaryDirectory() as folder,mock.patch.dict(os.environ,{},clear=True):
            history=Path(folder)/'h.jsonl'
            with owner.BackendLease(folder) as lease:
                valid=json.loads(lease.delegation(history))
                wrong_frames=[dict(valid,parent_pid=os.getpid()+100000),
                              dict(valid,instance_id='0'*32),
                              dict(valid,history=str(history.with_name('different.jsonl'))),
                              dict(valid,protocol=True),dict(valid,extra='CANARY')]
                with mock.patch.object(owner.os,'getppid',return_value=os.getpid()):
                    for frame in wrong_frames:
                        with self.subTest(frame=frame),self.assertRaises(owner.LeaseError):
                            with owner.benchmark_ownership(history,delegated=True,
                                    stream=io.BytesIO(json.dumps(frame).encode()+b'\n')):pass
                # A valid-looking stale record alone never authorizes writes.
                stale=lease.delegation(history)
            with mock.patch.object(owner.os,'getppid',return_value=os.getpid()),self.assertRaises(owner.LeaseError):
                with owner.benchmark_ownership(history,delegated=True,stream=io.BytesIO(stale)):pass
            with owner.BackendLease(folder):pass
            self.assertFalse(history.exists())

    def test_completed_child_exits_cleanly_while_parent_pipe_is_still_open(self):
        program='''
import sys
import clash_speedbench as core
core._execute_benchmark=lambda args,config:0
sys.argv=['clash_speedbench.py','--yes','--backend-child','--history',sys.argv[1]]
raise SystemExit(core.main())
'''
        with tempfile.TemporaryDirectory() as folder,owner.BackendLease(folder) as lease:
            history=Path(folder)/'h.jsonl'
            process=subprocess.Popen([sys.executable,'-u','-c',program,str(history)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                process.stdin.write(lease.delegation(history));process.stdin.flush()
                # Do not communicate()/close stdin before wait: the parent
                # remains alive on normal completion, not just on EOF cancel.
                process.wait(timeout=7)
                process.stdin.close();process.stdin=None
                stdout,stderr=process.communicate(timeout=3)
                self.assertEqual(process.returncode,0,stderr)
                self.assertFalse(history.exists())
            finally:
                if process.poll() is None:process.kill();process.wait(timeout=3)
                for stream in (process.stdin,process.stdout,process.stderr):
                    if stream:stream.close()

    def test_intelligence_pool_is_closed_before_releasing_cli_lease_on_failure(self):
        with tempfile.TemporaryDirectory() as folder,mock.patch.dict(os.environ,{},clear=True):
            enricher=mock.Mock()
            def close():
                with self.assertRaises(owner.LeaseError):owner.BackendLease(folder).acquire()
            enricher.close.side_effect=close
            def execute(args,config):
                args._owned_intel_pools.append(enricher)
                raise ValueError('fixture body')
            with mock.patch('sys.argv',['clash_speedbench.py','--yes','--history',str(Path(folder)/'h.jsonl')]), \
                    mock.patch.object(core,'_execute_benchmark',side_effect=execute):
                with contextlib.redirect_stderr(io.StringIO()) as stderr:self.assertEqual(core.main(),1)
                self.assertNotIn('fixture body',stderr.getvalue())
            self.assertGreaterEqual(enricher.close.call_count,1)
            with owner.BackendLease(folder):pass

    def test_default_history_honors_home_and_hidden_flag_requires_private_pipe(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(os.environ,{'SPEEDBENCH_HOME':folder},clear=True), \
                mock.patch('sys.argv',['clash_speedbench.py','--yes']), \
                mock.patch.object(core,'_execute_benchmark',return_value=0) as execute:
            self.assertEqual(core.main(),0)
            self.assertEqual(Path(execute.call_args.args[0].history).resolve(),(Path(folder)/'speedbench-history.jsonl').resolve())
            with mock.patch('sys.argv',['clash_speedbench.py','--backend-child','--yes']), \
                    mock.patch.object(core.sys,'stdin',io.TextIOWrapper(io.BytesIO(b''))), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(core.main(),2)
            self.assertEqual(execute.call_count,1)

    def test_parent_disconnect_reaches_external_process_cancellation_callback(self):
        event=__import__('threading').Event();event.set()
        with mock.patch.object(owner,'_parent_event',event),mock.patch.object(core,'_CANCEL_FILE',''):
            self.assertTrue(core.cancel_requested())
            with mock.patch.object(core,'run_cancellable') as run:
                core.run_external(['fixture-curl'],capture_output=True,timeout=2)
                self.assertIs(run.call_args.kwargs['cancel'],core.cancel_requested)
