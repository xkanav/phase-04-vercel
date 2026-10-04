"""Local HTTP boundary/export checks. No real provider calls."""
import io,json,threading,unittest,urllib.request,urllib.error,zipfile
from http.server import ThreadingHTTPServer
import test_app
from server import Handler
class HttpTests(test_app.AppTests):
    def setUp(self):
        super().setUp(); self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler); self.server.app=self.app
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start(); self.base='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); super().tearDown()
    def fetch(self,path,body=None,headers=None):
        req=urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers or {})
        try:
            with urllib.request.urlopen(req,timeout=5) as response: return response.status,response.read()
        except urllib.error.HTTPError as e: return e.code,e.read()
    def test_http_blocks_foreign_host_and_origin(self):
        self.assertEqual(self.fetch('/api/state',headers={'Host':'attacker.example'})[0],403)
        self.assertEqual(self.fetch('/api/state',headers={'Origin':'https://attacker.example'})[0],403)
    def test_http_requires_role_and_rejects_role_escalation(self):
        self.assertEqual(self.fetch('/api/run',{'mode':'practice'})[0],403)
        self.assertEqual(self.fetch('/api/run',{'mode':'practice'},{'X-SaunpDo-Session':self.app.tokens['receiver']})[0],403)
    def test_http_artifacts_cannot_escape_run_directory(self):
        self.assertEqual(self.fetch('/api/artifact?run='+self.rid+'&path=../../saunpdo.sqlite3')[0],404)
        self.assertEqual(self.fetch('/.env')[0],404)
    def test_http_export_preserves_sources_and_omits_secrets(self):
        self.grant(); self.app.settings['GEMINI_API_KEY']='local-test-secret-never-export'
        status,raw=self.fetch('/api/export?run='+self.rid); self.assertEqual(status,200)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            self.assertIn('system-prompt.md',z.namelist()); self.assertIn('events.jsonl',z.namelist()); self.assertIn('decision-ledger.md',z.namelist())
            for name in z.namelist(): self.assertNotIn(b'local-test-secret-never-export',z.read(name))
            self.assertIn('OPERATOR_TEST_ACTION',z.read('events.jsonl').decode())
    def test_http_state_survives_another_read_without_fabricating_actions(self):
        self.grant(); path='/api/state?run='+self.rid
        first=json.loads(self.fetch(path)[1]); second=json.loads(self.fetch(path)[1])
        self.assertEqual(first['case'],second['case']); self.assertEqual(first['events'],second['events'])
# Inherit setup helpers only, not the separate AppTests suite.
for name in list(test_app.AppTests.__dict__):
    if name.startswith('test_') and name not in HttpTests.__dict__: setattr(HttpTests,name,None)
if __name__=='__main__': unittest.main()
