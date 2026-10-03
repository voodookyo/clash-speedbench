"""Versioned child-to-parent progress records, independent of console wording."""
import hashlib
import json
import os
import re
import sys
import threading
from speedbench_jobs import EVENT_TYPES, PHASES, MAX_PAYLOAD, _result

PREFIX = '@speedbench-event '


class ProgressEmitter:
    def __init__(self, job_id, stream=None):
        if not re.fullmatch(r'job_[0-9a-f]{32}',job_id):
            raise ValueError('Invalid task identity')
        self.job_id = job_id
        self.stream = stream if stream is not None else sys.stdout
        self.lock = threading.RLock()
        self.seq = 0

    @classmethod
    def from_environment(cls,environ=None):
        value = (os.environ if environ is None else environ).get('SPEEDBENCH_JOB_ID','')
        return cls(value) if re.fullmatch(r'job_[0-9a-f]{32}',value) else None

    def emit(self,event_type,phase_name='',node_id='',payload=None):
        if event_type not in EVENT_TYPES or phase_name not in PHASES:
            raise ValueError('Invalid progress event')
        payload = {} if payload is None else payload
        safe = {k:payload[k] for k in ('completed','total') if isinstance(payload.get(k),int)}
        if 'result' in payload:
            safe['result'] = _result(payload['result'])
        with self.lock:
            self.seq += 1
            record = dict(version=1,job_id=self.job_id,source_seq=self.seq,type=event_type,
                          phase=phase_name,node_id=node_id,payload=safe)
            self.stream.write(PREFIX+json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
            self.stream.flush()


def parse_record(line,job_id):
    if not isinstance(line,str) or not line.startswith(PREFIX) or len(line)>MAX_PAYLOAD*2:
        return None
    try:
        value = json.loads(line[len(PREFIX):])
        if (not isinstance(value,dict) or value.get('version')!=1 or value.get('job_id')!=job_id
                or value.get('type') not in EVENT_TYPES or value.get('phase') not in PHASES
                or not isinstance(value.get('source_seq'),int) or isinstance(value['source_seq'],bool)
                or value['source_seq']<1 or not isinstance(value.get('node_id'),str)
                or len(value['node_id'])>128 or not isinstance(value.get('payload'),dict)):
            return None
        return value
    except (ValueError,TypeError):
        return None


def phase(args,name):
    emitter = getattr(args,'progress',None)
    if emitter is not None:
        emitter.emit('phase_started',name)


def publish_result(args,event_type,result,*,phase_name='',completed=None,total=None):
    emitter = getattr(args,'progress',None)
    if emitter is None or result is None:
        return
    # A fallback identifier joins partial records within this job only. It is
    # deliberately NOT a verified node_id and cannot authorize switching.
    identity = (getattr(result,'origin',None) or {}).get('node_id')
    if not identity:
        value = json.dumps([result.proto,result.name],ensure_ascii=False).encode('utf-8')
        identity = 'legacy_'+hashlib.sha256(value).hexdigest()[:32]
    from clash_speedbench import result_to_dict
    payload = {'result':result_to_dict(result)}
    if completed is not None: payload['completed'] = completed
    if total is not None: payload['total'] = total
    emitter.emit(event_type,phase_name,identity,payload)
