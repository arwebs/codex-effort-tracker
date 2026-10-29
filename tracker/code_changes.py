"""Net workspace changes using hashes of code lines, not stored source text."""
import difflib
import hashlib
import subprocess
import tempfile
import os
import signal
from pathlib import Path

CODE_EXTENSIONS = {'.cs','.fs','.vb','.py','.js','.jsx','.ts','.tsx','.sql','.ps1','.psm1','.sh','.bash','.c','.h','.cpp','.hpp','.java','.go','.rs','.rb','.php','.swift','.kt','.kts','.vue','.svelte','.css','.scss','.html','.razor','.cshtml'}
EXCLUDED = {'.git','node_modules','vendor','bin','obj','dist','build','coverage','__pycache__','.next'}


def run_command(command, timeout=5, text=False):
    # Git for Windows' launcher can leave a child holding captured pipes open.
    # File-backed output and whole-tree cancellation keep the timeout bounded.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process=subprocess.Popen(command,stdout=stdout,stderr=stderr,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),start_new_session=os.name!='nt')
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name=='nt':
                try:
                    subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                   timeout=5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                except (OSError,subprocess.SubprocessError):
                    pass
            else:
                try:
                    os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            raise TimeoutError('Git code scan timed out') from None
        stdout.seek(0)
        stderr.seek(0)
        out,err=stdout.read(),stderr.read()
        if text:
            out,err=out.decode('utf-8','surrogateescape'),err.decode('utf-8','surrogateescape')
        return subprocess.CompletedProcess(command,process.returncode,out,err)


def git_root(path):
    result = run_command(['git','-C',str(path),'rev-parse','--show-toplevel'],text=True,timeout=5)
    if result.returncode:
        return None
    root=Path(result.stdout.strip()).resolve()
    return None if root in (Path.home().resolve(),Path(root.anchor)) else root


def snapshot(path, max_bytes=30_000_000):
    root = git_root(path)
    if root is None:
        return None
    result = run_command(['git','-C',str(root),'ls-files','-z','--cached','--others','--exclude-standard'],timeout=15)
    if result.returncode:
        raise RuntimeError('Cannot enumerate code baseline')
    files, total, complete = {}, 0, True
    for name in sorted(set(result.stdout.decode('utf-8','surrogateescape').split('\0'))):
        if not name:
            continue
        relative=Path(name)
        if relative.suffix.lower() not in CODE_EXTENSIONS or any(part.lower() in EXCLUDED for part in relative.parts) or name.lower().endswith(('.g.cs','.designer.cs','.min.js','.min.css')):
            continue
        file=root/relative
        if file.is_symlink() or not file.exists():
            continue
        if not file.resolve().is_relative_to(root):
            complete=False
            continue
        try:
            size=file.stat().st_size
            total += size
            if size>2_000_000 or total>max_bytes:
                complete=False
                continue
            content=file.read_bytes()
            if b'\0' in content:
                continue
            text=content.decode('utf-8-sig')
        except (OSError,UnicodeError):
            complete=False
            continue
        files[name]=[hashlib.sha256(line.encode('utf-8')).hexdigest() for line in text.splitlines()]
    return dict(root=str(root),files=files,complete=complete)


def net_changes(before, after):
    if not before or not after or not before['complete'] or not after['complete']:
        return None,None
    added=removed=0
    for name in set(before['files']) | set(after['files']):
        old,new=before['files'].get(name,[]),after['files'].get(name,[])
        for tag,a,b,c,d in difflib.SequenceMatcher(None,old,new,autojunk=False).get_opcodes():
            if tag!='equal':
                removed+=b-a
                added+=d-c
    return added,removed
