import sqlite3, threading, json, uuid, os
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone

def now(): return datetime.now(timezone.utc).isoformat()
def uid(prefix): return prefix+'-'+uuid.uuid4().hex[:12]
def packed(value): return json.dumps(value,ensure_ascii=False,separators=(',',':'))

class Store:
    def __init__(self, path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.lock=threading.RLock(); self.local=threading.local()
        self.db=sqlite3.connect(path,check_same_thread=False,isolation_level=None)
        os.chmod(path,0o600)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL'); self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, created TEXT, mode TEXT, state TEXT, status TEXT, contents TEXT, cursor INTEGER DEFAULT 0, waiting TEXT, failure TEXT, model_calls INTEGER DEFAULT 0, clock_wakes INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events(run TEXT, seq INTEGER, id TEXT UNIQUE, at TEXT, kind TEXT, actor TEXT, source TEXT, payload TEXT, before_v INTEGER, after_v INTEGER, incoming INTEGER DEFAULT 0, PRIMARY KEY(run,seq));
        CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, run TEXT, recipient TEXT, text TEXT, purpose TEXT, response_kind TEXT, created TEXT, version INTEGER, answered TEXT, read_at TEXT, source_event TEXT, trigger_seq INTEGER);
        CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY, run TEXT, name TEXT, path TEXT, sha TEXT, duration REAL, source TEXT);
        CREATE TABLE IF NOT EXISTS dedupe(run TEXT, key TEXT, result TEXT, PRIMARY KEY(run,key));
        CREATE TABLE IF NOT EXISTS cache(run TEXT, key TEXT, result TEXT, PRIMARY KEY(run,key));
        CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY, run TEXT, data TEXT);
        ''')
    @contextmanager
    def tx(self):
        with self.lock:
            depth=getattr(self.local,'depth',0)
            if depth==0: self.db.execute('BEGIN IMMEDIATE')
            self.local.depth=depth+1
            try:
                yield self.db
                if depth==0: self.db.execute('COMMIT')
            except BaseException:
                if depth==0: self.db.execute('ROLLBACK')
                raise
            finally: self.local.depth=depth
    def row(self, rid):
        with self.lock:
            result=self.db.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
            if not result: raise ValueError('Unknown run')
            return dict(result)
    def state(self,rid): return json.loads(self.row(rid)['state'])
    def save_state(self,rid,state): self.db.execute('UPDATE runs SET state=? WHERE id=?',(packed(state),rid))
    def log(self,rid,kind,payload,actor='runtime',source='APP_INTERNAL',incoming=False,before=None,after=None):
        with self.tx():
            state=self.state(rid); ver=state['state_version']
            seq=self.db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run=?',(rid,)).fetchone()[0]
            event={'event_id':uid('E'),'run_id':rid,'sequence':seq,'received_at':now(),'event_type':kind,'actor':actor,'source_mode':source,'payload':payload,'state_version_before':ver if before is None else before,'state_version_after':ver if after is None else after}
            self.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)',(rid,seq,event['event_id'],event['received_at'],kind,actor,source,packed(payload),event['state_version_before'],event['state_version_after'],int(incoming)))
            return event
    def events(self,rid,after=0,incoming=False):
        with self.lock:
            query='SELECT * FROM events WHERE run=? AND seq>?'+(' AND incoming=1' if incoming else '')+' ORDER BY seq'
            rows=self.db.execute(query,(rid,after)).fetchall()
            return [{'event_id':r['id'],'run_id':rid,'sequence':r['seq'],'received_at':r['at'],'event_type':r['kind'],'actor':r['actor'],'source_mode':r['source'],'payload':json.loads(r['payload']),'state_version_before':r['before_v'],'state_version_after':r['after_v']} for r in rows]
    def messages(self,rid):
        with self.lock: return [dict(r) for r in self.db.execute('SELECT * FROM messages WHERE run=? ORDER BY created,id',(rid,))]
    def decisions(self,rid):
        with self.lock: return [json.loads(r['data']) for r in self.db.execute('SELECT data FROM decisions WHERE run=? ORDER BY rowid',(rid,))]
    def put_decision(self,rid,decision):
        with self.tx(): self.db.execute('INSERT OR REPLACE INTO decisions VALUES(?,?,?)',(decision['decision_id'],rid,packed(decision)))
    def cache_get(self,rid,key,table='cache'):
        assert table in ('cache','dedupe')
        with self.lock:
            row=self.db.execute(f'SELECT result FROM {table} WHERE run=? AND key=?',(rid,key)).fetchone()
            return json.loads(row[0]) if row else None
    def cache_set(self,rid,key,result,table='cache'):
        assert table in ('cache','dedupe')
        self.db.execute(f'INSERT INTO {table} VALUES(?,?,?)',(rid,key,packed(result)))
