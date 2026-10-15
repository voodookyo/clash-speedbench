import io
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack,redirect_stdout,redirect_stderr
from pathlib import Path
from unittest import mock
import clash_speedbench as core
import speedbench_workers as workers


class SerialConsentTest(unittest.TestCase):
    def test_unconfirmed_web_serial_never_connects_or_changes_global(self):
        with mock.patch.dict(os.environ,{},clear=True), \
                mock.patch.object(sys,'argv',['clash_speedbench.py','--workers','1','--yes','--deny-serial-fallback']), \
                mock.patch.object(core,'connect_controller') as connect,redirect_stderr(io.StringIO()):
            self.assertEqual(core.main(),2)
        connect.assert_not_called()

    def test_worker_unavailable_stops_before_any_global_mutation(self):
        api=mock.Mock()
        api.get.side_effect=lambda path:{
            '/version':{'version':'fixture'},'/configs':{'mode':'rule','mixed-port':7897},
            '/proxies':{'proxies':{'GLOBAL':{'type':'Selector','all':['a'],'now':'a'},'a':{'type':'ss'}}}}[path]
        with tempfile.TemporaryDirectory() as folder,ExitStack() as patches:
            patches.enter_context(mock.patch.dict(os.environ,{},clear=True))
            patches.enter_context(mock.patch.object(sys,'argv',['clash_speedbench.py','--workers','2','--yes','--deny-serial-fallback','--history',str(Path(folder)/'history.jsonl'),'--no-history']))
            patches.enter_context(mock.patch.object(core,'connect_controller',return_value=api))
            patches.enter_context(mock.patch.object(core.source_catalog,'discover_catalog',return_value={'nodes':[],'sources':[]}))
            patches.enter_context(mock.patch.object(workers,'run_pool',side_effect=workers.WorkerUnavailable('fixture unavailable')))
            patches.enter_context(redirect_stdout(io.StringIO()));patches.enter_context(redirect_stderr(io.StringIO()))
            self.assertEqual(core.main(),1)
        api.patch.assert_not_called();api.select.assert_not_called()


if __name__=='__main__':unittest.main()
