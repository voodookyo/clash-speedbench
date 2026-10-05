"""Backend-private child transport, temporary data and mocked controller only."""
import io
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest import mock

import speedbench_web as web
from speedbench_owner import BackendLease


class WebChildOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.home=Path(self.folder.name)
        self.stack=__import__('contextlib').ExitStack();self.addCleanup(self.stack.close)
        self.lease=self.stack.enter_context(BackendLease(self.home))
        for name,value in [('DATA_OWNER',self.lease),('DATA_HOME',self.home),
                           ('HISTORY',self.home/'h.jsonl'),('CANCEL_FILE',self.home/'cancel'),
                           ('STATE',dict(running=False,lines=[],started=0,exit_code=None,proc=None,
                                         cancel_requested=False,cleanup_incomplete=False))]:
            self.stack.enter_context(mock.patch.object(web,name,value))
        self.stack.enter_context(mock.patch.object(web,'connect_controller'))
        self.stack.enter_context(mock.patch.object(web,'sync_db'))

    def process(self):
        process=mock.Mock();process.stdout=[];process.wait.return_value=0
        process.stdin=mock.Mock();process.stdin.buffer=io.BytesIO()
        return process

    def test_delegation_is_only_in_private_stdin_and_pipe_closes_after_exit(self):
        process=self.process()
        with mock.patch.dict(os.environ,{'SPEEDBENCH_HOME':'relative-fixture'}), \
                mock.patch.object(web.subprocess,'Popen',return_value=process) as popen:
            web.run_benchmark({})
        arguments=popen.call_args.args[0];environment=popen.call_args.kwargs['env']
        self.assertIn('--backend-child',arguments)
        self.assertEqual(popen.call_args.kwargs['stdin'],subprocess.PIPE)
        self.assertEqual(environment['SPEEDBENCH_HOME'],str(self.home.resolve()))
        self.assertEqual(process.stdin.buffer.getvalue(),self.lease.delegation(web.HISTORY))
        for public in (arguments,environment,web.STATE):
            self.assertNotIn(self.lease.instance_id,str(public))
        process.stdin.close.assert_called_once()
        self.assertFalse(web.HISTORY.exists());self.assertFalse(web.STATE['running'])

    def test_broken_private_pipe_is_closed_and_child_reaped_before_idle(self):
        process=self.process();process.stdin.buffer=mock.Mock()
        process.stdin.buffer.write.side_effect=BrokenPipeError('fixture transport closed')
        with mock.patch.object(web.subprocess,'Popen',return_value=process):web.run_benchmark({})
        process.stdin.close.assert_called()
        process.wait.assert_called_once_with(timeout=8)
        self.assertIsNone(web.STATE['proc']);self.assertFalse(web.STATE['running'])
        self.assertEqual(web.STATE['exit_code'],-1)

    def test_transport_cleanup_timeout_retains_owned_handle_and_blocks_new_jobs(self):
        process=self.process();process.stdin.buffer=mock.Mock()
        process.stdin.buffer.write.side_effect=BrokenPipeError('fixture')
        process.wait.side_effect=subprocess.TimeoutExpired('fixture',8)
        # Capture the watcher rather than leave a real fixture thread alive.
        with mock.patch.object(web.subprocess,'Popen',return_value=process), \
                mock.patch.object(web.threading,'Thread') as thread:
            web.run_benchmark({})
        self.assertIs(web.STATE['proc'],process);self.assertTrue(web.STATE['running'])
        self.assertTrue(web.STATE['cleanup_incomplete'])
        process.terminate.assert_not_called();process.kill.assert_not_called()
        process.wait.side_effect=None
        thread.call_args.kwargs['target']()
        self.assertFalse(web.STATE['running']);self.assertTrue(web.STATE['cleanup_incomplete'])

    def test_closed_parent_lease_never_spawns_a_child(self):
        self.lease.close()
        with mock.patch.object(web.subprocess,'Popen') as popen:web.run_benchmark({})
        popen.assert_not_called();self.assertFalse(web.STATE['running'])
        self.assertFalse(web.HISTORY.exists())
