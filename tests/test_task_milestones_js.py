"""Shared state contract only; no DOM or browser dependency."""
import json
import subprocess
from pathlib import Path
import unittest
from tests.test_resume_js import NODE


@unittest.skipUnless(NODE,'Node unavailable')
class MilestoneStateJsTest(unittest.TestCase):
    def test_numeric_counters_and_first_milestones_merge_across_sse_updates(self):
        module=Path(__file__).resolve().parents[1]/'web/tasks.js'
        script="""
const T=require(process.argv[1]);
const s=new T.TaskState({version:1,job_id:'j',seq:1,status:'probing',results:[]});
for(const seq of [2,3])s.apply({version:1,job_id:'j',seq,type:'phase_finished',payload:{
 metrics:{provider:{attempts:1,successes:1,counters:{api_calls:1,api_key:'CANARY'}}},
 milestones:{first_result:seq*100,private_url:'CANARY'}}});
console.log(JSON.stringify(s.value));
"""
        result=subprocess.run([NODE,'-e',script,str(module)],capture_output=True,text=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr);value=json.loads(result.stdout)
        self.assertEqual(value['metrics']['provider']['counters']['api_calls'],2)
        self.assertEqual(value['milestones'],{'first_result':200})
        self.assertNotIn('CANARY',result.stdout)
