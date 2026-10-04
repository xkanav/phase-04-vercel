#!/usr/bin/env python3
"""Start a loopback-only SaunpDo demo; no production identity or carrier service."""
import argparse, json, mimetypes, re, signal
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from app import App, Rejected, ROOT

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send(self,status,body,kind='application/json; charset=utf-8',extra=None):
        if not isinstance(body,bytes): body=json.dumps(body,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type',kind); self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'")
        if extra:
            for k,v in extra.items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(body)
    def trusted(self):
        host=self.headers.get('Host','')
        allowed={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
        origin=self.headers.get('Origin')
        return host in allowed and (not origin or origin in {'http://'+x for x in allowed})
    def route(self):
        parsed=urlparse(self.path)
        return parsed.path,{k:v[0] for k,v in parse_qs(parsed.query).items()}
    def do_GET(self):
        if not self.trusted(): return self.send(403,{'error':'Local demo only.'})
        app=self.server.app; path,q=self.route()
        try:
            if path in ('/','/index.html'): return self.send(200,(ROOT/'static/index.html').read_bytes(),'text/html; charset=utf-8')
            if path in ('/app.js','/style.css'):
                return self.send(200,(ROOT/'static'/path[1:]).read_bytes(),'text/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8')
            if path=='/api/session': return self.send(200,{'tokens':app.tokens,'identity_note':'Demo role controls, not production authentication.'})
            if path=='/api/state': return self.send(200,app.view(q.get('run')))
            if path=='/api/audio':
                with app.store.lock: asset=app.store.db.execute('SELECT * FROM assets WHERE id=?',(q.get('id'),)).fetchone()
                if not asset: raise Rejected('UNKNOWN_AUDIO','Recording not found.')
                p=app.data/asset['path']; return self.send(200,p.read_bytes(),'audio/mp4' if p.suffix=='.m4a' else (mimetypes.guess_type(p.name)[0] or 'application/octet-stream'))
            if path=='/api/artifact':
                rid=q.get('run'); app.store.row(rid); base=(app.data/'runs'/rid).resolve(); requested=(base/q.get('path','')).resolve()
                if not requested.is_relative_to(base) or not requested.is_file() or requested.name.endswith('.tmp'): raise Rejected('UNKNOWN_ARTIFACT','Artifact not found.')
                return self.send(200,requested.read_bytes(),'application/json; charset=utf-8' if requested.suffix=='.json' else 'text/plain; charset=utf-8')
            if path=='/api/export': return self.send(200,app.export_zip(q['run']),'application/zip',{'Content-Disposition':'attachment; filename="saunpdo-run-evidence.zip"'})
            self.send(404,{'error':'Not found'})
        except (ValueError,KeyError): self.send(404,{'error':'The requested run or file was not found.'})
    def do_POST(self):
        if not self.trusted(): return self.send(403,{'error':'Local demo only.'})
        app=self.server.app; path,q=self.route()
        role=next((r for r,t in app.tokens.items() if self.headers.get('X-SaunpDo-Session')==t),None)
        if not role: return self.send(403,{'error':'Open a demo role session first.'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<29*1024*1024: raise Rejected('REQUEST_SIZE','Request is too large or empty.')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict): raise Rejected('REQUEST_FORMAT','Expected a JSON object.')
            if path=='/api/run' and role=='presenter':
                if set(data)!={'mode'}: raise Rejected('REQUEST_FORMAT','Only the run mode can be set.')
                return self.send(200,{'run_id':app.new_run(data['mode'])})
            if path=='/api/action': return self.send(200,app.action(data['run_id'],role,data['kind'],data['args'],data['request_id'],data['expected_state_version']))
            if path=='/api/upload' and role=='receiver': return self.send(200,app.upload(data['run_id'],data['name'],data['data']))
            if path=='/api/read': return self.send(200,app.read_message(data['run_id'],role,data['message_id']))
            self.send(403,{'error':'This role cannot use that action.'})
        except Rejected as exc: self.send(409,{'error':str(exc),'code':exc.code})
        except (KeyError,ValueError,TypeError): self.send(400,{'error':'The request was incomplete or invalid.'})
        except Exception: self.send(500,{'error':'A local error stopped this request. No success was assumed.'})

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--project',type=Path,default=ROOT.parent.parent)
    parser.add_argument('--data',type=Path,default=ROOT/'data')
    args=parser.parse_args()
    app=App(args.data,args.project)
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler); server.app=app
    print(f'SaunpDo is ready at http://127.0.0.1:{args.port}',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: app.stop.set(); server.server_close()

if __name__=='__main__': main()
