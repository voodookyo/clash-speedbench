"""Bounded in-process job state and event protocol (no persistent ghost locks).

This protocol layer is independent of subprocess/HTTP lifecycle. It is not
yet an SSE transport or a cancellation mechanism; those must share this state
when connected to the benchmark runner, rather than infer progress from logs.
"""
import json
import math
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timezone
from speedbench_tasks import TaskConfig

TERMINAL = ('completed','cancelled','failed')
TRANSITIONS = {
    'queued':('preparing','cancelling','failed'),
    'preparing':('probing','cancelling','failed'),
    'probing':('measuring','enriching','finalizing','cancelling','failed'),
    'measuring':('enriching','finalizing','cancelling','failed'),
    'enriching':('measuring','finalizing','cancelling','failed'),
    'finalizing':('completed','cancelling','failed'),
    'cancelling':('cancelled','failed'),
}
EVENT_TYPES = ('job_started','phase_started','phase_finished','node_probe','node_exit',
               'node_measurement','node_intelligence','job_finished','job_cancelled','job_failed')
PHASES = ('','preparing','probing','measuring','enriching','finalizing','cleanup')
RESULT_SCALARS = ('name','provider','node_id','proto','latency_ms','jitter_ms','connect_ms',
                  'median_mbps','multi_mbps','network_score','score','ip_quality_score','ip_grade',
                  'probe_attempts','probe_successes','probe_failures','probe_success_rate','probe_loss_pct',
                  'exit_ipv4','exit_ipv6','source_status','subscription_name','sample_mb','best_mbps',
                  'status','fail_reason','tags','node_key','identity_version','identity_strength',
                  'dual_stack_inconsistent','stars','download_bytes')
SCOPE_FIELDS = ('mode','probe','bandwidth','exit','intel')
MAX_PAYLOAD = 65536
MAX_RESULTS = 10000
METRIC_PHASES = ('connection','discovery','dns','worker_start','delay','exit_v4','exit_v6',
                 'basic_intel','provider','warmup','download','summary','restore','cleanup','probe')


def safe_metrics(value):
    public = {}
    if not isinstance(value,dict):
        return public
    for phase,metric in value.items():
        if phase not in METRIC_PHASES or not isinstance(metric,dict):
            continue
        def count(key):
            v = metric.get(key,0)
            return v if isinstance(v,int) and not isinstance(v,bool) and 0<=v<2**63 else 0
        d = metric.get('duration_ms',0)
        d = d if isinstance(d,(int,float)) and not isinstance(d,bool) and 0<=d<=1e15 and math.isfinite(d) else 0
        public[phase] = dict(duration_ms=d,attempts=count('attempts'),
                            successes=min(count('successes'),count('attempts')),bytes=count('bytes'))
    return public
IP_FIELDS = ('exit_ip','country','country_code','region','city','isp','org','asn','asname','kind',
             'proxy','hosting','mobile','ok')
INTEL_FIELDS = ('ip','ip_version','country','asn','as_name','isp','organization','hosting','proxy',
                'vpn','tor','mobile','residential_proxy','connection_type','ipqs_fraud_score',
                'ipqs_recent_abuse','ipqs_abuse_velocity','scamalytics_score','scamalytics_risk',
                'scamalytics_datacenter','scamalytics_blacklisted','ip_quality_score','ip_grade')
PROVIDER_FIELDS = INTEL_FIELDS + ('country_code','as_type','company_type','asn_type','isp_type',
                                 'fraud_score','recent_abuse','abuse_velocity','bot_status',
                                 'datacenter','blacklisted','risk','score','public_proxy',
                                 'web_proxy','server','residential','org','asname')


def _scalars(value, fields):
    if not isinstance(value,dict):
        return {}
    return {k:value[k] for k in fields if k in value and
            (value[k] is None or isinstance(value[k],(str,int,float,bool)))}


class JobError(ValueError):
    pass


def _copy(value):
    try:
        encoded = json.dumps(value,ensure_ascii=False,allow_nan=False)
        if len(encoded.encode('utf-8')) > MAX_PAYLOAD:
            raise JobError('Event payload exceeds limits')
        return json.loads(encoded)
    except (TypeError,ValueError,OverflowError):
        raise JobError('Event payload is unsupported') from None


def _result(value):
    if not isinstance(value,dict):
        raise JobError('Node event result is unsupported')
    public = _scalars(value,RESULT_SCALARS)
    scope = value.get('measurement_scope')
    if isinstance(scope,dict):
        public['measurement_scope'] = {k:scope[k] for k in SCOPE_FIELDS if isinstance(scope.get(k),str)}
    if isinstance(value.get('exit_status'),dict):
        public['exit_status'] = {k:v for k,v in value['exit_status'].items()
            if k in ('ipv4','ipv6','basic') and v in ('pending','completed','failed','not_requested')}
    if isinstance(value.get('samples_mbps'),list):
        public['samples_mbps'] = [v for v in value['samples_mbps'][:5] if isinstance(v,(int,float))]
    if isinstance(value.get('ip'),dict):
        public['ip'] = _scalars(value['ip'],IP_FIELDS)
    for key in ('intel_v4','intel_v6'):
        intel = value.get(key)
        if intel is None and key in value:
            public[key] = None
        elif isinstance(intel,dict):
            normalized = _scalars(intel,INTEL_FIELDS)
            classification = intel.get('classification')
            if isinstance(classification,dict):
                c = _scalars(classification,('category','confidence'))
                for field in ('evidence','conflicts'):
                    if isinstance(classification.get(field),list):
                        c[field] = [s[:2000] for s in classification[field][:100] if isinstance(s,str)]
                normalized['classification'] = c
            statuses = intel.get('provider_status',{})
            normalized['provider_status'] = _scalars(statuses,('ip-api','ipinfo','ipqs','scamalytics'))
            data = intel.get('provider_data',{})
            if isinstance(data,dict):
                normalized['provider_data'] = {p:_scalars(data[p],PROVIDER_FIELDS)
                    for p in ('ip-api','ipinfo','ipqs','scamalytics') if isinstance(data.get(p),dict)}
            public[key] = normalized
    # Imported lazily to keep the protocol usable without importing measurement
    # engines; nested subscription metadata has its own explicit whitelist.
    from speedbench_sources import result_origin
    public.update(result_origin(value))
    return _copy(public)


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


class JobStore:
    def __init__(self, event_limit=512, job_limit=16):
        if not 1 <= event_limit <= 4096 or not 1 <= job_limit <= 128:
            raise JobError('Job retention limits are unsupported')
        self.event_limit, self.job_limit = event_limit, job_limit
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.jobs = {}
        self.active = None

    def _job(self, job_id):
        try:
            return self.jobs[job_id]
        except (KeyError,TypeError):
            raise JobError('Job unavailable; refresh task state') from None

    def active_id(self):
        with self.lock:
            return self.active

    def latest(self):
        with self.lock:
            key = self.active or next(reversed(self.jobs),None)
            return self._snapshot(self.jobs[key]) if key else None

    def wait(self,job_id,since_seq,timeout=2):
        with self.changed:
            job = self._job(job_id)
            if job['seq'] == since_seq and job['status'] not in TERMINAL:
                self.changed.wait(timeout=max(0,min(timeout,20)))
            return self.read(job_id,since_seq)

    def create(self, config):
        if not isinstance(config,TaskConfig):
            raise JobError('Job requires a validated task configuration')
        with self.lock:
            if self.active is not None:
                raise JobError('A task already owns the runner')
            while len(self.jobs) >= self.job_limit:
                oldest = next((key for key,j in self.jobs.items() if j['status'] in TERMINAL),None)
                if oldest is None:
                    raise JobError('Task retention is full')
                del self.jobs[oldest]
            job_id = 'job_'+secrets.token_hex(16)
            self.jobs[job_id] = dict(job_id=job_id,status='queued',config=config.public(),
                started_at=_timestamp(),finished_at=None,seq=0,partial=False,
                results={},results_truncated=False,metrics={},elapsed_ms=0,
                _started_clock=time.monotonic(),events=deque(maxlen=self.event_limit))
            self.active = job_id
            self._append(self.jobs[job_id],'job_started','', '', {})
            return job_id

    def _append(self, job, event_type, phase, node_id, payload):
        job['seq'] += 1
        event = dict(version=1,job_id=job['job_id'],seq=job['seq'],timestamp=_timestamp(),
                     type=event_type,phase=phase,node_id=node_id,payload=payload)
        job['events'].append(event)
        self.changed.notify_all()

    def transition(self, job_id, status):
        with self.lock:
            job = self._job(job_id)
            if job['status'] in TERMINAL or status == job['status']:
                return False
            if status not in TRANSITIONS.get(job['status'],()):
                raise JobError('Invalid job transition')
            job['status'] = status
            if status in TERMINAL:
                job['finished_at'] = _timestamp()
                job['elapsed_ms'] = max(0,time.monotonic()-job['_started_clock'])*1000
                job['partial'] = status != 'completed'
                if self.active == job_id:
                    self.active = None
                event_type = dict(completed='job_finished',cancelled='job_cancelled',failed='job_failed')[status]
            else:
                event_type = 'phase_started'
            self._append(job,event_type,status if status in PHASES else '', '', {'status':status})
            return True

    def publish(self, job_id, event_type, *, phase='', node_id='', payload=None):
        if event_type not in EVENT_TYPES or event_type.startswith('job_'):
            raise JobError('Unsupported event type')
        if phase not in PHASES or not isinstance(node_id,str) or len(node_id)>128:
            raise JobError('Unsupported event phase or node identity')
        payload = {} if payload is None else payload
        if not isinstance(payload,dict):
            raise JobError('Unsupported event payload')
        public = {k:payload[k] for k in ('completed','total') if isinstance(payload.get(k),int)
                  and not isinstance(payload[k],bool) and 0 <= payload[k] <= MAX_RESULTS}
        if 'result' in payload:
            public['result'] = _result(payload['result'])
        if event_type=='phase_finished' and 'metrics' in payload:
            public['metrics'] = safe_metrics(payload['metrics'])
        with self.lock:
            job = self._job(job_id)
            if job['status'] in TERMINAL:
                return False
            for phase,metric in public.get('metrics',{}).items():
                target = job['metrics'].setdefault(phase,dict(duration_ms=0,attempts=0,successes=0,bytes=0))
                for key,value in metric.items():
                    target[key] += value
            if node_id and 'result' in public:
                if node_id in job['results'] or len(job['results']) < MAX_RESULTS:
                    job['results'].setdefault(node_id,{}).update(public['result'])
                else:
                    job['results_truncated'] = True
            self._append(job,event_type,phase,node_id,public)
            return True

    def _snapshot(self, job):
        # Snapshot can be larger than an individual bounded event. It still
        # has a fixed MAX_RESULTS cap and only contains whitelisted records.
        public = {k:v for k,v in job.items() if k not in ('results','events','_started_clock')}
        if job['status'] not in TERMINAL:
            public['elapsed_ms'] = max(0,time.monotonic()-job['_started_clock'])*1000
        public['version'] = 1
        public['results'] = list(job['results'].values())
        return json.loads(json.dumps(public,ensure_ascii=False,allow_nan=False))

    def snapshot(self, job_id):
        with self.lock:
            return self._snapshot(self._job(job_id))

    def read(self, job_id, since_seq=0):
        if not isinstance(since_seq,int) or isinstance(since_seq,bool) or since_seq < 0:
            raise JobError('Invalid event cursor')
        with self.lock:
            job = self._job(job_id)
            oldest = job['events'][0]['seq'] if job['events'] else job['seq']+1
            resync = since_seq < oldest-1 or since_seq > job['seq']
            result = dict(version=1,job_id=job_id,seq=job['seq'],resync=resync,
                          events=[] if resync else list(e for e in job['events'] if e['seq']>since_seq))
            if resync:
                result['snapshot'] = self._snapshot(job)
            return json.loads(json.dumps(result,ensure_ascii=False,allow_nan=False))


class PhaseTimer:
    """Monotonic phase durations and integer actual counters, not estimates."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.RLock()
        self.started = {}
        self.metrics = {}

    def start(self, phase):
        if phase not in ('connection','discovery','dns','worker_start','delay','exit_v4','exit_v6',
                         'basic_intel','provider','warmup','download','summary','restore','cleanup','probe'):
            raise JobError('Unsupported timing phase')
        with self.lock:
            if phase in self.started:
                raise JobError('Timing phase already active')
            self.started[phase] = self.clock()

    def finish(self, phase, *, attempts=0, successes=0, bytes=0):
        counters = dict(attempts=attempts,successes=successes,bytes=bytes)
        if any(not isinstance(v,int) or isinstance(v,bool) or v<0 for v in counters.values()) or successes>attempts:
            raise JobError('Timing counters must be actual nonnegative integers')
        with self.lock:
            if phase not in self.started:
                raise JobError('Timing phase is not active')
            elapsed = max(0,self.clock()-self.started.pop(phase)) * 1000
            metric = self.metrics.setdefault(phase,dict(duration_ms=0,attempts=0,successes=0,bytes=0))
            metric['duration_ms'] += elapsed
            for key,value in counters.items():
                metric[key] += value

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.metrics,allow_nan=False))
