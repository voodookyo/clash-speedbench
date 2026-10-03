"""Strict non-secret UI preferences, shared across random desktop ports.

Never store keys, controller addresses, paths, subscriptions or task results.
The backend's data-directory lease provides cross-process single ownership.
"""
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading

from speedbench_identity import _secure_new_file, _windows_private, _windows_sid, IdentityError

LOCK=threading.RLock()
ENUMS={'sb_theme':('system','light','dark'),
       'sb_profile':('all','daily','download','ipclean','residential'),
       'sb_mode':('quick','standard','deep','ip'),
       'sb_target':('daily','download','balanced','ip','residential'),
       'sb_subs_days':('7','30','90','365'),
       'sb_notifications':('off','on')}
MAX_BYTES=262144


class PreferenceError(ValueError):pass


def validate(values):
    if not isinstance(values,dict) or set(values)-set(ENUMS)-{'sb_favs','sb_favs_v2'}:
        raise PreferenceError('Unsupported preference fields')
    result={}
    for key,value in values.items():
        if not isinstance(value,str):raise PreferenceError('Invalid preference value')
        if key in ENUMS:
            if value not in ENUMS[key]:raise PreferenceError('Invalid preference choice')
        else:
            try:names=json.loads(value)
            except ValueError:raise PreferenceError('Invalid favorites') from None
            if not isinstance(names,list) or len(names)>2000 or any(not isinstance(n,str) or len(n)>512 for n in names):
                raise PreferenceError('Invalid favorites')
            if key=='sb_favs_v2' and any(not re.fullmatch(r'node_v2_[0-9a-f]{32}',n) for n in names):
                raise PreferenceError('Invalid stable favorite identity')
            value=json.dumps(list(dict.fromkeys(names)),ensure_ascii=False)
        result[key]=value
    if len(json.dumps(result,ensure_ascii=False).encode('utf-8'))>MAX_BYTES:
        raise PreferenceError('Preferences exceed limit')
    return result


class Preferences:
    def __init__(self,directory):self.path=Path(directory)/'ui-preferences.json'

    def _read(self):
        if not self.path.exists() and not self.path.is_symlink():return {}
        before=self.path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size>MAX_BYTES:raise PreferenceError('Unsafe preference file')
        if os.name=='nt':_windows_private(self.path,_windows_sid())
        elif before.st_uid!=os.getuid() or before.st_mode & 0o077:raise PreferenceError('Unsafe preference permissions')
        descriptor=os.open(str(self.path),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(descriptor,'rb') as stream:
            actual=os.fstat(stream.fileno())
            if (actual.st_ino,actual.st_dev)!=(before.st_ino,before.st_dev):raise PreferenceError('Preferences changed during read')
            value=json.loads(stream.read(MAX_BYTES+1))
        if not isinstance(value,dict) or set(value)!={'version','values'} or type(value['version']) is not int or value['version']!=1:
            raise PreferenceError('Unsupported preferences schema')
        return validate(value['values'])

    def read(self):
        try:
            with LOCK:return self._read()
        except (OSError,ValueError,IdentityError):
            raise PreferenceError('Preferences unavailable; preserve the file and restore a private backup') from None

    def patch(self,values):
        values=validate(values);temporary=None
        try:
            with LOCK:
                current=self._read();current.update(values);current=validate(current)
                self.path.parent.mkdir(parents=True,exist_ok=True)
                descriptor,temporary=tempfile.mkstemp(prefix='.ui-preferences-',dir=str(self.path.parent))
                with os.fdopen(descriptor,'wb') as stream:
                    _secure_new_file(temporary,_windows_sid() if os.name=='nt' else None)
                    stream.write(json.dumps({'version':1,'values':current},ensure_ascii=False).encode('utf-8'))
                    stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,self.path);temporary=None
                return current
        except (OSError,ValueError,IdentityError):
            raise PreferenceError('Preferences were not saved; preserve the previous private file') from None
        finally:
            if temporary is not None:
                try:os.unlink(temporary) # Only this call's mkstemp artifact.
                except OSError:pass
