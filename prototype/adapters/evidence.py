"""Append-only event records and exact HTTP body artifacts."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import html
import json
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Evidence:
    def __init__(self, root, mode='LIVE_API_WITH_FICTIONAL_CASE', secrets=()):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.mode = mode
        self.secrets = tuple(x for x in secrets if x)
        self.run_id = self.root.name
        self.events = []
        self.event('run_started', {'mode': mode, 'live_proof_passed': False})

    def clean(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, '[REDACTED]')
            return value
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        return value

    def write(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if isinstance(body, str):
            body = body.encode('utf-8')
        path.write_bytes(body)
        path.chmod(0o600)
        return {'path': name, 'sha256': digest(body), 'bytes': len(body)}

    def json(self, name, obj):
        return self.write(name, json.dumps(self.clean(obj), ensure_ascii=False, indent=2) + '\n')

    def event(self, kind, details):
        item = {'event_id': f'E{len(self.events)+1:04}', 'at_utc': now(),
                'run_id': self.run_id, 'run_mode': self.mode, 'kind': kind,
                'details': self.clean(details)}
        self.events.append(item)
        path = self.root / 'events.jsonl'
        with path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + '\n')
        path.chmod(0o600)
        return item

    def finish(self, summary):
        summary = {**summary, 'run_id': self.run_id, 'run_mode': self.mode,
                   'finished_at_utc': now()}
        if self.mode == 'OFFLINE_TEST_ONLY':
            summary['live_proof_passed'] = False
        self.event('run_finished', summary)
        self.json('summary.json', summary)
        cards = []
        for event in self.events:
            cards.append('<details><summary>' + html.escape(event['event_id'] + ' · ' +
                         event['kind'] + ' · ' + event['at_utc']) + '</summary><pre>' +
                         html.escape(json.dumps(event['details'], ensure_ascii=False, indent=2)) +
                         '</pre></details>')
        banner = ('OFFLINE TEST ONLY. No model or Gnani service was called.'
                  if self.mode == 'OFFLINE_TEST_ONLY' else
                  'Integration evidence. Fictional case data is labelled. This is not delivery proof.')
        self.write('trace.html', '<!doctype html><html lang="en"><meta charset="utf-8">'
                   '<meta name="viewport" content="width=device-width, initial-scale=1">'
                   '<title>SaunpDo integration trace</title><style>'
                   'body{font:16px/1.55 system-ui;max-width:1000px;margin:40px auto;padding:0 24px;'
                   'background:#f5f6f4;color:#182b2a}h1{font-size:32px}p{max-width:800px}'
                   'aside{padding:18px;background:#ffedbd;border-radius:10px}'
                   'details{margin:12px 0;background:white;border:1px solid #ddd;padding:16px;border-radius:10px}'
                   'summary{cursor:pointer;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;'
                   'font:13px/1.55 ui-monospace,monospace}</style><h1>SaunpDo · Phase 2</h1><aside>' +
                   html.escape(banner) + '</aside><p>Open an event to inspect the visible action, '
                   'connector result, or error. Raw private artifacts are stored beside this report. '
                   'Review and redact before sharing.</p><pre>' +
                   html.escape(json.dumps(self.clean(summary), ensure_ascii=False, indent=2)) +
                   '</pre>' + ''.join(cards) + '</html>')
        return summary


def run_directory(base):
    return Path(base) / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
