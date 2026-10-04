"""Documented REST adapters. No fallback to fake responses."""
from pathlib import Path
import io
import json
import mimetypes
import re
import subprocess
import time
import wave
import requests
from .evidence import digest

STT_URL = 'https://api.vachana.ai/stt/v3'
TTS_URL = 'https://api.vachana.ai/api/v1/tts/inference'
GEMINI_BASE = 'https://generativelanguage.googleapis.com/v1beta'


class IntegrationError(Exception):
    pass


def audio_info(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise IntegrationError('Audio file not found. Supply a real participant recording.')
    if path.suffix.lower() not in {'.wav', '.mp3', '.ogg', '.flac', '.aac', '.m4a'}:
        raise IntegrationError('Unsupported audio suffix. Use WAV, M4A, MP3, OGG, FLAC or AAC.')
    if not 0 < path.stat().st_size <= 20 * 1024 * 1024:
        raise IntegrationError('Audio must be nonempty and at most 20 MB for this proof runner.')
    duration = None
    if path.suffix.lower() == '.wav':
        try:
            with wave.open(str(path)) as handle:
                duration = handle.getnframes() / handle.getframerate()
        except (wave.Error, EOFError, ZeroDivisionError) as exc:
            raise IntegrationError('Unreadable PCM WAV. Export a standard WAV or M4A recording.') from exc
    elif Path('/usr/bin/afinfo').is_file():
        result = subprocess.run(['/usr/bin/afinfo', str(path)], capture_output=True,
                                text=True, timeout=10, check=False)
        match = re.search(r'estimated duration:\s*([0-9.]+)\s*sec', result.stdout)
        if match:
            duration = float(match.group(1))
    if duration is None:
        raise IntegrationError('Could not verify audio duration locally. Export a PCM WAV under 60 seconds.')
    if duration <= 0 or duration > 60:
        raise IntegrationError('Use an audio clip longer than zero and no more than 60 seconds.')
    data = path.read_bytes()
    return {'path': str(path), 'filename': path.name, 'duration_seconds': duration,
            'mime_type': mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
            'bytes': len(data), 'sha256': digest(data)}


class Transport:
    def __init__(self, evidence, session=None):
        self.evidence = evidence
        self.session = session or requests.Session()
        self.count = 0

    def request(self, provider, method, url, headers=None, payload=None, data=None, files=None):
        self.count += 1
        call_id = f'C{self.count:03}'
        request = requests.Request(method, url, headers=headers, json=payload, data=data, files=files)
        prepared = self.session.prepare_request(request)
        body = prepared.body or b''
        if isinstance(body, str):
            body = body.encode()
        if not isinstance(body, bytes):
            raise IntegrationError('Unexpected streaming body; exact request capture is required.')
        request_artifact = self.evidence.write(f'private/{call_id}-request.body', body)
        safe_headers = {k: ('[REDACTED]' if k.lower() in {'authorization', 'x-api-key-id', 'x-goog-api-key', 'cookie'} else v)
                        for k, v in prepared.headers.items()}
        self.evidence.json(f'private/{call_id}-request.json', {
            'provider': provider, 'method': method, 'url': prepared.url,
            'headers': safe_headers, 'body': request_artifact, 'json': payload,
            'form_fields': data})
        self.evidence.event('connector_request', {'call_id': call_id, 'provider': provider,
                            'method': method, 'url': prepared.url, 'body': request_artifact,
                            'json': payload if provider != 'Gemini' else None,
                            'form_fields': data})
        started = time.monotonic()
        try:
            response = self.session.send(prepared, timeout=(10, 60), allow_redirects=False)
        except requests.RequestException as exc:
            self.evidence.event('connector_transport_error', {'call_id': call_id,
                                'error_type': type(exc).__name__,
                                'elapsed_ms': round((time.monotonic()-started)*1000)})
            raise IntegrationError(f'{provider}: connection or timeout error; no response was fabricated.') from exc
        raw = response.content
        artifact = self.evidence.write(f'private/{call_id}-response.body', raw)
        metadata = {'call_id': call_id, 'provider': provider, 'status_code': response.status_code,
                    'elapsed_ms': round((time.monotonic()-started)*1000),
                    'content_type': response.headers.get('Content-Type', ''),
                    'response_body': artifact}
        try:
            metadata['json'] = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            pass
        self.evidence.json(f'private/{call_id}-response.json', metadata)
        visible = {k: v for k, v in metadata.items() if k != 'json' or provider != 'Gemini'}
        self.evidence.event('connector_response', visible)
        if not 200 <= response.status_code < 300:
            raise IntegrationError(f'{provider}: HTTP {response.status_code}. Raw response saved under {call_id}.')
        return raw, metadata


def object_response(raw, provider):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise IntegrationError(f'{provider}: response is not JSON; original bytes were saved.') from exc
    if not isinstance(value, dict):
        raise IntegrationError(f'{provider}: expected a JSON object; original response was saved.')
    return value


class Gnani:
    def __init__(self, key, transport):
        self.key = key
        self.transport = transport

    def transcribe(self, path):
        info = audio_info(path)
        with Path(path).open('rb') as handle:
            raw, metadata = self.transport.request('Gnani STT', 'POST', STT_URL,
                headers={'X-API-Key-ID': self.key}, data={'language_code': 'hi-IN', 'format': 'verbatim'},
                files={'audio_file': (info['filename'], handle, info['mime_type'])})
        body = object_response(raw, 'Gnani STT')
        if body.get('success') is not True or not isinstance(body.get('transcript'), str) or not body['transcript'].strip():
            raise IntegrationError('Gnani STT did not return a usable successful transcript. Exact response retained.')
        mode = 'OFFLINE_TEST_RESPONSE' if self.transport.evidence.mode == 'OFFLINE_TEST_ONLY' else 'LIVE_GNANI_API'
        return {'source_mode': mode, 'api_response': body,
                'call_id': metadata['call_id'], 'audio_sha256': info['sha256']}

    def speak(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 3000:
            raise IntegrationError('TTS requires a nonempty response of no more than 3,000 characters.')
        payload = {'text': text, 'model': 'timbre-v2.5', 'voice': 'Nalini', 'language': 'hi-IN',
                   'speed': 1.0, 'audio_config': {'sample_rate': 48000, 'num_channels': 1,
                   'sample_width': 2, 'encoding': 'linear_pcm', 'container': 'wav'}}
        raw, metadata = self.transport.request('Gnani TTS', 'POST', TTS_URL,
            headers={'X-API-Key-ID': self.key}, payload=payload)
        if not (raw[:4] == b'RIFF' and raw[8:12] == b'WAVE'):
            raise IntegrationError('Gnani TTS response is not the requested WAV audio. Original bytes retained.')
        try:
            with wave.open(io.BytesIO(raw)) as handle:
                if handle.getnframes() == 0:
                    raise IntegrationError('Gnani TTS returned an empty WAV.')
        except (wave.Error, EOFError) as exc:
            raise IntegrationError('Gnani TTS returned an unreadable WAV.') from exc
        return raw, metadata


class Gemini:
    def __init__(self, key, model, transport):
        if not re.fullmatch(r'gemini-[A-Za-z0-9._-]+', model):
            raise IntegrationError('Set GEMINI_MODEL to a valid Gemini model ID.')
        self.key, self.model, self.transport = key, model, transport

    def generate(self, contents, system_prompt, tools):
        payload = {'contents': contents, 'systemInstruction': {'parts': [{'text': system_prompt}]},
                   'tools': [{'functionDeclarations': tools}],
                   'toolConfig': {'functionCallingConfig': {'mode': 'AUTO'}},
                   'generationConfig': {'temperature': 1.0, 'maxOutputTokens': 8192, 'thinkingConfig': {'thinkingLevel': 'LOW'}}}
        raw, _ = self.transport.request('Gemini', 'POST',
            f'{GEMINI_BASE}/models/{self.model}:generateContent',
            headers={'x-goog-api-key': self.key}, payload=payload)
        result = object_response(raw, 'Gemini')
        candidates = result.get('candidates', [])
        if not candidates:
            raise IntegrationError('Gemini returned no candidate. Inspect the saved block/error response.')
        candidate = candidates[0]
        if candidate.get('finishReason') not in (None, 'STOP'):
            raise IntegrationError('Gemini response did not finish normally: ' + str(candidate.get('finishReason')))
        content = candidate.get('content')
        if not isinstance(content, dict) or content.get('role') != 'model' or not isinstance(content.get('parts'), list):
            raise IntegrationError('Gemini returned no usable model content.')
        return content, {'model_version': result.get('modelVersion'), 'usage': result.get('usageMetadata')}


def track_delhivery(waybill, token, transport, staging=True):
    if not re.fullmatch(r'[0-9]{6,30}', waybill):
        raise IntegrationError('Provide an actual authorized numeric waybill; fictional IDs cannot be sent.')
    host = 'staging-express.delhivery.com' if staging else 'track.delhivery.com'
    raw, metadata = transport.request('Delhivery tracking', 'GET',
        f'https://{host}/api/v1/packages/json/?waybill={waybill}&ref_ids=',
        headers={'Authorization': 'Token ' + token, 'Content-Type': 'application/json'})
    return object_response(raw, 'Delhivery tracking'), metadata
