"""Evidence-based subscription catalogue. Credentials never leave this module.

``provider_names`` means Mihomo membership, not a verified airport/subscription.
Public dictionaries contain display names, opaque IDs and fixed status/evidence
strings only.  Unknown definitions must not silently match by display name.
"""
import json
import math
import os
from pathlib import Path
import re

from speedbench_identity import IdentityError, load_seed, opaque_id


MAX_BYTES = 8 * 1024 * 1024
MAX_ITEMS = 10000
MAX_DEPTH = 24


class SourceError(ValueError):
    pass


class SourceSelectionChanged(SourceError):
    """Selected configuration changed; abort rather than test a different ID."""


def _bounded(value, depth=0, budget=None):
    budget = [MAX_ITEMS * 20] if budget is None else budget
    budget[0] -= 1
    if depth > MAX_DEPTH or budget[0] < 0:
        raise SourceError('Source document exceeds structural limits')
    if isinstance(value, dict):
        if len(value) > MAX_ITEMS:
            raise SourceError('Source document exceeds collection limits')
        return {str(k): _bounded(v, depth + 1, budget) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > MAX_ITEMS:
            raise SourceError('Source document exceeds collection limits')
        return [_bounded(v, depth + 1, budget) for v in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise SourceError('Source document contains unsupported values')


def canonical_connection(proxy):
    """Conservative effective configuration, keeping auth and unknown options.

    No secret fingerprint is returned by public catalogue APIs. Only the name
    is excluded; routing/transport/auth/chain options
    are never removed merely to improve the apparent match rate.
    """
    if not isinstance(proxy, dict):
        raise SourceError('Node definition is unsupported')
    value = _bounded(proxy)
    value.pop('name', None)
    value['type'] = str(value.get('type') or '').lower()
    if value.get('server'):
        value['server'] = str(value['server']).lower()
    if 'port' in value:
        try:
            value['port'] = int(value['port'])
        except (TypeError, ValueError):
            raise SourceError('Node port is unsupported') from None
    return value


def _strong(proxy):
    return bool(proxy.get('server') and proxy.get('port') and proxy.get('type'))


def _effective_definition(proxy, by_name, seen=()):
    canonical = canonical_connection(proxy)
    dependency = canonical.get('dialer-proxy')
    if not dependency:
        return canonical
    if not isinstance(dependency,str) or dependency in seen or len(seen) >= MAX_DEPTH:
        raise SourceError('Connection dependency is cyclic or unsupported')
    parent = by_name.get(dependency)
    if not isinstance(parent,dict) or not _strong(parent):
        raise SourceError('Connection dependency is unresolved')
    # A reference name is mutable; actual upstream auth/transport must form
    # part of the identity and matching evidence instead.
    canonical['dialer-proxy'] = _effective_definition(parent,by_name,seen+(dependency,))
    return canonical


def build_catalog(runtime, profiles, *, seed, namespace, providers=None):
    runtime = _bounded(runtime)
    profiles = _bounded(profiles)
    if not isinstance(runtime, list) or not isinstance(profiles, list):
        raise SourceError('Source collections are unsupported')
    sources, index, seen = [], {}, set()
    for item in profiles:
        if not isinstance(item, dict) or item.get('type') not in ('remote', 'local') or not item.get('uid'):
            continue
        source_id = opaque_id(seed, 'subscription', [namespace, str(item['uid'])])
        if source_id in seen:
            raise SourceError('Duplicate subscription identity')
        seen.add(source_id)
        available = item.get('available', True) is not False
        source = dict(subscription_id=source_id, name=str(item.get('name') or '未命名订阅'),
                      kind=item['type'], available=available, loaded=False,
                      mapping_status='not_loaded' if available else 'unavailable')
        sources.append(source)
        if not available:
            continue
        definitions = item.get('proxies', [])
        by_name = {p.get('name'):p for p in definitions if isinstance(p,dict)}
        for p in definitions:
            if not isinstance(p, dict) or not _strong(p):
                continue
            try:
                effective = _effective_definition(p,by_name)
            except SourceError:
                continue
            fingerprint = opaque_id(seed, 'connection', effective)
            index.setdefault(fingerprint, set()).add(source_id)
    sources_by_id = {s['subscription_id']: s for s in sources}
    memberships = {}
    for name, provider in (providers or {}).items():
        if not isinstance(provider, dict):
            continue
        for p in provider.get('proxies', []):
            if isinstance(p, dict) and isinstance(p.get('name'), str):
                memberships.setdefault(p['name'], set()).add(str(name))
    nodes = []
    names_seen = set()
    runtime_by_name = {p.get('name'):p for p in runtime if isinstance(p,dict)}
    for p in runtime:
        if not isinstance(p, dict) or not isinstance(p.get('name'), str):
            continue
        if p['name'] in names_seen:
            raise SourceError('Duplicate runtime node name')
        names_seen.add(p['name'])
        strong = _strong(p)
        try:
            canonical = _effective_definition(p,runtime_by_name)
        except SourceError:
            canonical = canonical_connection(p)
            strong = False
        fingerprint = opaque_id(seed, 'connection', canonical)
        source_ids = sorted(index.get(fingerprint, set())) if strong else []
        status = 'verified' if len(source_ids) == 1 else 'ambiguous' if source_ids else 'unknown'
        for source_id in source_ids:
            source = sources_by_id[source_id]
            source['loaded'] = True
            source['mapping_status'] = 'verified' if status == 'verified' else 'ambiguous'
        # Weak identities are deliberately name-scoped. Never merge them into
        # strong histories; changing a weak name means a new, unproven identity.
        definition = canonical if strong else [canonical, p['name']]
        node_id = opaque_id(seed, 'node', [namespace, source_ids or ['unknown'], definition])
        node = dict(node_id=node_id, identity_version=2,
                    identity_strength='strong' if strong else 'weak', runtime_name=p['name'],
                    subscription_ids=source_ids,
                    subscription_name=sources_by_id[source_ids[0]]['name'] if status == 'verified' else '',
                    subscriptions=[dict(subscription_id=sid, name=sources_by_id[sid]['name'],
                                        kind=sources_by_id[sid]['kind']) for sid in source_ids],
                    provider_names=sorted(memberships.get(p['name'], set())), source_status=status,
                    evidence=['Exact local connection definition match'] if source_ids else
                             ['No verifiable local subscription definition'])
        nodes.append(node)
    return dict(version=2, status='ok', sources=sources, nodes=nodes)


def _read_document(path):
    try:
        with path.open('rb') as stream:
            content = stream.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise SourceError('Source file exceeds size limits')
        text = content.decode('utf-8-sig')
        # Preflight before the recursive YAML parser. Extreme indentation,
        # flow nesting or enormous line counts must not exhaust its stack.
        if len(text.splitlines()) > MAX_ITEMS * 20:
            raise SourceError('Source file exceeds line limits')
        if any(len(line) - len(line.lstrip(' ')) > MAX_DEPTH * 4 for line in text.splitlines()):
            raise SourceError('Source file exceeds nesting limits')
        if re.search(r'(^|\s)!!|(^|\s)![A-Za-z]|(^|\s)[&*][A-Za-z0-9]', text):
            raise SourceError('Source YAML tags and aliases are unsupported')
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            from speedbench_workers import _parse_verge_yaml
            document = _parse_verge_yaml(text)
        document = _bounded(document)
        if not isinstance(document, dict):
            raise SourceError('Source root must be a mapping')
        return document
    except (OSError, UnicodeError, ValueError, RecursionError, TypeError):
        raise SourceError('Source file unavailable or unsupported') from None


def _profile_path(root, name):
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise SourceError('Profile path is unsupported')
    # Reject Windows paths even when validating on a POSIX CI host.
    if '\\' in name or ':' in name or '..' in Path(name).parts:
        raise SourceError('Profile path escapes the profile directory')
    directory = (root / 'profiles').resolve()
    target = (directory / name).resolve()
    try:
        target.relative_to(directory)
    except ValueError:
        raise SourceError('Profile path escapes the profile directory') from None
    return target


def read_catalog(root, *, seed, runtime=None, providers=None):
    root = Path(root).resolve()
    status = 'ok'
    try:
        metadata = _read_document(root / 'profiles.yaml')
        items = metadata.get('items', [])
        if not isinstance(items, list):
            raise SourceError('Source metadata is unsupported')
    except SourceError:
        items = []
        status = 'metadata_unavailable'
    profiles = []
    for item in items:
        if not isinstance(item, dict) or item.get('type') not in ('remote', 'local'):
            continue
        p = {k: item.get(k) for k in ('uid', 'name', 'type')}
        try:
            content = _read_document(_profile_path(root, item.get('file')))
            p['proxies'] = content.get('proxies', [])
            if not isinstance(p['proxies'], list):
                raise SourceError('Profile nodes are unsupported')
            p['available'] = True
        except SourceError:
            p['available'] = False
        profiles.append(p)
    if runtime is None:
        try:
            runtime = _read_document(root / 'clash-verge.yaml').get('proxies', [])
        except SourceError:
            runtime = []
            status = 'runtime_unavailable'
    catalog = build_catalog(runtime, profiles, seed=seed,
                            namespace=os.path.normcase(str(root)), providers=providers)
    catalog['status'] = status
    return catalog


def discover_catalog(api, data_home, *, config_file='', snapshot=None, config_root=None):
    """Resolve only a controller bound to this local generated configuration.

    All failures are optional-feature failures. No config body, path or error
    string is exposed. Manual/remote controllers never acquire local origins.
    Identity seed lives next to application history, not the source checkout.
    """
    import speedbench_controller as controller
    unavailable = dict(version=2, status='configuration_unavailable', sources=[], nodes=[])
    try:
        base = getattr(api, 'controller_base', None)
        if not isinstance(base, str):
            return unavailable
        paths = ([Path(config_file)] if config_file else controller.config_paths() if config_root is None else
                 controller.config_paths(config_root=config_root))
        path = next((p for p in paths if p.is_file()), None)
        if path is None:
            return unavailable
        before = path.stat()
        document = _read_document(path)
        declared = []
        tcp = controller.local_tcp_base(str(document.get('external-controller') or ''))
        if tcp:
            declared.append(tcp)
        pipe = controller._local_pipe(str(document.get('external-controller-pipe') or ''))
        if pipe:
            declared.append(pipe)
        unix = document.get('external-controller-unix')
        if isinstance(unix, str) and unix.startswith('/'):
            declared.append('unix://' + unix)
        if controller.canonical_explicit(base) not in declared:
            unavailable['status'] = 'controller_config_mismatch'
            return unavailable
        if not isinstance(document.get('proxies'), list) or not document['proxies']:
            return unavailable
        seed = load_seed(Path(data_home) / 'identity-seed')
        proxies = snapshot if snapshot is not None else api.get('/proxies').get('proxies', {})
        if not isinstance(proxies, dict):
            return unavailable
        try:
            providers = api.get('/providers/proxies').get('providers', {})
        except Exception:
            providers = {}
        runtime = [p for p in document.get('proxies', [])
                   if isinstance(p, dict) and p.get('name') in proxies]
        cat = read_catalog(path.parent, seed=seed, runtime=runtime, providers=providers)
        after = path.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            unavailable['status'] = 'configuration_changed'
            return unavailable
        return cat
    except IdentityError:
        unavailable['status'] = 'identity_unavailable'
        return unavailable
    except Exception:
        return unavailable


def result_origin(origin):
    """Explicit result whitelist; never serialize a connection/config object."""
    if not isinstance(origin, dict):
        return {}
    result = {k: origin[k] for k in ('node_id', 'identity_version', 'identity_strength',
              'source_status', 'subscription_name') if isinstance(origin.get(k), (str, int))}
    for key in ('subscription_ids', 'provider_names', 'evidence'):
        values = origin.get(key)
        if isinstance(values, list):
            result[key] = [v for v in values[:MAX_ITEMS] if isinstance(v, str)]
    subscriptions = origin.get('subscriptions')
    if isinstance(subscriptions, list):
        result['subscriptions'] = [{k: s[k] for k in ('subscription_id', 'name', 'kind')
                                    if isinstance(s.get(k), str)}
                                   for s in subscriptions[:MAX_ITEMS] if isinstance(s, dict)]
    return result


def revalidate_origins(origins, runtime, *, seed, namespace):
    """Recheck the actual worker configuration, closing discovery/extraction drift.

    The source IDs came from an already bound catalogue. Credentials are used
    only to recompute HMAC in memory; changed identities receive no old origin.
    """
    runtime = _bounded(runtime)
    by_name = {p.get('name'): p for p in runtime if isinstance(p, dict)}
    checked = {}
    for name, origin in origins.items():
        p = by_name.get(name)
        if not isinstance(p, dict):
            continue
        try:
            canonical = _effective_definition(p, by_name)
            if origin.get('identity_strength') == 'weak':
                canonical = [canonical, name]
            sid = origin.get('subscription_ids') or ['unknown']
            identity = opaque_id(seed, 'node', [namespace, sid, canonical])
            if identity == origin.get('node_id'):
                checked[name] = origin
        except SourceError:
            continue
    return checked


def apply_origin(result, origin):
    if origin is None and getattr(result, 'origin', None):
        return
    result.origin = result_origin(origin)
    if result.origin.get('source_status') == 'verified':
        result.provider = result.origin.get('subscription_name', '')
