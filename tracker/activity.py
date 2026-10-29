"""Read-only foreground and last-input sampling; never records input contents."""
import ctypes
import os
import time
from pathlib import Path


def resolve_title(title, threads):
    candidates = set()
    for tid, data in threads.items():
        name = data.get('name') or data.get('title')
        if name and title in (name + ' - Codex', name + ' — Codex', name + ' – Codex'):
            candidates.add(tid)
    return next(iter(candidates)) if len(candidates) == 1 else None


def classify_interval(previous, current, idle_seconds=300, max_gap=15):
    a, b = previous['at'], current['at']
    if b <= a:
        return []
    base = dict(start=a, end=b, thread_id=previous.get('thread_id'), quality=previous.get('quality', 'unknown'))
    if b-a > max_gap or previous.get('unavailable') or current.get('unavailable') or any(previous.get(k) != current.get(k) for k in ('foreground', 'thread_id', 'quality')):
        return [dict(base, category='unavailable', thread_id=None)]
    if not previous.get('foreground'):
        return [dict(base, category='away', thread_id=None)]
    # New input somewhere in this sample window cannot establish its exact preceding engagement.
    last_input = previous.get('last_input')
    if last_input is None:
        return [dict(base, category='unavailable', thread_id=None)]
    cutoff = last_input + idle_seconds
    result = []
    if cutoff > a:
        result.append(dict(base, end=min(b,cutoff), category='active'))
    if cutoff < b:
        result.append(dict(base, start=max(a,cutoff), category='idle'))
    return result


def sample_windows(threads):
    now = time.time()
    if os.name != 'nt':
        return dict(at=now, unavailable=True, quality='unsupported_os')
    from ctypes import wintypes as w
    user, kernel = ctypes.WinDLL('user32', use_last_error=True), ctypes.WinDLL('kernel32', use_last_error=True)
    user.GetForegroundWindow.restype = w.HWND
    user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.GetTickCount64.restype = ctypes.c_ulonglong
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [('cbSize',w.UINT),('dwTime',w.DWORD)]
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO),0)
    if not user.GetLastInputInfo(ctypes.byref(info)):
        return dict(at=now, unavailable=True, quality='input_unavailable')
    idle_ms = (kernel.GetTickCount64() - info.dwTime) & 0xffffffff
    hwnd = user.GetForegroundWindow()
    if not hwnd:
        return dict(at=now, unavailable=True, quality='locked_or_no_foreground')
    pid = w.DWORD()
    user.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
    handle = kernel.OpenProcess(0x1000,False,pid.value)
    if not handle:
        return dict(at=now, unavailable=True, quality='foreground_unavailable')
    try:
        name, size = ctypes.create_unicode_buffer(4096), w.DWORD(4096)
        if not kernel.QueryFullProcessImageNameW(handle,0,name,ctypes.byref(size)):
            return dict(at=now, unavailable=True, quality='foreground_unavailable')
        process = Path(name.value).name.lower()
    finally:
        kernel.CloseHandle(handle)
    title = ctypes.create_unicode_buffer(1024)
    user.GetWindowTextW(hwnd,title,1024)
    foreground = process in ('codex.exe','chatgpt.exe')
    tid = resolve_title(title.value,threads) if foreground else None
    quality = 'unique_codex_window_title' if tid else ('chatgpt_host_surface_unverified' if process=='chatgpt.exe' else ('codex_unassigned' if foreground else 'other_app'))
    return dict(at=now, foreground=foreground, thread_id=tid, quality=quality, last_input=now-idle_ms/1000)


def record_sample(store, previous, current, idle_seconds=300):
    if previous:
        for interval in classify_interval(previous,current,idle_seconds):
            store.db.execute('INSERT INTO activity(start,end,category,thread_id,quality) VALUES(?,?,?,?,?)',
                             tuple(interval[k] for k in ('start','end','category','thread_id','quality')))
    store.setting('last_sample', {k:current.get(k) for k in ('at','foreground','thread_id','quality','unavailable')})
    store.db.commit()
