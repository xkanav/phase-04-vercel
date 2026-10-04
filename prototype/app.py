"""Local, event-driven SaunpDo demo. Humans own consent; Gemini chooses actions."""
import base64, copy, hashlib, html, json, os, re, threading, time, zipfile, io
from datetime import datetime, timezone, timedelta
from pathlib import Path
from storage import Store, now, uid, packed
from schema_check import validate, Invalid, gemini_schema
from adapters.providers import Gemini, Gnani, Transport, IntegrationError, audio_info

ROOT=Path(__file__).resolve().parent
TERMINAL={'unresolved','declined','cancelled','completed_simulated'}
CHOICES={'invitation':['Accept invitation','Decline invitation'], 'availability':['I can receive','Not this parcel','I cannot receive']}
ALLOWED_WAIT={'buyer_granted','buyer_revoked','invitation_responded','availability_changed','receiver_audio_uploaded','receiver_text_received'}

class Rejected(ValueError):
    def __init__(self,code,message): self.code=code; super().__init__(message)

def safe_json(value): return json.dumps(value,ensure_ascii=False,indent=2)
def hash_bytes(value): return hashlib.sha256(value).hexdigest()

class Journal:
    """Phase 2 Transport interface, backed by the Phase 4 persistent event store."""
    def __init__(self,app,rid):
        self.app=app; self.rid=rid; self.root=app.data/'runs'/rid
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.mode='OFFLINE_TEST_ONLY' if app.model_factory else 'LIVE_API_WITH_FICTIONAL_CASE'
        self.secrets=tuple(v for k,v in app.settings.items() if k.endswith('API_KEY') and v)
    def clean(self,value):
        if isinstance(value,str):
            for secret in self.secrets: value=value.replace(secret,'[REDACTED]')
        elif isinstance(value,dict): value={k:self.clean(v) for k,v in value.items()}
        elif isinstance(value,list): value=[self.clean(v) for v in value]
        return value
    def write(self,name,body):
        path=self.root/name
        if not path.resolve().is_relative_to(self.root.resolve()): raise ValueError('Invalid artifact path')
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if isinstance(body,str): body=self.clean(body).encode()
        for secret in self.secrets: body=body.replace(secret.encode(),b'[REDACTED]')
        tmp=path.with_name(path.name+'.tmp'); tmp.write_bytes(body); tmp.chmod(0o600); tmp.replace(path)
        return {'path':name,'sha256':hash_bytes(body),'bytes':len(body)}
    def json(self,name,value): return self.write(name,safe_json(self.clean(value))+'\n')
    def event(self,kind,payload):
        provider=payload.get('provider','runtime')
        return self.app.store.log(self.rid,kind,self.clean(payload),actor='provider' if kind=='connector_response' else 'runtime',source='LIVE_API' if kind.startswith('connector_') else 'APP_INTERNAL')

class App:
    def __init__(self,data,project,auto_worker=True,model_factory=None):
        self.data=Path(data); self.project=Path(project); self.data.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.store=Store(self.data/'saunpdo.sqlite3')
        self.case_template=json.loads((ROOT/'config/case.json').read_text())
        self.specs={t['name']:t for t in json.loads((ROOT/'config/tools.json').read_text())}
        self.declarations=[{'name':t['name'],'description':t['description'],'parameters':gemini_schema(t['parameters'])} for t in self.specs.values()]
        self.prompt=(ROOT/'config/system-prompt.md').read_text()
        self.settings={}
        env=self.project/'phase-02/.env'
        if env.exists():
            for line in env.read_text().splitlines():
                name,sep,value=line.partition('=')
                if sep and name in {'GEMINI_API_KEY','GNANI_API_KEY','GEMINI_MODEL'}: self.settings[name]=value.strip().strip('\"\'')
        for key in ['GEMINI_API_KEY','GNANI_API_KEY','GEMINI_MODEL']:
            if os.environ.get(key): self.settings[key]=os.environ[key]
        self.settings.setdefault('GEMINI_MODEL','gemini-3.8-flash')
        self.model_factory=model_factory
        self.providers={}; self.processing=threading.Lock(); self.stop=threading.Event(); self.wake=threading.Event()
        self.tokens={role:uid('session') for role in ('buyer','receiver','presenter')}
        self.register_samples()
        with self.store.tx():
            interrupted=self.store.db.execute("SELECT id FROM runs WHERE status='running'").fetchall()
            for row in interrupted: self.halt(row['id'],'Process stopped during an action. The result is uncertain; no automatic replay was attempted.')
        if auto_worker:
            self.worker=threading.Thread(target=self.loop,daemon=True); self.worker.start()

    def register_samples(self):
        for name,label in [('receiver-question.m4a','Your question recording'),('receiver-refusal.m4a','Your refusal recording')]:
            source=self.project/name
            if not source.exists(): continue
            info=audio_info(source); asset='sample-'+info['sha256'][:16]
            target=self.data/'audio'/f'{asset}.m4a'; target.parent.mkdir(exist_ok=True,mode=0o700)
            if not target.exists(): target.write_bytes(source.read_bytes()); target.chmod(0o600)
            with self.store.tx():
                self.store.db.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?,?,?,?,?)',(asset,None,name,str(target.relative_to(self.data)),info['sha256'],info['duration_seconds'],'USER_SUPPLIED_RECORDING'))

    def new_run(self,mode='practice'):
        if mode not in ('practice','participant'): raise Rejected('BAD_MODE','Choose a practice or participant run.')
        rid=uid('run'); state=copy.deepcopy(self.case_template)
        state.update({'last_human_seq':0,'case_state':'not_started','profile':'coordination_only','outcome':None,'reason_code':None,'unresolved_input_event_ids':[]})
        with self.store.tx():
            self.store.db.execute('INSERT INTO runs(id,created,mode,state,status,contents) VALUES(?,?,?,?,?,?)',(rid,now(),mode,packed(state),'idle','[]'))
            self.store.log(rid,'run_started',{'profile':'coordination_only','interaction_mode':mode,'case_source':'FICTIONAL_DEVELOPMENT_FIXTURE','carrier_integration':False,'model':self.settings['GEMINI_MODEL'],'prompt_sha256':hash_bytes(self.prompt.encode()),'audio_processing_agreement':'User attestation recorded in Phase 2 inputs.'})
        j=Journal(self,rid)
        j.write('system-prompt.md',self.prompt); j.json('tool-definitions.json',list(self.specs.values())); j.json('provider-tool-definitions.json',self.declarations); j.json('initial-case.json',state)
        j.json('configuration.json',{'profile':'coordination_only','interaction_mode':mode,'model':self.settings['GEMINI_MODEL'],'source':'FICTIONAL_DEVELOPMENT_FIXTURE','generation_config':{'temperature':1.0,'maxOutputTokens':8192,'thinkingConfig':{'thinkingLevel':'LOW'}},'max_model_turns_per_event':8,'max_tool_calls_per_event':12,'max_model_calls_per_run':40,'max_consecutive_clock_wakes':2,'source_hashes':{str(p.relative_to(ROOT)):hash_bytes(p.read_bytes()) for p in ROOT.glob('*.py')}})
        return rid

    def assert_open(self,rid):
        row=self.store.row(rid)
        if row['status'] in ('closed','halted'): raise Rejected('RUN_ENDED','This run has ended. Start a new run to try again.')
        return row

    def valid_grant(self,state):
        g=state.get('buyer_grant')
        return bool(g and not g['revoked_at'] and g['expires_at']>now() and g['starts_at']<=now() and g['receiver_id']==state['receiver']['id'] and g['parcel_id']==state['order']['parcel_id'] and g['value_cap_inr']>=state['order']['value_inr'])

    def action(self,rid,role,kind,args,key,expected):
        if role not in ('buyer','receiver') or not isinstance(args,dict): raise Rejected('BAD_ACTOR','Use the correct participant view.')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',key): raise Rejected('BAD_REQUEST_ID','The action needs a valid request ID.')
        signature=hash_bytes(packed([role,kind,args,expected]).encode()); dedupe_key='human:'+key
        with self.store.tx():
            cached=self.store.cache_get(rid,dedupe_key,'dedupe')
            if cached:
                if cached['signature']!=signature: raise Rejected('IDEMPOTENCY_CONFLICT','This request ID was already used for a different action.')
                return cached['result']
            row=self.assert_open(rid); s=json.loads(row['state']); old=s['state_version']
            if type(expected)!=int or expected!=old: raise Rejected('STALE_STATE','The case changed. Refresh the view and try again.')
            source='LIVE_HUMAN_ACTION' if row['mode']=='participant' else 'OPERATOR_TEST_ACTION'
            payload={}; event_kind=kind
            if kind=='grant' and role=='buyer':
                if set(args)!={'minutes','confirmed'} or args['confirmed'] is not True or type(args['minutes'])!=int or args['minutes'] not in (15,30,60): raise Rejected('INVALID_GRANT','Confirm this parcel and choose a valid duration.')
                if s['buyer_grant'] is not None: raise Rejected('GRANT_ALREADY_EXISTS','This run already has a grant. Revoke it or start a new run.')
                instant=datetime.now(timezone.utc)
                g={'grant_id':uid('grant'),'version':1,'buyer_id':s['buyer']['id'],'receiver_id':s['receiver']['id'],'order_id':s['order']['reference'],'parcel_id':s['order']['parcel_id'],'address_id':s['order']['address_reference'],'scope':'receive_only','value_cap_inr':s['order']['value_inr'],'starts_at':instant.isoformat(),'expires_at':(instant+timedelta(minutes=args['minutes'])).isoformat(),'revoked_at':None,'source_event_id':None}
                s['buyer_grant']=g; s['case_state']='awaiting_receiver'; payload={'grant':g,'request':'Help my mother understand this test parcel and answer her questions. Carrier release support is unavailable.'}; event_kind='buyer_granted'
            elif kind=='revoke' and role=='buyer':
                if args or not s['buyer_grant'] or s['buyer_grant']['revoked_at']: raise Rejected('NO_ACTIVE_GRANT','There is no active permission to withdraw.')
                s['buyer_grant']['revoked_at']=now(); s['buyer_grant']['version']+=1
                payload={'grant_id':s['buyer_grant']['grant_id'],'grant_version':s['buyer_grant']['version']}; event_kind='buyer_revoked'
            elif kind=='respond' and role=='receiver':
                if set(args)!={'message_id','choice'}: raise Rejected('INVALID_RESPONSE','Use an available response button.')
                m=self.store.db.execute('SELECT * FROM messages WHERE id=? AND run=?',(args['message_id'],rid)).fetchone()
                if not m or m['recipient']!=s['receiver']['id'] or m['response_kind'] not in CHOICES or args['choice'] not in CHOICES[m['response_kind']]: raise Rejected('INVALID_RESPONSE','This response does not match an active question.')
                if m['answered']: raise Rejected('ALREADY_ANSWERED','This question has already been answered.')
                if not self.valid_grant(s): raise Rejected('GRANT_UNAVAILABLE','This permission is expired or withdrawn.')
                if m['response_kind']=='availability' and (not s['invitation'] or s['invitation']['choice']!='Accept invitation'): raise Rejected('INVITATION_REQUIRED','Respond to the invitation first.')
                self.store.db.execute('UPDATE messages SET answered=? WHERE id=?',(args['choice'],m['id']))
                record={'choice':args['choice'],'event_id':None,'recorded_at':now()}
                field='invitation' if m['response_kind']=='invitation' else 'receiver_availability'
                s[field]=record; s['case_state']='coordinating'; event_kind='invitation_responded' if field=='invitation' else 'availability_changed'; payload={'choice':args['choice'],'message_id':m['id']}
            elif kind=='availability' and role=='receiver':
                if set(args)!={'choice'} or args['choice'] not in CHOICES['availability']: raise Rejected('INVALID_RESPONSE','Choose one of the three availability options.')
                if not self.valid_grant(s): raise Rejected('GRANT_UNAVAILABLE','This permission is expired or withdrawn.')
                if not s['invitation'] or s['invitation']['choice']!='Accept invitation': raise Rejected('INVITATION_REQUIRED','Respond to the invitation first.')
                if s['receiver_availability']: raise Rejected('ALREADY_ANSWERED','Availability is already recorded. You may still withdraw it.')
                s['receiver_availability']={'choice':args['choice'],'event_id':None,'recorded_at':now()}
                s['case_state']='coordinating'; payload={'choice':args['choice'],'interface':'receiver_availability_form'}; event_kind='availability_changed'
                self.store.db.execute("UPDATE messages SET answered=? WHERE run=? AND response_kind='availability' AND answered IS NULL",(args['choice'],rid))
            elif kind=='withdraw' and role=='receiver':
                if args or not s.get('receiver_availability') or s['receiver_availability']['choice']!='I can receive': raise Rejected('NO_AVAILABILITY','There is no availability confirmation to withdraw.')
                s['receiver_availability']={'choice':'I cannot receive','event_id':None,'recorded_at':now()}; payload={'choice':'I cannot receive','previous_availability_nonbinding':True}; event_kind='availability_changed'
            elif kind=='audio' and role=='receiver':
                if set(args)!={'audio_id'}: raise Rejected('INVALID_AUDIO','Choose an audio recording.')
                asset=self.store.db.execute('SELECT * FROM assets WHERE id=?',(args['audio_id'],)).fetchone()
                if not asset or asset['run'] not in (None,rid): raise Rejected('UNKNOWN_AUDIO','That recording is not registered for this run.')
                payload={'audio_id':asset['id'],'filename':asset['name'],'sha256':asset['sha'],'duration_seconds':asset['duration'],'recording_source':asset['source'],'transcript':None}; event_kind='receiver_audio_uploaded'
                if s.get('doorstep_willingness'): s['doorstep_willingness']=None
            elif kind=='text' and role=='receiver':
                if set(args)!={'text'} or not isinstance(args['text'],str) or not 1<=len(args['text'].strip())<=2000: raise Rejected('INVALID_TEXT','Write a message of 1 to 2,000 characters.')
                payload={'text':args['text'].strip()}; event_kind='receiver_text_received'
                if s.get('doorstep_willingness'): s['doorstep_willingness']=None
            else: raise Rejected('ACTION_NOT_AVAILABLE','This action is not available in this demo.')
            s['state_version']+=1; self.store.save_state(rid,s)
            event=self.store.log(rid,event_kind,payload,actor=role,source=source,incoming=True,before=old,after=s['state_version'])
            if event_kind=='buyer_granted': s['buyer_grant']['source_event_id']=event['event_id']
            if event_kind in ('invitation_responded','availability_changed'):
                field='invitation' if event_kind=='invitation_responded' else 'receiver_availability'; s[field]['event_id']=event['event_id']
            if event_kind in ('receiver_audio_uploaded','receiver_text_received'): s['unresolved_input_event_ids'].append(event['event_id'])
            s['last_human_seq']=event['sequence']; self.store.save_state(rid,s)
            self.store.db.execute("UPDATE runs SET waiting=NULL,clock_wakes=0,status=CASE WHEN status='waiting' THEN 'idle' ELSE status END WHERE id=?",(rid,))
            result={'ok':True,'event_id':event['event_id'],'state_version':s['state_version']}
            self.store.cache_set(rid,dedupe_key,{'signature':signature,'result':result},'dedupe')
        self.wake.set(); return result

    def upload(self,rid,name,encoded):
        self.assert_open(rid)
        suffix=Path(name).suffix.lower()
        if suffix not in ('.m4a','.mp3','.wav','.aac','.ogg','.flac'): raise Rejected('AUDIO_FORMAT','Use M4A, MP3 or WAV audio.')
        try: body=base64.b64decode(encoded,validate=True)
        except Exception: raise Rejected('AUDIO_FORMAT','The audio upload could not be read.')
        if not 0<len(body)<=20*1024*1024: raise Rejected('AUDIO_SIZE','Use an audio file below 20 MB.')
        asset=uid('audio'); path=self.data/'audio'/(asset+suffix); path.parent.mkdir(exist_ok=True,mode=0o700); path.write_bytes(body); path.chmod(0o600)
        try: info=audio_info(path)
        except IntegrationError as exc: path.unlink(); raise Rejected('AUDIO_FORMAT',str(exc))
        with self.store.tx(): self.store.db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?)',(asset,rid,Path(name).name,str(path.relative_to(self.data)),info['sha256'],info['duration_seconds'],'PARTICIPANT_UPLOAD'))
        return {'audio_id':asset,'name':Path(name).name,'duration':info['duration_seconds']}

    def provider(self,rid):
        if rid not in self.providers:
            journal=Journal(self,rid); transport=Transport(journal)
            nums=[int(p.name[1:4]) for p in (journal.root/'private').glob('C???-request.json')]
            transport.count=max(nums,default=0)
            model=Gemini(self.settings.get('GEMINI_API_KEY',''),self.settings['GEMINI_MODEL'],transport)
            voice=Gnani(self.settings.get('GNANI_API_KEY',''),transport)
            if self.model_factory: model,voice=self.model_factory(journal)
            self.providers[rid]=(model,voice,journal)
        return self.providers[rid]

    def tool(self,rid,name,args,call_id,trigger_seq):
        if name not in self.specs: raise Rejected('TOOL_DISABLED','This capability is not implemented or enabled.')
        try: validate(args,self.specs[name]['parameters'])
        except Invalid as exc: raise Rejected('INVALID_ARGUMENTS',str(exc))
        if args['case_id']!=self.case_template['case_id']: raise Rejected('WRONG_CASE','Use this run\'s case ID.')
        # Network calls never hold the database lock. Every consequential write rechecks state.
        if name=='transcribe_receiver_audio':
            with self.store.tx():
                self.assert_open(rid)
                row=self.store.db.execute('SELECT * FROM assets WHERE id=?',(args['audio_id'],)).fetchone()
                registered=[e for e in self.store.events(rid,incoming=True) if e['event_type']=='receiver_audio_uploaded' and e['payload']['audio_id']==args['audio_id']]
                if not row or row['run'] not in (None,rid) or not registered: raise Rejected('UNKNOWN_AUDIO','Audio must arrive as a receiver event first.')
                key='stt:'+row['sha']; cached=self.store.cache_get(rid,key)
                if cached: return {**cached,'cache_reused':True,'new_provider_call':False}
                path=self.data/row['path']; original_call=call_id
            _,voice,journal=self.provider(rid)
            journal.write('inputs/'+args['audio_id']+path.suffix,path.read_bytes())
            try: result={'api_result':voice.transcribe(path),'original_tool_call_id':original_call,'cache_reused':False}
            except IntegrationError as exc: result={'error':{'code':'VOICE_UNAVAILABLE','message':str(exc)},'original_tool_call_id':original_call,'cache_reused':False}
            with self.store.tx(): self.store.cache_set(rid,key,result)
            return result
        with self.store.tx():
            row=self.assert_open(rid); s=json.loads(row['state']); version=s['state_version']
            if name=='get_parcel_context':
                return {'case':s,'state_version':version,'run_profile':'coordination_only','messages':self.store.messages(rid),'source_label':'Fictional order; participant actions labelled separately','pending_inputs':self.store.events(rid,row['cursor'],incoming=True),'evidence_refs':[e['event_id'] for e in self.store.events(rid)],'grant_valid_now':self.valid_grant(s),'now':now()}
            if 'expected_state_version' in args and args['expected_state_version']!=version: raise Rejected('STALE_STATE','The case changed. Read the current context before acting.')
            semantic={k:v for k,v in args.items() if k not in ('rule_id','reason')}
            key='tool:'+name+':'+hash_bytes(packed(semantic).encode())
            if name=='send_message':
                cached=self.store.cache_get(rid,key)
                if cached: return {**cached,'duplicate':True}
                if args['recipient_id'] not in (s['buyer']['id'],s['receiver']['id']): raise Rejected('RECIPIENT_NOT_ENROLLED','Only this buyer and receiver can receive messages.')
                kind=args['response_kind']
                if kind not in ('none','text','invitation','availability') or args.get('attempt_id') is not None: raise Rejected('CARRIER_UNAVAILABLE','Doorstep and receipt controls are disabled in coordination mode.')
                if kind in CHOICES:
                    if args['recipient_id']!=s['receiver']['id'] or not self.valid_grant(s): raise Rejected('INVALID_RESPONSE_CONTROL','An active grant and the named receiver are required.')
                    if kind=='invitation' and s['invitation']: raise Rejected('ALREADY_ANSWERED','Invitation already answered.')
                    if kind=='availability' and (not s['invitation'] or s['invitation']['choice']!='Accept invitation'): raise Rejected('INVITATION_REQUIRED','Invitation acceptance is needed before availability.')
                    if kind=='availability' and s['receiver_availability']: raise Rejected('ALREADY_ANSWERED','Availability is already recorded. The receiver can withdraw it.')
                if kind=='text' and args['recipient_id']!=s['receiver']['id']: raise Rejected('TEXT_CHANNEL_UNAVAILABLE','Only the receiver has a free-text conversation in this demo.')
                mid=uid('message'); instant=now()
                ev=self.store.log(rid,'message_stored',{'message_id':mid,'recipient_id':args['recipient_id'],'text':args['text'],'purpose':args['purpose'],'response_kind':kind,'choices':CHOICES.get(kind,[]),'delivery_status':'stored_in_app','tool_call_id':call_id},actor='model')
                self.store.db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(mid,rid,args['recipient_id'],args['text'],args['purpose'],kind,instant,version,None,None,ev['event_id'],trigger_seq))
                result={'message_id':mid,'persisted_at':instant,'delivery_status':'stored_in_app','read_at':None,'evidence_ref':ev['event_id'],'choices':CHOICES.get(kind,[])}
                self.store.cache_set(rid,key,result); return result
            if name=='wait_for_event':
                if not set(args['event_types']).issubset(ALLOWED_WAIT): raise Rejected('EVENT_UNAVAILABLE','Choose event types available in coordination mode.')
                pending=self.store.events(rid,row['cursor'],incoming=True)
                deadline=(datetime.now(timezone.utc)+timedelta(seconds=args['timeout_seconds'])).isoformat()
                wait={'event_types':args['event_types'],'deadline':deadline,'requested_by':call_id}
                self.store.db.execute('UPDATE runs SET waiting=? WHERE id=?',(packed(wait),rid))
                ev=self.store.log(rid,'wait_registered',{**wait,'pending_inputs':len(pending)})
                return {'waiting':True,'deadline':deadline,'queued_input_will_resume':bool(pending),'evidence_ref':ev['event_id']}
            if name=='close_case':
                if args['outcome']=='completed_simulated': raise Rejected('CARRIER_DISABLED','No delivery completion is possible in this mode.')
                pending=self.store.events(rid,row['cursor'],incoming=True)
                if pending: raise Rejected('PENDING_INPUT','New input arrived. Consume it before closing.')
                known={e['event_id'] for e in self.store.events(rid)}|{m['id'] for m in self.store.messages(rid)}
                if any(ref not in known for ref in args['evidence_refs']): raise Rejected('UNKNOWN_EVIDENCE','Use event or message IDs from this run.')
                if args['outcome']=='cancelled' and not (s['buyer_grant'] and s['buyer_grant']['revoked_at']): raise Rejected('NOT_REVOKED','No buyer revocation supports this outcome.')
                negative=bool((s['invitation'] and s['invitation']['choice']=='Decline invitation') or (s['receiver_availability'] and s['receiver_availability']['choice'] in ('I cannot receive','Not this parcel')))
                # Audio/text refusal is the model's interpretation, never a positive consent write.
                has_receiver_input=any(e['event_type'] in ('receiver_audio_uploaded','receiver_text_received') for e in self.store.events(rid,incoming=True))
                if args['outcome']=='declined' and not negative and not has_receiver_input: raise Rejected('NO_REFUSAL_INPUT','No receiver input supports a declined outcome.')
                recipients={s['buyer']['id']}
                if s['invitation'] or has_receiver_input: recipients.add(s['receiver']['id'])
                notices=[m for m in self.store.messages(rid) if m['trigger_seq']>=s['last_human_seq'] and m['response_kind']=='none' and m['purpose'] in ('update','explain')]
                if not recipients.issubset({m['recipient'] for m in notices}): raise Rejected('NOTIFICATIONS_REQUIRED','Store an outcome update for the buyer and any participating receiver before closing.')
                s['outcome']=args['outcome']; s['reason_code']=args['reason_code']; s['case_state']=args['outcome']; s['state_version']+=1
                self.store.save_state(rid,s); self.store.db.execute("UPDATE runs SET status='closed',waiting=NULL WHERE id=?",(rid,))
                ev=self.store.log(rid,'case_closed',{'outcome':args['outcome'],'reason_code':args['reason_code'],'evidence_refs':args['evidence_refs'],'delivery_completed':False},actor='model',before=version,after=s['state_version'])
                return {'closed':True,'outcome':args['outcome'],'evidence_ref':ev['event_id'],'delivery_completed':False}
        raise Rejected('TOOL_DISABLED','Tool unavailable.')

    def halt(self,rid,message):
        with self.store.tx():
            if self.store.row(rid)['status']=='closed': return
            self.store.db.execute("UPDATE runs SET status='halted',failure=?,waiting=NULL WHERE id=?",(message,rid))
            self.store.log(rid,'technical_halt',{'message':message,'agent_outcome_claimed':False})

    def tick(self):
        with self.store.tx():
            rows=self.store.db.execute("SELECT * FROM runs WHERE status IN ('idle','waiting') ORDER BY created").fetchall()
            for row in rows:
                pending=self.store.events(row['id'],row['cursor'],incoming=True)
                if pending: return row['id']
                if row['waiting'] and json.loads(row['waiting'])['deadline']<=now():
                    if row['clock_wakes']>=2: self.halt(row['id'],'Paused after two unattended timeouts. Start a new run when participants are ready.'); continue
                    self.store.log(row['id'],'timer_expired',{'deadline':json.loads(row['waiting'])['deadline']},source='APP_CLOCK',incoming=True)
                    self.store.db.execute('UPDATE runs SET clock_wakes=clock_wakes+1,waiting=NULL WHERE id=?',(row['id'],)); return row['id']
        return None

    def process_one(self,rid):
        if not self.processing.acquire(blocking=False): return
        try:
            with self.store.tx():
                row=self.assert_open(rid); events=self.store.events(rid,row['cursor'],incoming=True)
                if not events: return
                cursor=events[-1]['sequence']; contents=json.loads(row['contents'])
                contents.append({'role':'user','parts':[{'text':packed({'run_profile':'coordination_only','interaction_mode':row['mode'],'case_id':self.case_template['case_id'],'events':events,'available_tools':list(self.specs),'now':now()})}]})
                self.store.db.execute("UPDATE runs SET status='running',cursor=?,waiting=NULL,contents=? WHERE id=?",(cursor,packed(contents),rid))
            if not self.model_factory and not all(self.settings.get(k) for k in ('GEMINI_API_KEY','GNANI_API_KEY')): raise IntegrationError('Configure Gemini and Gnani keys before a live run.')
            model,voice,journal=self.provider(rid); tool_count=0; stopping=False
            for turn in range(8):
                with self.store.tx():
                    if self.store.row(rid)['model_calls']>=40: raise IntegrationError('The run reached its 40-call limit; no outcome was invented.')
                    self.store.db.execute('UPDATE runs SET model_calls=model_calls+1 WHERE id=?',(rid,))
                    call_index=self.store.row(rid)['model_calls']
                content,metadata=model.generate(contents,self.prompt,self.declarations)
                contents.append(copy.deepcopy(content))
                model_ref=journal.json(f'model/turn-{call_index:03}.json',{'content':content,'metadata':metadata})
                self.store.log(rid,'model_output',{'turn':call_index,**metadata,'response_ref':model_ref,'function_names':[p['functionCall'].get('name') for p in content['parts'] if 'functionCall' in p]},actor='model',source='LIVE_API' if not self.model_factory else 'OFFLINE_TEST_ONLY')
                calls=[p['functionCall'] for p in content['parts'] if 'functionCall' in p]
                if not calls:
                    note=''.join(p.get('text','') for p in content['parts'] if not p.get('thought'))
                    self.store.log(rid,'presenter_note',{'exact_text':note,'not_a_delivered_message':True},actor='model')
                    raise IntegrationError('The model stopped without requesting wait or close. Its exact output is saved; no action was invented.')
                responses=[]; batch_failed=False
                for call in calls:
                    tool_count+=1; cid=uid('tool'); name=call.get('name'); args=call.get('args',{})
                    decision={'decision_id':uid('D'),'run_id':rid,'case_id':self.case_template['case_id'],'decided_at':now(),'trigger_event_ids':[e['event_id'] for e in events],'input_exact_refs':[e['event_id'] for e in events],'source_refs':[e['source_mode']+':'+e['event_id'] for e in events],'incoming_connectors':['SaunpDo participant app' if e['actor'] in ('buyer','receiver') else 'App clock' for e in events],'decision_summary':name,'rule_id':args.get('rule_id') if isinstance(args,dict) else None,'reason':args.get('reason') if isinstance(args,dict) else None,'kind':'tool_call','tool_name':name,'tool_arguments':args,'call_id':cid,'action_or_message_exact':args.get('text',packed(args)) if isinstance(args,dict) else packed(args),'recipient_ids':[args['recipient_id']] if isinstance(args,dict) and 'recipient_id' in args else [],'outgoing_connector':'Gnani STT' if name=='transcribe_receiver_audio' else 'SaunpDo local app','context_state_version':self.store.state(rid)['state_version'],'result_event_ids':[],'result_refs':[],'execution_status':'requested','model_response_ref':model_ref['path']}
                    decision['trigger_inputs_exact']=events
                    decision['preceding_tool_result_event_ids']=[e['event_id'] for e in self.store.events(rid) if e['event_type']=='tool_result']
                    self.store.put_decision(rid,decision)
                    self.store.log(rid,'model_tool_request',{'decision_id':decision['decision_id'],'tool_call_id':cid,'name':name,'arguments':args},actor='model')
                    try:
                        if batch_failed: raise Rejected('PRIOR_TOOL_FAILED','An earlier tool in this batch failed. Inspect its result before deciding what to do next.')
                        if stopping: raise Rejected('AFTER_STOP','A wait or close already ended this batch.')
                        if tool_count>12: raise Rejected('TOOL_LIMIT','Maximum tool calls reached for this input.')
                        result=self.tool(rid,name,args,cid,cursor)
                        ok=not bool(result.get('error'))
                        envelope={'ok':ok,'call_id':cid,'state_version':self.store.state(rid)['state_version'],'result':result}
                        decision['execution_status']='succeeded' if ok else 'failed'
                        if not ok: batch_failed=True
                        if ok and name in ('wait_for_event','close_case'): stopping=True
                    except Rejected as exc:
                        batch_failed=True
                        envelope={'ok':False,'call_id':cid,'state_version':self.store.state(rid)['state_version'],'error':{'code':exc.code,'message':str(exc)}}; decision['execution_status']='rejected'
                    ev=self.store.log(rid,'tool_result',{'tool_call_id':cid,'tool_name':name,'response':envelope})
                    decision['result_event_ids']=[ev['event_id']]; decision['result_refs']=[ev['event_id']]; self.store.put_decision(rid,decision)
                    response={'name':name,'response':envelope}
                    if 'id' in call: response['id']=call['id']
                    responses.append({'functionResponse':response})
                contents.append({'role':'user','parts':responses})
                with self.store.tx(): self.store.db.execute('UPDATE runs SET contents=? WHERE id=?',(packed(contents),rid))
                journal.json('private/conversation.json',contents)
                # Yield at a tool boundary when new input arrived during the model call.
                # The next worker pass adds that actual event to the saved conversation.
                # This prevents a close/read loop over an unconsumed, queued refusal.
                with self.store.tx():
                    row=self.store.row(rid)
                    if row['status']!='closed' and self.store.events(rid,row['cursor'],incoming=True):
                        self.store.db.execute("UPDATE runs SET status='idle',waiting=NULL WHERE id=?",(rid,))
                        return
                if tool_count>12: raise IntegrationError('Tool limit reached; exact requests retained.')
                if stopping:
                    with self.store.tx():
                        row=self.store.row(rid)
                        if row['status']!='closed': self.store.db.execute("UPDATE runs SET status='waiting' WHERE id=?",(rid,))
                    return
            raise IntegrationError('Model turn limit reached; no successful outcome was claimed.')
        except (IntegrationError,ValueError,KeyError,TypeError,OSError) as exc:
            self.halt(rid,str(exc) if isinstance(exc,IntegrationError) else 'A local validation/storage error stopped the run. Inspect the saved trace.')
        finally:
            self.processing.release()
            self.export(rid)

    def loop(self):
        while not self.stop.is_set():
            try:
                rid=self.tick()
                if rid: self.process_one(rid); continue
            except Exception:
                # Unexpected scheduler errors are not converted into agent outcomes.
                pass
            self.wake.wait(0.5); self.wake.clear()

    def view(self,rid=None):
        with self.store.lock:
            runs=[dict(r) for r in self.store.db.execute('SELECT id,created,mode,status FROM runs ORDER BY created DESC')]
            samples=[{'id':r['id'],'name':r['name'],'duration':r['duration']} for r in self.store.db.execute('SELECT * FROM assets WHERE run IS NULL')]
        result={'runs':runs,'samples':samples,'model':self.settings['GEMINI_MODEL'],'keys_ready':all(self.settings.get(k) for k in ('GEMINI_API_KEY','GNANI_API_KEY')),'profile':'coordination_only'}
        if rid:
            row=self.store.row(rid)
            result.update({'run':{k:v for k,v in row.items() if k not in ('contents','state')},'case':json.loads(row['state']),'messages':self.store.messages(rid),'events':self.store.events(rid),'decisions':self.store.decisions(rid)})
        return result

    def read_message(self,rid,role,mid):
        if role not in ('buyer','receiver'): raise Rejected('BAD_ROLE','Use a participant view.')
        with self.store.tx():
            s=self.store.state(rid); row=self.store.db.execute('SELECT * FROM messages WHERE id=? AND run=?',(mid,rid)).fetchone()
            if not row or row['recipient']!=s[role]['id']: raise Rejected('WRONG_RECIPIENT','This message belongs to the other participant.')
            if not row['read_at']:
                self.store.db.execute('UPDATE messages SET read_at=? WHERE id=?',(now(),mid)); self.store.log(rid,'message_viewed',{'message_id':mid,'meaning':'Message rendered in this role view, not proof of comprehension.'},actor=role,source='APP_INTERNAL')
        return {'ok':True}

    def export(self,rid):
        with self.store.lock:
            row=self.store.row(rid); events=self.store.events(rid); decisions=self.store.decisions(rid); messages=self.store.messages(rid)
            data={'run_id':rid,'profile':'coordination_only','interaction_mode':row['mode'],'status':row['status'],'case':json.loads(row['state']),'model':self.settings['GEMINI_MODEL'],'model_calls':row['model_calls'],'failure':row['failure'],'delivery_completed':False,'exported_at':now()}
        j=Journal(self,rid); j.json('summary.json',data); j.json('decisions.json',decisions); j.json('messages.json',messages); j.write('events.jsonl',''.join(packed(e)+'\n' for e in events))
        lines=['# SaunpDo decision ledger','', 'Recorded model choices. This is a fictional order scenario; practice clicks are labelled OPERATOR_TEST_ACTION.', '']
        for d in decisions:
            lines.extend([f"## {d['decision_id']} · {d['tool_name']}",'',f"- When: {d['decided_at']}",f"- Trigger: {', '.join(d['trigger_event_ids'])}",f"- Source and connector: {', '.join(d['source_refs'])}; {', '.join(d['incoming_connectors'])}",f"- Decision: {d['decision_summary']}",f"- Why: {d['rule_id']}: {d['reason']}",f"- Recipient: {', '.join(d['recipient_ids']) or 'Internal tool'}",f"- Through: {d['outgoing_connector']}",f"- Result: {d['execution_status']}",'','Exact action/message:','','```json',d['action_or_message_exact'],'```','','Exact triggering input:','','```json',safe_json(d.get('trigger_inputs_exact',[e for e in events if e['event_id'] in d['trigger_event_ids']])),'```','','Earlier tool results available in this decision context: '+', '.join(d.get('preceding_tool_result_event_ids',[])),''])
        j.write('decision-ledger.md','\n'.join(lines))
        cards=''.join('<details><summary>'+html.escape(e['received_at']+' · '+e['event_type']+' · '+e['actor'])+'</summary><pre>'+html.escape(safe_json(e))+'</pre></details>' for e in events)
        j.write('trace.html','<!doctype html><meta charset="utf-8"><title>SaunpDo run evidence</title><style>body{font:16px/1.6 system-ui;background:#faf9f5;color:#193d35;max-width:1100px;margin:40px auto;padding:24px}aside{background:#eee7d4;padding:16px;border-radius:12px}details{background:white;padding:16px;margin:12px 0;border:1px solid #ddd;border-radius:12px}summary{cursor:pointer}pre{font:13px/1.6 monospace;white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>SaunpDo · Run evidence</h1><aside>Fictional order. '+html.escape(row['mode'])+' interaction. Real API responses where labelled. No carrier release or delivery completion.</aside><h2>Outcome</h2><pre>'+html.escape(safe_json(data))+'</pre>'+cards)
        return j.root

    def export_zip(self,rid):
        folder=self.export(rid); out=io.BytesIO()
        with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
            for p in folder.rglob('*'):
                if p.is_file() and not p.name.endswith('.tmp'): z.write(p,str(p.relative_to(folder)))
        return out.getvalue()
