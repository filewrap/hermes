"""Owner-configured tenant capabilities, enforced in a local Bubblewrap namespace.

This is not the ordinary LocalEnvironment backend (which is NOT a sandbox).
Only the adapters below are supported: arbitrary registry/plugin tools are denied.
No network, host home, credentials, uploads, delegation or deferred jobs are exposed.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
from typing import Any

logger = logging.getLogger(__name__)
SUPPORTED = frozenset({'terminal', 'read_file', 'write_file'})
KEYS = frozenset({'allowed_tools', 'denied_tools', 'denied_actions', 'allowed_paths', 'denied_paths', 'permitted_effects'})
DEFAULT = {'allowed_tools': sorted(SUPPORTED), 'denied_tools': [], 'denied_actions': [],
           'allowed_paths': ['/workspace'], 'denied_paths': [], 'permitted_effects': ['read', 'write', 'shell']}
_LOCK = threading.RLock()
_PROBES: dict[str, bool] = {}


def _subject(subject: str) -> str:
    subject = str(subject)
    if subject != 'allowed' and not re.fullmatch(r'[1-9][0-9]{0,19}', subject):
        raise ValueError('Policy subject must be allowed or a numeric user ID')
    return subject


def _validate(policy: dict) -> dict:
    if not isinstance(policy, dict) or set(policy) - KEYS:
        raise ValueError('Unknown policy keys')
    result = {}
    for key, value in policy.items():
        if not isinstance(value, list) or len(value) > 100 or any(not isinstance(x, str) or not x or len(x) > 1024 for x in value):
            raise ValueError('Policy values must be bounded string arrays')
        result[key] = sorted(set(value))
    if set(result.get('allowed_tools', [])) - SUPPORTED:
        raise ValueError('Supported bounded tools: terminal, read_file, write_file')
    if set(result.get('permitted_effects', [])) - {'read', 'write', 'shell'}:
        raise ValueError('Supported effects: read, write, shell; network/publish/upload unavailable')
    for key in ('allowed_paths', 'denied_paths'):
        for path in result.get(key, []):
            if not path.startswith('/workspace') or '\\' in path or '\x00' in path or '..' in Path(path).parts or not Path(path).is_relative_to('/workspace'):
                raise ValueError('Paths must be canonical /workspace paths')
    return result


def _file(root: Path) -> Path:
    root = Path(root).resolve(strict=True)
    path = root / 'tenant-tool-policies.json'
    if path.is_symlink():
        raise ValueError('Unsafe policy file')
    return path


def load_policy(root: Path, subject: str = 'allowed') -> dict:
    subject = _subject(subject)
    with _LOCK:
        path = _file(root)
        data = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError('Invalid policy store')
        # Per-user restrictions replace role fields explicitly; omitted fields inherit.
        return _validate({**DEFAULT, **_validate(data.get('allowed', {})), **_validate(data.get(subject, {}))})


def save_policy(root: Path, subject: str, policy: dict) -> None:
    subject, policy = _subject(subject), _validate(policy)
    with _LOCK:
        path = _file(root)
        data = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError('Invalid policy store')
        data[subject] = policy
        fd, tmp = tempfile.mkstemp(prefix='.tenant-policy-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(data, stream, sort_keys=True)
                stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def policy_for(scope: Any) -> dict:
    return load_policy(scope.root, str(scope.user_id or getattr(scope.principal, 'user_id', '')))


def _eligible(scope: Any) -> bool:
    if not getattr(scope, 'policy_allowed', False):
        return False
    # Re-read durable authorization at each execution, including cached callbacks.
    from gateway.mongo_multiuser import store_for_home
    store = store_for_home(Path(scope.root))
    uid = str(scope.user_id or scope.principal.user_id)
    access = store.access_for_scope(uid, scope.chat_type, scope.chat_id)
    generation = getattr(scope, 'authorization_generation', None)
    if type(generation) is not int or generation < 0 or store.authorization_generation(uid) != generation:
        return False
    return bool(access.get('allowed') and access.get('sudo') and not access.get('banned') and not access.get('revoked'))


def supported_tool_names(scope: Any) -> set[str]:
    if not getattr(scope, 'policy_allowed', False):
        return set()
    try:
        if not _eligible(scope):
            return set()
        policy = policy_for(scope)
    except Exception:
        return set()
    names = set(policy['allowed_tools']) - set(policy['denied_tools'])
    effects = set(policy['permitted_effects'])
    for name, effect in [('read_file', 'read'), ('write_file', 'write'), ('terminal', 'shell')]:
        if effect not in effects or name in policy['denied_actions'] or effect in policy['denied_actions']:
            names.discard(name)
    # Arbitrary shell cannot honor per-file deny rules without stronger mount policy.
    # Keep structured file tools available; expose this limitation in the owner UI.
    if policy['denied_paths'] or policy['allowed_paths'] != ['/workspace'] or 'read' not in effects or 'write' not in effects or policy['denied_actions']:
        names.discard('terminal')
    return names & SUPPORTED


def policy_guidance(scope: Any) -> str:
    if not getattr(scope, 'policy_allowed', False):
        return 'Group guest access grants conversation only, never tools or global/DM access.'
    policy = policy_for(scope)
    return ('Owner-enforced local policy: ' + json.dumps(policy, sort_keys=True) +
            '. Policy-permitted bounded tools (execution still requires a successful local namespace probe): ' + ', '.join(sorted(supported_tool_names(scope))) +
            '. Paths are inside /workspace only. Network, upload, publish, arbitrary host tools, '
            'background/deferred execution and delegation are unavailable. Shell requires full workspace '
            'read/write permission and no path/action restrictions. On a namespace failure, stop instead of '
            'attempting a host or alternate backend. Never claim unsupported isolation or aggregate resource quotas.')


def bounded_tool_definitions(scope: Any, names: set[str]) -> list[dict]:
    schemas = {
        'terminal': ('Run a foreground shell in your isolated /workspace. No host home, credentials, network, background work or alternate backend. Maximum 60 seconds.',
                     {'command': {'type':'string'}, 'timeout': {'type':'number','minimum':1,'maximum':60}, 'workdir': {'type':'string'}}, ['command']),
        'read_file': ('Read a UTF-8 file under /workspace, subject to owner resource policy. Never reads host files.',
                      {'path': {'type':'string'}, 'offset': {'type':'integer','minimum':1}, 'limit': {'type':'integer','minimum':1,'maximum':5000}}, ['path']),
        'write_file': ('Write a UTF-8 file under /workspace, subject to owner resource policy. No upload or publishing side effects.',
                       {'path': {'type':'string'}, 'content': {'type':'string'}}, ['path','content']),
    }
    return [{'type':'function','function':{'name':name,'description':schemas[name][0],
             'parameters':{'type':'object','properties':schemas[name][1], 'required':schemas[name][2], 'additionalProperties':False}}}
            for name in sorted(names & supported_tool_names(scope))]


def _workspace(scope: Any) -> Path:
    base = Path(scope.cache_home).resolve(strict=True)
    root = base / 'workspace'
    if root.is_symlink():
        raise PermissionError('Unsafe workspace')
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def _command(root: Path, command: list[str], *, writable: bool) -> list[str]:
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise PermissionError('Local namespace sandbox unavailable')
    args = [bwrap, '--die-with-parent', '--unshare-all', '--unshare-user', '--disable-userns', '--new-session', '--cap-drop', 'ALL',
            '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'HOME', '/workspace',
            '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'TMPDIR', '/tmp']
    for path in ('/usr', '/bin', '/sbin', '/lib', '/lib64'):
        if Path(path).exists():
            args += ['--ro-bind', path, path]
    args += ['--proc', '/proc', '--dev', '/dev', '--size', '16777216', '--tmpfs', '/tmp',
             '--bind' if writable else '--ro-bind', str(root), '/workspace', '--chdir', '/workspace', '--',
             '/usr/bin/prlimit', '--fsize=8388608', '--cpu=60', '--nofile=128',
             '--as=268435456', '--nproc=128', '--', *command]
    return args


def sandbox_available(scope: Any) -> bool:
    """Probe the real kernel boundary. Failure never falls back to host execution."""
    root = _workspace(scope)
    key = str(root)
    with _LOCK:
        if key not in _PROBES:
            try:
                result = subprocess.run(_command(root, ['/bin/true'], writable=False), stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                                        env={'PATH': '/usr/bin:/bin'}, check=False)
                _PROBES[key] = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                _PROBES[key] = False
            if len(_PROBES) > 256:
                _PROBES.pop(next(iter(_PROBES)))
        return _PROBES[key]


def _notify(scope: Any, name: str) -> None:
    uid = scope.user_id or getattr(scope.principal, 'user_id', 'unknown')
    safe_name = re.sub(r'[^A-Za-z0-9_.:-]', '_', str(name))[:80]
    message = f'Tenant policy denied profile={Path(scope.root).name} user={uid} tool={safe_name}'
    logger.warning(message)
    try:
        from plugins.platforms.telegram.forum_audit import enqueue_operation
        enqueue_operation(Path(scope.root), message)
    except Exception:
        logger.error('Operations audit enqueue unavailable')


# Runs ONLY inside the namespace. realpath checks happen where file I/O happens,
# so symlinks to denied paths cannot bypass preflight checks. Tenant tools run
# serially per room; no user-controlled host processes share this workspace.
_FILE_SCRIPT = r'''
import json, os, sys
p=json.load(sys.stdin); a=p['args']; policy=p['policy']
path=os.path.realpath(a.get('path') or '/workspace')
def inside(child, parent):
    return child == parent or child.startswith(parent.rstrip('/')+'/')
allowed=[os.path.realpath(x) for x in policy['allowed_paths']]
denied=[os.path.realpath(x) for x in policy['denied_paths']]
def same_inode(left, right):
    try: return os.path.samestat(os.stat(left), os.stat(right))
    except FileNotFoundError: return False
try: linked_file=os.stat(path).st_nlink>1 and not os.path.isdir(path)
except FileNotFoundError: linked_file=False
# A denied directory may contain an alias's inode without being its inode.
# Without an unbounded tree scan, restrict all hard-linked files when resources
# are narrowed. Shell is already suppressed under the same resource policies.
restricted_links=linked_file and (bool(denied) or policy['allowed_paths']!=['/workspace'])
if restricted_links or not inside(path, '/workspace') or not any(inside(path,x) for x in allowed) or any(inside(path,x) or same_inode(path,x) for x in denied):
    print(json.dumps({'error':'Resource denied by owner policy'})); sys.exit(0)
if p['name']=='read_file':
    offset=max(1,min(int(a.get('offset',1)),1000000)); limit=max(1,min(int(a.get('limit',2000)),5000))
    with open(path, encoding='utf-8') as f:
        if os.fstat(f.fileno()).st_size > 4*1024*1024: raise ValueError('File too large')
        lines=f.readlines()
    print(json.dumps({'content': ''.join(lines[offset-1:offset-1+limit])[:100000], 'total_lines':len(lines)}))
else:
    content=a.get('content','')
    if not isinstance(content,str) or len(content.encode()) > 1024*1024: raise ValueError('Content too large')
    os.makedirs(os.path.dirname(path),exist_ok=True)
    with open(path,'w',encoding='utf-8') as f: f.write(content)
    print(json.dumps({'success':True,'path':path}))
'''


def dispatch(scope: Any, name: str, args: dict) -> str:
    """Never invokes an existing host handler, even for a cached/direct call."""
    try:
        if not getattr(scope, 'policy_allowed', False):
            raise PermissionError('This tool is not available in isolated tenant sessions.')
        if not _eligible(scope) or name not in supported_tool_names(scope):
            raise PermissionError('Tool/action denied by owner policy')
        if not isinstance(args, dict):
            raise PermissionError('Invalid tool arguments')
        if not sandbox_available(scope):
            raise PermissionError('Local namespace sandbox unavailable; no host fallback')
        policy = policy_for(scope)
        root = _workspace(scope)
        if name == 'terminal':
            # Prevent persistence, alternate backends, elevated execution, and hidden effects.
            if set(args) - {'command', 'timeout', 'workdir'}:
                raise PermissionError('Background, privilege, and alternate execution options unavailable')
            command = args.get('command')
            if not isinstance(command, str) or len(command) > 32768:
                raise PermissionError('Invalid shell command')
            workdir = args.get('workdir') or '/workspace'
            if not isinstance(workdir, str) or not Path(workdir).is_absolute() or not Path(workdir).is_relative_to('/workspace') or '..' in Path(workdir).parts:
                raise PermissionError('Workdir outside workspace')
            command = ['/bin/bash', '--noprofile', '--norc', '-c', 'cd -- ' + shlex.quote(workdir) + ' && ' + command]
            payload = b''
        else:
            if set(args) - ({'path','offset','limit'} if name == 'read_file' else {'path','content'}):
                raise PermissionError('Unsupported file options')
            command = ['/usr/bin/python3', '-I', '-c', _FILE_SCRIPT]
            payload = json.dumps({'name':name,'args':args,'policy':policy}).encode()
        timeout = max(1, min(float(args.get('timeout', 30)), 60))
        # File-backed capped output avoids communicate() accumulating arbitrary output in RAM.
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(_command(root, command, writable=name != 'read_file'),
                                    stdin=subprocess.PIPE, stdout=output, stderr=output,
                                    env={'PATH':'/usr/bin:/bin'}, start_new_session=True)  # detached-spawn: ok — awaited; the timeout kills the whole group
            try:
                proc.communicate(payload, timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL); proc.wait()
                raise PermissionError('Bounded tool timed out') from None
            finally:
                # A shell must not leave detached workers behind after a successful exit.
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
            output.seek(0)
            # A 100k-character file result may use 1.2 MB of JSON surrogate
            # escapes. Do not truncate that JSON midway through a codepoint.
            raw = output.read(100001 if name == 'terminal' else 1500001)
            text = raw.decode('utf-8', errors='replace')
        if proc.returncode and text.lstrip().startswith(('bwrap:', 'prlimit:')):
            raise PermissionError('Local sandbox execution unavailable; no host fallback')
        if name == 'terminal':
            return json.dumps({'output':text[:100000], 'returncode':proc.returncode, 'truncated':len(raw)>100000})
        if proc.returncode:
            raise PermissionError('Bounded file operation failed')
        result = json.loads(text)
        if 'error' in result:
            _notify(scope, name)
        return json.dumps(result)
    except Exception as exc:
        _notify(scope, name)
        return json.dumps({'error': str(exc) if isinstance(exc, PermissionError) else 'Tool denied: invalid policy or arguments'})
