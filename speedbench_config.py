"""Session-only, fixed-layout local Verge root selection. No config export API."""
import os
from pathlib import Path
import stat
import threading

ENV='SPEEDBENCH_VERGE_ROOT'
MAX_PATH=2048
MAX_CONFIG_BYTES=8*1024*1024


class ConfigRootError(ValueError):
    pass


def validate_root(value):
    """Check only fixed layout/stat metadata, never enumerate/read arbitrary files.

    The source catalogue still independently validates/parses bounded documents
    and matches the controller. Layout validation is not connection validation.
    """
    try:
        if not isinstance(value,str) or len(value)>MAX_PATH or any(ord(c)<32 for c in value):
            raise ValueError()
        value.encode('utf-8')
        if value=='':return ''
        if value.startswith(('\\\\','//')):raise ValueError()
        path=Path(value)
        if not path.is_absolute():raise ValueError()
        if os.name=='nt':
            import ctypes
            # Do not stat an authenticated browser's UNC/mapped SMB target.
            if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor))==4:raise ValueError()
        root=path.resolve(strict=True)
        if root.parent==root or not root.is_dir():raise ValueError()
        for name in ('clash-verge.yaml','profiles.yaml'):
            fixed=root/name
            if fixed.is_symlink() or fixed.resolve(strict=True)!=fixed:raise ValueError()
            metadata=fixed.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size>MAX_CONFIG_BYTES:raise ValueError()
        profiles=root/'profiles'
        if profiles.resolve(strict=True)!=profiles or not profiles.is_dir():raise ValueError()
        return str(root)
    except (ValueError,UnicodeError,OSError,RuntimeError):
        raise ConfigRootError('请选择包含 clash-verge.yaml、profiles.yaml 和 profiles 目录的本机绝对路径；不支持远程、越界链接或超限文件') from None


class RootChoice:
    def __init__(self,initial=''):
        self._lock=threading.RLock();self._revision=0
        self._root=initial
        self._invalid=False
        try:self._root=validate_root(initial)
        except ConfigRootError:self._invalid=True

    def snapshot(self):
        with self._lock:return self._root,self._revision

    def public(self):
        with self._lock:
            return dict(mode='invalid' if self._invalid else 'custom' if self._root else 'auto',
                        path=None if self._invalid else self._root or None,
                        revision=self._revision,persistence='session',connection_verified=False)

    def apply(self,value):
        root=validate_root(value)
        with self._lock:
            self._root=root;self._invalid=False;self._revision+=1
            return self.public()
