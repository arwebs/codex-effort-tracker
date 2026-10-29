"""Pure interval and text metrics. All time values are UTC epoch seconds."""
import re
from datetime import datetime, timezone


def epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            return None
    return None


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds') if value is not None else ''


def merge_intervals(intervals):
    merged = []
    for start, end in sorted((a, b) for a, b in intervals if a is not None and b is not None and b >= a):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def union_seconds(intervals):
    return sum(b - a for a, b in merge_intervals(intervals))


def timing(intervals, session_gap=1800):
    spans = merge_intervals(intervals)
    gaps = [spans[i][0] - spans[i - 1][1] for i in range(1, len(spans))]
    return dict(active=sum(b - a for a, b in spans), idle=sum(g for g in gaps if g <= session_gap),
                dormant=sum(g for g in gaps if g > session_gap), sessions=(1 + sum(g > session_gap for g in gaps)) if spans else 0)


def prose_words(text):
    text = re.sub(r'(?ms)^\s*(`{3,}|~{3,}).*?^\s*\1\s*$', ' ', text)
    # Unclosed fences also do not turn code into prose.
    text = re.sub(r'(?ms)^\s*(?:`{3,}|~{3,}).*\Z', ' ', text)
    text = re.sub(r'(`+).*?\1', ' ', text, flags=re.S)
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'https?://\S+', ' ', text)
    return len(re.findall(r"[^\W_]+(?:['’][^\W_]+)*", text, flags=re.UNICODE))
