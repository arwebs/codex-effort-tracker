"""Collector-owned state only; Codex databases are opened elsewhere read-only."""
import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(thread_id TEXT, kind TEXT, event_key TEXT,
                turn_id TEXT, ts REAL, data TEXT NOT NULL, source TEXT,
                PRIMARY KEY(thread_id,kind,event_key));
            CREATE INDEX IF NOT EXISTS events_thread_turn ON events(thread_id,turn_id);
            CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, offset INTEGER, state TEXT);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY, start REAL, end REAL,
                category TEXT, thread_id TEXT, quality TEXT);
            CREATE TABLE IF NOT EXISTS hooks(id TEXT PRIMARY KEY, received REAL, data TEXT);
        ''')

    def close(self):
        self.db.close()

    def thread(self, tid, values=None):
        row = self.db.execute('SELECT data FROM threads WHERE id=?', (tid,)).fetchone()
        data = json.loads(row[0]) if row else {'id': tid}
        if values is not None:
            data.update(values)
            self.db.execute('INSERT OR REPLACE INTO threads VALUES(?,?)', (tid, json.dumps(data)))
        return data

    def all_threads(self):
        return {r['id']: json.loads(r['data']) for r in self.db.execute('SELECT * FROM threads')}

    def event(self, tid, kind, key, turn, ts, data, source=''):
        self.db.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?)',
                        (tid, kind, str(key), turn, ts, json.dumps(data), source))

    def events(self, tid=None):
        query = 'SELECT * FROM events' + (' WHERE thread_id=?' if tid else '') + ' ORDER BY ts,event_key'
        return [dict(r, data=json.loads(r['data'])) for r in self.db.execute(query, (tid,) if tid else ())]

    def setting(self, key, value=None):
        if value is not None:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None
