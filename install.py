"""Idempotent per-user installation. Never edits Codex hook trust state."""
import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from tracker.cli import atomic_json, HOOK_EVENTS


def remove_hooks(document,command):
    result=copy.deepcopy(document)
    for event,groups in list(result.get('hooks',{}).items()):
        kept=[]
        for group in groups:
            handlers=group.get('hooks',[])
            remaining=[h for h in handlers if h.get('command')!=command]
            if len(remaining)==len(handlers):
                kept.append(group)
            elif remaining:
                kept.append(dict(group,hooks=remaining))
        if kept:
            result['hooks'][event]=kept
        else:
            result['hooks'].pop(event,None)
    return result


def merge_hooks(document,command):
    result=remove_hooks(document,command)
    for event in sorted(HOOK_EVENTS):
        result.setdefault('hooks',{}).setdefault(event,[]).append({'hooks':[{'type':'command','command':command,'timeout':5,'statusMessage':'Recording local effort metrics'}]})
    return result


def ps_quote(value):
    return "'"+str(value).replace("'","''")+"'"


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('action',choices=['install','uninstall'])
    args=parser.parse_args()
    if os.name!='nt':
        parser.error('This per-user installer targets Windows')
    config_path=args.config.resolve()
    config=json.loads(config_path.read_text(encoding='utf-8-sig'))
    data=Path(config['data_dir'])
    data.mkdir(parents=True,exist_ok=True)
    runner=Path(__file__).resolve().parent/'run_tracker.py'
    python=Path(sys.executable).with_name('python.exe')
    pythonw=python.with_name('pythonw.exe')
    if not python.exists() or not pythonw.exists():
        raise RuntimeError('Python and pythonw must exist before installation')
    command='& '+ps_quote(python)+' '+ps_quote(runner)+' --config '+ps_quote(config_path)+' hook'
    hook_file=Path(config['codex_home'])/'hooks.json'
    original_bytes=hook_file.read_bytes() if hook_file.exists() else None
    document=json.loads(original_bytes.decode('utf-8-sig')) if original_bytes is not None else {'hooks':{}}
    startup=Path(os.environ['APPDATA'])/'Microsoft/Windows/Start Menu/Programs/Startup/CodexEffortTracker.vbs'
    launch_command=subprocess.list2cmdline([str(pythonw),str(runner),'--config',str(config_path),'watch'])
    script='Set shell = CreateObject("WScript.Shell")\r\nshell.Run "'+launch_command.replace('"','""')+'", 0, False\r\n'
    receipt_path=data/'installation.json'
    if args.action=='install':
        if startup.exists() and startup.read_text(encoding='utf-8-sig')!=script.replace('\r\n','\n'):
            raise RuntimeError('Startup launcher already exists with different content; leaving it untouched')
        if original_bytes is not None:
            backup=data/('hooks-before-'+time.strftime('%Y%m%d-%H%M%S')+'.json')
            backup.write_bytes(original_bytes)
        # Detect changes made since we read the file rather than replacing a concurrent edit.
        if (hook_file.read_bytes() if hook_file.exists() else None)!=original_bytes:
            raise RuntimeError('Hooks changed during installation; retry after reviewing the edit')
        atomic_json(hook_file,merge_hooks(document,command))
        startup.parent.mkdir(parents=True,exist_ok=True)
        startup.write_bytes(script.encode('utf-8'))
        atomic_json(receipt_path,dict(hook_file=str(hook_file),command=command,startup=str(startup),startup_sha256=hashlib.sha256(script.encode()).hexdigest(),installed_at=time.time()))
        (data/'paused').unlink(missing_ok=True)
        subprocess.Popen([str(pythonw),str(runner),'--config',str(config_path),'watch'],creationflags=subprocess.CREATE_NO_WINDOW,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        print(json.dumps({'installed':True,'hooks_require_normal_trust_review':True,'startup':str(startup),'output_dir':config['output_dir']},indent=2))
    else:
        receipt=json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        if startup.exists() and hashlib.sha256(startup.read_bytes()).hexdigest()!=receipt.get('startup_sha256'):
            raise RuntimeError('Startup launcher has changed or lacks a receipt; leaving installation untouched')
        (data/'paused').touch()
        atomic_json(hook_file,remove_hooks(document,receipt.get('command',command)))
        startup.unlink(missing_ok=True)
        print('Uninstalled owned hooks and startup launcher; observer pause requested. Data and backups retained.')


if __name__=='__main__':
    main()
