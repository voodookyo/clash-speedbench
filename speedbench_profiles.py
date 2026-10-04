"""Target recommendation on normalized results; never changes measurements.

Formulas match web/profiles.js. Legacy Overall remains the balanced score.
Runtime needs only the standard library; Node is used solely for parity tests.
"""
import math
import re

GRADES={'S':95,'A':85,'B':70,'C':50,'D':20}
CATEGORIES={'residential':7,'corporate':6,'mobile':5.5,'residential_proxy':5,
            'datacenter':3.5,'vpn_proxy':2,'unknown':0}
ALIASES={'住宅':'ISP/非托管','住宅IP':'ISP/非托管','机房':'机房托管','移动':'移动网络'}


def _field(source,names):
    if not isinstance(source,dict):return None
    for key in names:
        for obj in (source,*(source.get(k) for k in ('ipqs','scamalytics','ipinfo','privacy'))):
            if isinstance(obj,dict) and obj.get(key) is not None and obj.get(key)!='':return obj[key]
    return None


def _number(source,names):
    value=_field(source,names)
    if value is None:return None
    try:value=float(value.strip() or '0') if isinstance(value,str) else float(value)
    except (ValueError,TypeError,OverflowError):return None
    return value if math.isfinite(value) else None


def _boolean(source,names):
    if not isinstance(source,dict):return None
    saw_false=False
    for key in names:
        for obj in (source,*(source.get(k) for k in ('ipqs','scamalytics','ipinfo','privacy'))):
            value=obj.get(key) if isinstance(obj,dict) else None
            if isinstance(value,bool):parsed=value
            elif isinstance(value,(int,float)):parsed=value!=0
            elif isinstance(value,str) and value.lower() in ('true','false'):parsed=value.lower()=='true'
            else:continue
            if parsed:return True
            saw_false=True
    return False if saw_false else None


def _sources(row):
    sources=[]
    for value in (row.get('ip_intel'),row.get('intel'),row.get('intel_v4'),row.get('intel_v6'),
                  (row.get('ip') or {}).get('intel')):
        if isinstance(value,dict) and value and all(value is not old for old in sources):sources.append(value)
    if not sources and isinstance(row.get('ip'),dict) and row['ip']:sources.append(row['ip'])
    return sources


def _aggregate(row):
    for source in (row.get('ip_intel'),row.get('intel'),(row.get('ip') or {}).get('intel')):
        if isinstance(source,dict) and source:return source
    family=row.get('intel_v4') or row.get('intel_v6')
    if family:
        return dict(family,ip_quality_score=row.get('ip_quality_score') if row.get('ip_quality_score') is not None
                    else family.get('ip_quality_score'),ip_grade=row.get('ip_grade') or family.get('ip_grade'))
    return row.get('ip') or {}


def _root_field(row,source,names):
    for obj in (source,_aggregate(row),row):
        value=_field(obj,names)
        if value is not None:return value
    return None


def _root_number(row,source,names):
    for obj in (source,_aggregate(row),row):
        value=_number(obj,names)
        if value is not None:return value
    return None


def _category(source):
    cls=source.get('classification')
    return str((cls.get('category') if isinstance(cls,dict) else cls) or source.get('category') or 'unknown').lower()


def _clamp(value,lo=0,hi=100):return max(lo,min(hi,value))


def _risk(row,source):
    value=100;observed=False
    quality=_root_number(row,source,('ip_quality_score','quality_score'))
    grade=GRADES.get(str(_root_field(row,source,('ip_grade','grade')) or '').upper())
    fraud=_number(source,('ipqs_fraud_score','fraud_score'))
    scam=_number(source,('scamalytics_score','scamalytics_fraud_score'))
    for number in (quality,grade,None if fraud is None else 100-_clamp(fraud),
                   None if scam is None else 100-_clamp(scam)):
        if number is not None:value=min(value,_clamp(number));observed=True
    flags=((('proxy','is_proxy'),25),(('vpn','is_vpn'),20),(('tor','is_tor'),10),
           (('hosting','is_hosting'),45),(('mobile','is_mobile'),75),
           (('residential_proxy','is_residential_proxy','is_res_proxy'),55),
           (('ipqs_recent_abuse','recent_abuse'),30),
           (('scamalytics_blacklisted','blacklisted','is_blacklisted_external'),20),
           (('scamalytics_datacenter','datacenter','is_datacenter'),45))
    for names,cap in flags:
        flag=_boolean(source,names)
        if flag is not None:observed=True
        if flag is True:value=min(value,cap)
    connection=_field(source,('connection_type',))
    if connection is not None:
        observed=True
        if re.search(r'data.?center|hosting|server',str(connection),re.I):value=min(value,45)
        elif re.search(r'mobile|cellular',str(connection),re.I):value=min(value,75)
    risk=_field(source,('scamalytics_risk','risk'))
    if risk is not None:
        observed=True
        if re.search(r'very.?high|high|danger|severe',str(risk),re.I):value=min(value,35)
    classification=source.get('classification')
    classification=classification if isinstance(classification,dict) else {}
    confidence=_number(source,('confidence',))
    if confidence is None:confidence=_number(classification,('confidence',))
    if confidence is not None:value=min(value,_clamp(confidence));observed=True
    if _category(source)=='unknown' or classification.get('conflicts'):value=min(value,35)
    return math.floor(_clamp(value)*10+.5)/10 if observed else 20


def _residential(row,source):
    cls=_category(source);clean=_risk(row,source)
    category=source.get('classification')
    confidence=_number(source,('confidence',))
    if confidence is None:confidence=_number(category,('confidence',))
    fraud=_number(source,('ipqs_fraud_score','fraud_score'))
    scam=_number(source,('scamalytics_score','scamalytics_fraud_score'))
    high_risk=(cls!='unknown' and clean<40 or fraud is not None and fraud>=75 or
               scam is not None and scam>=75 or _boolean(source,('proxy','vpn','tor','ipqs_recent_abuse',
                 'recent_abuse','scamalytics_blacklisted','blacklisted')) is True)
    if high_risk:return 100+clean
    rank=CATEGORIES.get(cls,0)
    if cls=='residential':rank=7 if confidence is not None and confidence>=80 else 5.8
    return rank*100+clean


def score(row,profile='balanced'):
    if profile in ('daily','download'):
        latency=_number(row,('latency_ms',));bw=_number(row,('median_mbps',))
        if profile=='daily':
            if latency is None:return None
            jitter=_number(row,('jitter_ms',))
            return .5*100*_clamp((800-latency)/720,0,1)+.3*(50 if jitter is None else
                100*_clamp((200-jitter)/190,0,1))+.2*min(bw or 0,100)
        if bw is None:return None
        multi=_number(row,('multi_mbps',))
        return .7*min(bw,300)/300*100+.3*min(bw if multi is None else multi,500)/500*100
    if profile not in ('ip','ipclean','residential'):return _number(row,('score',))
    sources=_sources(row)
    if not sources:return None
    # An empty normalized object is still structured evidence, not permission
    # to fall back to the more optimistic legacy ip-api classification.
    structured=any(isinstance(row.get(k),dict) for k in ('ip_intel','intel','intel_v4','intel_v6')) or isinstance((row.get('ip') or {}).get('intel'),dict)
    if structured:
        return min((_residential if profile=='residential' else _risk)(row,source) for source in sources)
    ip=row.get('ip') or {}
    if not ip.get('ok'):return None
    if profile!='residential':return 20 if ip.get('proxy') else 50 if ip.get('hosting') else 75 if ip.get('mobile') else 60
    if ip.get('proxy'):return 120
    if ip.get('hosting'):return 395
    if ip.get('mobile'):return 625
    kind=ALIASES.get(ip.get('kind'),ip.get('kind'))
    return {'ISP/非托管':500,'机房托管':395,'代理/VPN':220}.get(kind,0)


def coverage(row,profile='balanced'):
    scope=row.get('measurement_scope') or {}
    if profile in ('balanced','all','daily','download') and scope.get('mode') in ('quick','standard','deep'):
        state=scope.get('bandwidth')
        if state=='partial':return 2 if _number(row,('median_mbps',)) is not None else 0
        return {'completed':3,'failed':1}.get(state,0)
    return 0


def rank(rows,profile='balanced'):
    def key(row):
        value=score(row,profile);latency=_number(row,('latency_ms',))
        # Old unscoped ties retain input order. New tasks use stable identity.
        stable=(str(row.get('node_id') or ''),str(row.get('name') or '')) if (row.get('measurement_scope') or {}).get('mode') else ('','')
        return (-coverage(row,profile),value is None,-(value or 0),float('inf') if latency is None else latency,*stable)
    return sorted(rows,key=key)
