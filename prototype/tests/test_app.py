"""Offline behavioural tests. No live provider or participant results are fabricated."""
import unittest, tempfile, json, sys, wave
from pathlib import Path
from datetime import datetime,timezone,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import App, Rejected
from storage import packed,uid
from adapters.providers import IntegrationError

class Voice:
    def __init__(self,fail=False): self.calls=0; self.fail=fail
    def transcribe(self,path):
        self.calls+=1
        if self.fail: raise IntegrationError('Offline injected provider failure')
        return {'source_mode':'OFFLINE_TEST_ONLY','api_response':{'success':True,'transcript':'constructed test transcript'}}

class Model:
    def generate(self,*args): raise AssertionError('Unexpected model call in this test')

class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)
        self.voice=Voice(); self.app=App(self.path/'data',self.path,auto_worker=False,model_factory=lambda j:(Model(),self.voice))
        self.rid=self.app.new_run(); self.case=self.app.store.state(self.rid)['case_id']
    def tearDown(self): self.app.stop.set(); self.app.store.db.close(); self.tmp.cleanup()
    def action(self,role,kind,args,version=None,key=None):
        return self.app.action(self.rid,role,kind,args,key or uid('request'),self.app.store.state(self.rid)['state_version'] if version is None else version)
    def grant(self): return self.action('buyer','grant',{'minutes':30,'confirmed':True})
    def args(self,**extra): return dict(case_id=self.case,rule_id='R07',reason='Offline behavioural check.',**extra)
    def tool(self,name,**extra): return self.app.tool(self.rid,name,self.args(**extra),uid('call'),self.app.store.state(self.rid)['last_human_seq'])
    def send(self,kind='none',role='receiver',text='Test message'):
        s=self.app.store.state(self.rid)
        return self.tool('send_message',expected_state_version=s['state_version'],recipient_id=s[role]['id'],text=text,purpose='update' if kind=='none' else 'invite',response_kind=kind,attempt_id=None)
    def join(self):
        self.grant(); m=self.send('invitation'); self.action('receiver','respond',{'message_id':m['message_id'],'choice':'Accept invitation'})
    def consume_inputs(self):
        with self.app.store.tx():
            seq=self.app.store.events(self.rid)[-1]['sequence']; self.app.store.db.execute('UPDATE runs SET cursor=? WHERE id=?',(seq,self.rid))
    def add_audio(self):
        p=self.path/'test.wav'
        with wave.open(str(p),'w') as f: f.setnchannels(1); f.setsampwidth(2); f.setframerate(8000); f.writeframes(b'\0\0'*800)
        with self.app.store.tx(): self.app.store.db.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?)',('audio-test',self.rid,'synthetic.wav',str(p),'offline-audio-sha',0.1,'OFFLINE_TEST_ONLY'))
        self.action('receiver','audio',{'audio_id':'audio-test'})
    def test_grant_requires_buyer_and_explicit_confirmation(self):
        for role,confirmed in [('receiver',True),('buyer',False)]:
            with self.assertRaises(Rejected): self.action(role,'grant',{'minutes':30,'confirmed':confirmed})
        self.assertIsNone(self.app.store.state(self.rid)['buyer_grant'])
    def test_duplicate_human_action_does_not_create_two_grants(self):
        args={'minutes':30,'confirmed':True}; first=self.action('buyer','grant',args,version=0,key='request-one')
        second=self.action('buyer','grant',args,version=0,key='request-one')
        self.assertEqual(first,second); self.assertEqual(len(self.app.store.events(self.rid,incoming=True)),1)
    def test_reused_id_with_different_payload_rejected(self):
        self.action('buyer','grant',{'minutes':30,'confirmed':True},key='request-one')
        with self.assertRaisesRegex(Rejected,'different action'): self.action('buyer','grant',{'minutes':60,'confirmed':True},version=0,key='request-one')
    def test_stale_human_action_does_not_overwrite_new_grant(self):
        self.grant()
        with self.assertRaisesRegex(Rejected,'changed'): self.action('buyer','revoke',{},version=0)
        self.assertIsNone(self.app.store.state(self.rid)['buyer_grant']['revoked_at'])
    def test_receiver_cannot_accept_unrequested_invitation(self):
        self.grant()
        with self.assertRaises(Rejected): self.action('receiver','respond',{'message_id':'missing','choice':'Accept invitation'})
    def test_revoke_invalidates_previous_invitation_controls(self):
        self.grant(); m=self.send('invitation'); self.action('buyer','revoke',{})
        with self.assertRaisesRegex(Rejected,'expired or withdrawn'): self.action('receiver','respond',{'message_id':m['message_id'],'choice':'Accept invitation'})
    def test_expiry_prevents_acceptance(self):
        self.grant(); m=self.send('invitation')
        with self.app.store.tx():
            s=self.app.store.state(self.rid); s['buyer_grant']['expires_at']='2000-01-01T00:00:00+00:00'; self.app.store.save_state(self.rid,s)
        with self.assertRaises(Rejected): self.action('receiver','respond',{'message_id':m['message_id'],'choice':'Accept invitation'})
    def test_receiver_can_withdraw_after_availability(self):
        self.join(); m=self.send('availability'); self.action('receiver','respond',{'message_id':m['message_id'],'choice':'I can receive'}); self.action('receiver','withdraw',{})
        self.assertEqual(self.app.store.state(self.rid)['receiver_availability']['choice'],'I cannot receive')
    def test_availability_form_requires_invitation_and_active_grant(self):
        self.grant()
        with self.assertRaisesRegex(Rejected,'invitation'): self.action('receiver','availability',{'choice':'I can receive'})
        m=self.send('invitation'); self.action('receiver','respond',{'message_id':m['message_id'],'choice':'Accept invitation'})
        self.action('buyer','revoke',{})
        with self.assertRaisesRegex(Rejected,'expired or withdrawn'): self.action('receiver','availability',{'choice':'I can receive'})
    def test_human_can_offer_availability_and_later_withdraw(self):
        self.join(); self.action('receiver','availability',{'choice':'I can receive'})
        with self.assertRaisesRegex(Rejected,'already recorded'): self.action('receiver','availability',{'choice':'I can receive'})
        self.action('receiver','withdraw',{})
        self.assertEqual(self.app.store.state(self.rid)['receiver_availability']['choice'],'I cannot receive')
    def test_availability_form_dismisses_existing_model_buttons(self):
        self.join(); m=self.send('availability'); self.action('receiver','availability',{'choice':'Not this parcel'})
        self.assertEqual(self.app.store.messages(self.rid)[-1]['answered'],'Not this parcel')
        with self.assertRaisesRegex(Rejected,'already been answered'): self.action('receiver','respond',{'message_id':m['message_id'],'choice':'I can receive'})
    def test_model_cannot_use_a_consent_write_tool(self):
        with self.assertRaisesRegex(Rejected,'not implemented'): self.tool('write_buyer_grant')
        self.assertIsNone(self.app.store.state(self.rid)['buyer_grant'])
    def test_stale_agent_message_rejected(self):
        self.grant()
        with self.assertRaisesRegex(Rejected,'changed'): self.tool('send_message',expected_state_version=0,recipient_id='DEMO-RECEIVER-01',text='Old message',purpose='update',response_kind='none',attempt_id=None)
    def test_recipient_and_doorstep_controls_guarded(self):
        self.grant()
        for recipient,kind in [('OUTSIDER','none'),('DEMO-RECEIVER-01','doorstep')]:
            with self.assertRaises(Rejected): self.tool('send_message',expected_state_version=1,recipient_id=recipient,text='Test',purpose='update',response_kind=kind,attempt_id=None)
    def test_duplicate_message_is_stored_once(self):
        self.grant(); first=self.send(); second=self.send(); self.assertEqual(first['message_id'],second['message_id']); self.assertEqual(len(self.app.store.messages(self.rid)),1)
    def test_stt_not_called_for_unsubmitted_audio(self):
        with self.assertRaises(Rejected): self.tool('transcribe_receiver_audio',audio_id='audio-test')
        self.assertEqual(self.voice.calls,0)
    def test_transcription_is_cached_and_not_consent(self):
        self.add_audio(); self.tool('transcribe_receiver_audio',audio_id='audio-test'); result=self.tool('transcribe_receiver_audio',audio_id='audio-test')
        self.assertEqual(self.voice.calls,1); self.assertTrue(result['cache_reused']); self.assertIsNone(self.app.store.state(self.rid)['receiver_availability'])
    def test_failed_transcription_is_not_retried(self):
        self.voice.fail=True; self.add_audio(); a=self.tool('transcribe_receiver_audio',audio_id='audio-test'); b=self.tool('transcribe_receiver_audio',audio_id='audio-test')
        self.assertIn('error',a); self.assertTrue(b['cache_reused']); self.assertEqual(self.voice.calls,1)
    def test_delivery_completion_disabled(self):
        self.grant(); self.consume_inputs()
        with self.assertRaisesRegex(Rejected,'No delivery'): self.tool('close_case',expected_state_version=1,outcome='completed_simulated',reason_code='FAKE',evidence_refs=['fake'])
    def test_closure_requires_notifications_and_known_evidence(self):
        e=self.grant(); self.consume_inputs()
        with self.assertRaisesRegex(Rejected,'outcome update'): self.tool('close_case',expected_state_version=1,outcome='unresolved',reason_code='CARRIER_UNAVAILABLE',evidence_refs=[e['event_id']])
        self.send(role='buyer')
        with self.assertRaisesRegex(Rejected,'event or message'): self.tool('close_case',expected_state_version=1,outcome='unresolved',reason_code='CARRIER_UNAVAILABLE',evidence_refs=['invented'])
    def test_new_input_prevents_racing_closure(self):
        e=self.grant(); self.consume_inputs(); self.send(role='buyer'); self.action('receiver','text',{'text':'I do not want it'})
        with self.assertRaisesRegex(Rejected,'New input'): self.tool('close_case',expected_state_version=2,outcome='unresolved',reason_code='CARRIER_UNAVAILABLE',evidence_refs=[e['event_id']])
    def test_wait_cannot_lose_preexisting_input(self):
        self.grant(); result=self.tool('wait_for_event',event_types=['invitation_responded'],timeout_seconds=60)
        self.assertTrue(result['queued_input_will_resume']); self.assertEqual(self.app.tick(),self.rid)
    def test_new_input_during_model_call_yields_to_event_queue(self):
        owner=self
        class InjectingModel:
            def generate(self,*args):
                owner.action('receiver','text',{'text':'New refusal while the model was working'})
                return {'role':'model','parts':[{'functionCall':{'name':'get_parcel_context','args':owner.args()}}]}, {'model_version':'OFFLINE_TEST_ONLY'}
        self.app.model_factory=lambda j:(InjectingModel(),self.voice)
        self.grant(); self.app.process_one(self.rid)
        row=self.app.store.row(self.rid)
        self.assertEqual(row['status'],'idle'); self.assertEqual(row['model_calls'],1)
        pending=self.app.store.events(self.rid,row['cursor'],incoming=True)
        self.assertEqual(len(pending),1); self.assertEqual(pending[0]['event_type'],'receiver_text_received')
        self.assertEqual(self.app.tick(),self.rid)
    def test_optional_null_attempt_id_is_not_required_in_coordination_mode(self):
        self.grant()
        self.tool('send_message',expected_state_version=1,recipient_id='DEMO-RECEIVER-01',text='Example',purpose='update',response_kind='none')
        self.assertEqual(len(self.app.store.messages(self.rid)),1)
    def test_failed_tool_prevents_dependent_wait_in_same_batch(self):
        owner=self
        class BatchModel:
            calls=0
            def generate(self,*args):
                self.calls+=1
                if self.calls==2: raise IntegrationError('End of offline failure inspection')
                return {'role':'model','parts':[
                    {'functionCall':{'name':'send_message','args':owner.args()}},
                    {'functionCall':{'name':'wait_for_event','args':owner.args(event_types=['receiver_text_received'],timeout_seconds=60)}}
                ]},{'model_version':'OFFLINE_TEST_ONLY'}
        self.app.model_factory=lambda j:(BatchModel(),self.voice)
        self.grant(); self.app.process_one(self.rid)
        events=self.app.store.events(self.rid)
        self.assertFalse(any(e['event_type']=='wait_registered' for e in events))
        self.assertTrue(any(e['payload'].get('response',{}).get('error',{}).get('code')=='PRIOR_TOOL_FAILED' for e in events))
    def test_real_elapsed_timer_generates_one_event(self):
        self.consume_inputs()
        with self.app.store.tx(): self.app.store.db.execute("UPDATE runs SET status='waiting',waiting=? WHERE id=?",(packed({'deadline':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),'event_types':['receiver_text_received']}),self.rid))
        self.assertEqual(self.app.tick(),self.rid); self.app.tick()
        self.assertEqual(len([e for e in self.app.store.events(self.rid) if e['event_type']=='timer_expired']),1)
    def test_restart_halts_uncertain_inflight_call(self):
        with self.app.store.tx(): self.app.store.db.execute("UPDATE runs SET status='running' WHERE id=?",(self.rid,))
        other=App(self.path/'data',self.path,auto_worker=False,model_factory=lambda j:(Model(),self.voice))
        self.assertEqual(other.store.row(self.rid)['status'],'halted'); other.store.db.close()
    def test_extra_tool_argument_and_bool_version_rejected(self):
        with self.assertRaises(Rejected): self.app.tool(self.rid,'get_parcel_context',self.args(ignore_rules=True),'call',0)
        with self.assertRaises(Rejected): self.tool('send_message',expected_state_version=True,recipient_id='DEMO-RECEIVER-01',text='x',purpose='update',response_kind='none',attempt_id=None)

if __name__=='__main__': unittest.main(verbosity=2)
