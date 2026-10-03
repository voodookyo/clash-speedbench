"""Versioned child-to-parent progress records, independent of console wording."""
import hashlib
import copy
import json
import math
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from speedbench_jobs import EVENT_TYPES, PHASES, MAX_PAYLOAD, _result, safe_metrics, CHILD_MILESTONES

PREFIX = '@speedbench-event '


class ResultJournal:
    """Private per-run completed-result snapshots, independent of stdout.

    Runtime names are unique within one frozen Mihomo catalogue. This is not a
    cross-run identity or switching authority. It retains one row per name and
    never serializes arbitrary task configuration, exceptions or credentials.
    """
    def __init__(self):
        self.lock=threading.RLock()
        self.rows={}

    def remember(self,result):
        if result is None:return
        value=copy.deepcopy(result)
        with self.lock:
            previous=self.rows.get(value.name)
            sources=dict(getattr(previous,'probe_sources',None) or {},**(getattr(value,'probe_sources',None) or {}))
            if sources:
                value.probe_sources=sources
                # Final/exit rows must carry these independent observations
                # into reporting too, not just the private failure journal.
                result.probe_sources=copy.deepcopy(sources)
            if previous is not None and not value.probe_attempts:
                # Early exit-family events may omit the already completed
                # main probe stats. Do not lose those independent samples.
                for key in ('probe_attempts','probe_successes','probe_failures',
                            'probe_success_rate','probe_loss_pct'):
                    setattr(value,key,getattr(previous,key,None))
            self.rows[value.name]=value

    def snapshot(self):
        with self.lock:return copy.deepcopy([self.rows[k] for k in sorted(self.rows)])


def retain_result(args,result):
    journal=getattr(args,'_result_journal',None)
    if journal is not None:journal.remember(result)


class ProbeObserver:
    """Incremental completed samples; interrupted calls are not failed samples."""
    def __init__(self,args,name,proto,provider,source,requested,counts=None,on_update=None):
        self.args,self.name,self.proto,self.provider=args,name,proto,provider
        self.source,self.requested=source,requested
        self.counts,self.on_update=counts,on_update
        self.started=0;self.last=None;self.observed=False

    def start(self):
        self.started+=1
        if self.counts is not None:self.counts['attempts']=self.started
        if self.on_update is not None:self.on_update(self.name,self.last,self.started)

    def sample(self,stats,complete):
        self.last=stats;self.observed=True
        self.started=max(self.started,stats.attempts)
        if self.counts is not None:self.counts.update(attempts=self.started,successes=stats.successes)
        if self.on_update is not None:self.on_update(self.name,stats,self.started)
        if not self.started:return
        from clash_speedbench import Result,_apply_probe_stats
        from speedbench_sources import apply_origin
        row=Result(name=self.name,provider=self.provider,proto=self.proto,
            latency_ms=stats.latency_ms,jitter_ms=stats.jitter_ms,speeds_mbps=[],
            median_mbps=None,best_mbps=None,status='probe-only' if stats.successes else
                'unreachable' if complete else 'probe-pending')
        _apply_probe_stats(row,stats)
        row.probe_sources={self.source:dict(stats.to_dict(),started=self.started,
            requested=self.requested,status='completed' if complete else 'partial')}
        row.measurement_scope={'probe':'completed' if complete else 'partial'}
        apply_origin(row,getattr(self.args,'source_origins',{}).get(self.name))
        publish_result(self.args,'node_probe',row,phase_name='probing')


class DownloadCounter:
    """Observed request attempts and returned bytes, safe for same-node streams.

    Start before invoking curl; finish only when that invocation returns. An
    interrupted request is an attempt, but its unreported bytes stay unknown.
    This counter never substitutes the requested sample budget for traffic.
    """
    def __init__(self,args,result,counts):
        self.args,self.result,self.counts=args,result,counts
        self.lock=threading.Lock()

    def start(self):
        with self.lock:
            self.counts['attempts']+=1
            self.result.measurement_scope=dict(self.result.measurement_scope or {},bandwidth='partial')
            retain_result(self.args,self.result)

    def finish(self,speed,size):
        with self.lock:
            self.counts['successes']+=int(speed is not None)
            if isinstance(size,(int,float)) and not isinstance(size,bool) and math.isfinite(size) and size>=0:
                known=round(size*1_000_000)
                self.result.download_bytes=(self.result.download_bytes or 0)+known
                self.counts['bytes']+=known
            retain_result(self.args,self.result)


class ProgressEmitter:
    def __init__(self, job_id, stream=None):
        if not re.fullmatch(r'job_[0-9a-f]{32}',job_id):
            raise ValueError('Invalid task identity')
        self.job_id = job_id
        self.stream = stream if stream is not None else sys.stdout
        self.lock = threading.RLock()
        self.seq = 0
        self.transport_failed=False

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
        if event_type=='phase_finished' and 'metrics' in payload:
            safe['metrics'] = safe_metrics(payload['metrics'])
        if event_type=='milestone':
            if payload.get('milestone') not in CHILD_MILESTONES:raise ValueError('Invalid progress milestone')
            safe['milestone']=payload['milestone']
        with self.lock:
            if self.transport_failed:return
            self.seq += 1
            record = dict(version=1,job_id=self.job_id,source_seq=self.seq,type=event_type,
                          phase=phase_name,node_id=node_id,payload=safe)
            try:
                self.stream.write(PREFIX+json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
                self.stream.flush()
            except (OSError,ValueError):
                # Optional stdout transport must not interrupt owned cleanup
                # or prevent independent journal/history retention on EOF.
                self.transport_failed=True


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


def emit_metric(args,name,metric):
    emitter=getattr(args,'progress',None)
    if emitter is not None:emitter.emit('phase_finished',payload={'metrics':{name:metric}})


def milestone(args,name):
    emitter=getattr(args,'progress',None)
    if emitter is not None:emitter.emit('milestone',payload={'milestone':name})


@contextmanager
def measure(args,name):
    """Concurrent spans are accumulated service time, not additive wall time.

    Callers supply observed curl bytes; request budgets never count as traffic.
    If an in-flight command is forcibly interrupted its unreported bytes are
    unknown, not guessed. Parent elapsed_ms separately measures complete wait.
    """
    counts = dict(attempts=0,successes=0,bytes=0)
    started = time.monotonic()
    try:
        yield counts
    finally:
        emitter = getattr(args,'progress',None)
        if emitter is not None:
            counts['duration_ms'] = max(0,time.monotonic()-started)*1000
            emitter.emit('phase_finished',payload={'metrics':{name:counts}})


def publish_result(args,event_type,result,*,phase_name='',completed=None,total=None):
    retain_result(args,result)
    emitter = getattr(args,'progress',None)
    if emitter is None or result is None:
        return
    # A fallback identifier joins partial records within this job only. It is
    # deliberately NOT a verified node_id and cannot authorize switching.
    identity = (getattr(result,'origin',None) or {}).get('node_id')
    if not identity:
        # A frozen runtime catalogue has unique names. Controller/worker
        # protocol spelling (Shadowsocks vs ss) is not a second job identity.
        value = json.dumps([result.name],ensure_ascii=False).encode('utf-8')
        identity = 'legacy_'+hashlib.sha256(value).hexdigest()[:32]
    from clash_speedbench import result_to_dict
    payload = {'result':result_to_dict(result)}
    if completed is not None: payload['completed'] = completed
    if total is not None: payload['total'] = total
    emitter.emit(event_type,phase_name,identity,payload)
