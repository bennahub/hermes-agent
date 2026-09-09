import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

BASE=Path(__file__).resolve().parents[2]
CANDIDATE=BASE
from hermes_cli import google_workspace_onboarding as adapter,google_workspace_worker as worker
from hermes_cli.google_gmail_verification import SCOPES
NATIVE=BASE/'skills/productivity/google-workspace/scripts/setup.py'
PINS={'google-api-python-client':'2.194.0','google-auth':'2.55.1','google-auth-oauthlib':'1.3.1','google-auth-httplib2':'0.3.1','httplib2':'0.32.0','pyasn1':'0.6.4'}
CLIENT={'installed':{'client_id':'test.apps.googleusercontent.com','client_secret':'synthetic-only','auth_uri':'https://accounts.google.com/o/oauth2/auth','token_uri':'https://oauth2.googleapis.com/token'}}

class FakeFlow:
    calls=[]
    code_verifier='synthetic-pkce'
    credentials=types.SimpleNamespace(to_json=lambda:json.dumps({'token':'synthetic-token','refresh_token':'synthetic-refresh','client_id':'test.apps.googleusercontent.com','client_secret':'synthetic-only','scopes':SCOPES}),granted_scopes=SCOPES)
    @classmethod
    def from_client_secrets_file(cls,path,**kw):
        cls.calls.append(('construct',path,kw));return cls()
    def authorization_url(self,**kw):return 'https://accounts.google.com/o/oauth2/auth?state=synthetic-state','synthetic-state'
    def fetch_token(self,**kw):self.calls.append(('exchange',kw))

class NativeGoogleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)/'.hermes';self.home.mkdir()
        for name in ['one','two']:
            p=self.home/'profiles'/name;p.mkdir(parents=True);(p/'config.yaml').write_text('{}')
        self.umask=os.umask(0o077);self.addCleanup(os.umask,self.umask)
        for item in [patch.dict(os.environ,{'HOME':self.tmp.name,'HERMES_HOME':str(self.home)}),
                     patch.object(adapter,'native_path',return_value=NATIVE),
                     patch('importlib.metadata.version',side_effect=lambda name:PINS[name]),
                     patch.dict(sys.modules,{'google_auth_oauthlib.flow':types.SimpleNamespace(Flow=FakeFlow)})]:
            item.start();self.addCleanup(item.stop)
        FakeFlow.calls=[]
        # Keep native Credentials construction, serialization and verification;
        # only substitute the external refresh and two Gmail GET boundaries.
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import AuthorizedSession
        def refresh(credentials, request):
            credentials.token='synthetic-refreshed'
            credentials._granted_scopes=list(SCOPES)
        def get(session,url,**kw):
            if url.endswith('/profile'): data={'emailAddress':'owner@example.com'}
            elif url.endswith('/labels'): data={'labels':[]}
            else: raise AssertionError('unexpected Gmail operation')
            return types.SimpleNamespace(raise_for_status=lambda:None,json=lambda:data)
        for item in [patch.object(Credentials,'refresh',refresh),patch.object(AuthorizedSession,'get',get)]:
            item.start();self.addCleanup(item.stop)
    def op(self,action,scope='one',owner='owner-a',sid=None,value=None):
        return worker.execute({'scope':scope,'owner_id':owner,'action':action,'session_id':sid,'value':value})
    def start(self):
        self.op('save_client',value=json.dumps(CLIENT));return self.op('start')
    def callback(self,state='synthetic-state'):return 'http://localhost:1?code=synthetic-code&state='+state
    def test_callback_scope_cannot_override_approved_flow(self):
        started=self.start()
        self.op('submit',sid=started['session_id'],value=self.callback()+'&scope=https://mail.google.com/')
        scopes=[call[2]['scopes'] for call in FakeFlow.calls if call[0]=='construct']
        self.assertEqual(scopes[-1],started['requested_scopes'])
    def test_desktop_actual_loopback_port_preserved_through_exchange(self):
        self.op('save_client',value=json.dumps(CLIENT))
        data={'scope':'one','owner_id':'owner-a','action':'start','loopback_port':43217}
        for port in [True,1,65536,'43217']:
            with self.assertRaisesRegex(ValueError,'invalid_google_request'):
                worker.execute({**data,'loopback_port':port})
        started=worker.execute(data)
        pending=json.loads((self.home/'profiles/one/google_oauth_pending.json').read_text())
        self.assertEqual(pending['redirect_uri'],'http://127.0.0.1:43217')
        bad='http://127.0.0.1:43218/?code=synthetic-code&state=synthetic-state'
        with self.assertRaisesRegex(ValueError,'invalid_google_callback'):
            self.op('submit',sid=started['session_id'],value=bad)
        good=bad.replace(':43218',':43217')
        self.assertEqual(self.op('submit',sid=started['session_id'],value=good)['status'],'completed')
        constructors=[call for call in FakeFlow.calls if call[0]=='construct']
        self.assertEqual(constructors[-1][2]['redirect_uri'],pending['redirect_uri'])
    def test_native_save_and_two_scope_isolation(self):
        self.op('save_client',value=json.dumps(CLIENT))
        path=self.home/'profiles/one/google_client_secret.json'
        self.assertEqual(json.loads(path.read_text()),CLIENT);self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertFalse((self.home/'google_client_secret.json').exists())
        self.assertEqual(self.op('start',scope='two')['error'],'google_client_required')
    def test_real_native_start_and_verified_token_writer(self):
        start=self.start();sid=start['session_id']
        self.assertEqual(start['requested_scopes'],SCOPES)
        self.assertEqual(FakeFlow.calls[0][2]['scopes'],start['requested_scopes'])
        pending=self.home/'profiles/one/google_oauth_pending.json'
        self.assertIn('code_verifier',json.loads(pending.read_text()))
        self.assertNotIn('code_verifier',json.dumps(start));self.assertNotIn('owner-a',json.dumps(start))
        result=self.op('submit',sid=sid,value=self.callback());self.assertEqual(result['status'],'completed')
        token=self.home/'profiles/one/google_token.json';self.assertEqual(token.stat().st_mode&0o777,0o600)
        self.assertEqual(json.loads(token.read_text())['scopes'],SCOPES)
        self.assertNotIn('code_verifier',pending.read_text());self.assertNotIn('synthetic-token',json.dumps(result))
        self.assertEqual(self.op('poll',sid=sid)['status'],'completed')
    def test_browser_normalized_root_callback_completes_native_flow(self):
        start=self.start();sid=start['session_id']
        # Browser serialization adds '/' to native REDIRECT_URI=http://localhost:1.
        for callback in ['http://localhost:1/other?code=x&state=synthetic-state',
                         'http://127.0.0.1:1/?code=x&state=synthetic-state',
                         'http://localhost:2/?code=x&state=synthetic-state']:
            with self.assertRaisesRegex(ValueError,'invalid_google_callback'):
                self.op('submit',sid=sid,value=callback)
        result=self.op('submit',sid=sid,value='http://localhost:1/?code=synthetic-code&state=synthetic-state')
        self.assertEqual(result['status'],'completed')
        self.assertTrue((self.home/'profiles/one/google_token.json').is_file())
        self.assertEqual(sum(x[0]=='exchange' for x in FakeFlow.calls),1)
    def test_owner_scope_and_session_binding(self):
        sid=self.start()['session_id']
        for kwargs,error in [({'owner':'owner-b'},'google_operation_forbidden'),({'scope':'two'},'google_operation_not_found'),({'sid':'wrong'},'google_operation_not_found')]:
            with self.assertRaisesRegex(ValueError,error):self.op('submit',**{'sid':sid,'value':self.callback(),**kwargs})
        self.assertFalse(any(x[0]=='exchange' for x in FakeFlow.calls))
    def test_idempotent_start_and_other_owner_busy(self):
        first=self.start();self.assertEqual(self.op('start')['session_id'],first['session_id'])
        with self.assertRaisesRegex(ValueError,'google_operation_busy'):self.op('start',owner='owner-b')
    def test_bad_callback_does_not_consume_operation(self):
        sid=self.start()['session_id']
        for value in ['code-only',self.callback('wrong'),'https://evil.invalid/?code=x&state=synthetic-state',self.callback()+'&state=synthetic-state']:
            with self.assertRaises(ValueError):self.op('submit',sid=sid,value=value)
        self.assertEqual(self.op('poll',sid=sid)['status'],'pending')
        self.assertFalse(any(x[0]=='exchange' for x in FakeFlow.calls))
    def test_completed_operation_cannot_replay(self):
        sid=self.start()['session_id'];self.op('submit',sid=sid,value=self.callback())
        with self.assertRaisesRegex(ValueError,'google_operation_consumed'):self.op('submit',sid=sid,value=self.callback())
        self.assertEqual(sum(x[0]=='exchange' for x in FakeFlow.calls),1)
    def test_expired_operation_clears_pkce_without_touching_credentials(self):
        sid=self.start()['session_id'];target=self.home/'profiles/one/google_oauth_pending.json'
        raw=json.loads(target.read_text());raw[adapter.META]['expires_at']=0;target.write_text(json.dumps(raw))
        self.assertEqual(self.op('poll',sid=sid)['status'],'expired');self.assertNotIn('code_verifier',target.read_text())
        with self.assertRaisesRegex(ValueError,'google_operation_expired'):self.op('submit',sid=sid,value=self.callback())
    def test_cancel_preserves_client_and_token(self):
        sid=self.start()['session_id'];home=self.home/'profiles/one';(home/'google_token.json').write_text('{"token":"old-synthetic"}')
        self.op('cancel',sid=sid)
        self.assertFalse((home/'google_oauth_pending.json').exists());self.assertTrue((home/'google_client_secret.json').exists())
        self.assertEqual(json.loads((home/'google_token.json').read_text())['token'],'old-synthetic')
    def test_uncertain_exchange_consumed_and_no_native_error_echo(self):
        sid=self.start()['session_id']
        with patch.object(FakeFlow,'fetch_token',side_effect=RuntimeError('SECRET-MUST-NOT-ESCAPE')):
            with self.assertRaisesRegex(ValueError,'^google_operation_failed$'):self.op('submit',sid=sid,value=self.callback())
        self.assertEqual(self.op('poll',sid=sid)['status'],'failed')
        with self.assertRaisesRegex(ValueError,'google_operation_consumed'):self.op('submit',sid=sid,value=self.callback())
    def test_symlink_store_and_untrusted_client_endpoint_rejected(self):
        client=json.loads(json.dumps(CLIENT));client['installed']['token_uri']='https://evil.invalid/token'
        with self.assertRaisesRegex(ValueError,'invalid_google_client_endpoint'):self.op('save_client',value=json.dumps(client))
        (self.home/'profiles/one/google_client_secret.json').symlink_to(self.home/'outside')
        with self.assertRaisesRegex(ValueError,'invalid_google_store'):self.op('save_client',value=json.dumps(CLIENT))
    def test_missing_dependencies_do_not_install_or_start(self):
        self.op('save_client',value=json.dumps(CLIENT))
        with patch('importlib.metadata.version',return_value='missing'),patch('subprocess.run',side_effect=AssertionError('implicit install forbidden')):
            self.assertEqual(self.op('start'),{'ok':False,'error':'google_runtime_dependencies_required'})
        self.assertFalse((self.home/'profiles/one/google_oauth_pending.json').exists())
    def test_native_cli_pending_is_never_overwritten(self):
        path=self.home/'profiles/one/google_oauth_pending.json'
        original=json.dumps({'state':'cli-state','code_verifier':'cli-private-pkce'})
        path.write_text(original)
        with self.assertRaisesRegex(ValueError,'google_operation_busy'):self.op('save_client',value=json.dumps(CLIENT))
        self.assertEqual(path.read_text(),original)
    def test_per_scope_lock_refuses_parallel_operation(self):
        import fcntl
        with (self.home/'profiles/one/.google-oauth.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError,'google_operation_busy'):self.op('status')
        self.assertTrue(self.op('status',scope='two')['ok'])
    def test_metadata_is_local_only_and_declares_full_gmail_scopes(self):
        self.op('save_client',value=json.dumps(CLIENT));d=adapter.describe(self.home/'profiles/one')
        self.assertTrue(d['client_present']);self.assertFalse(d['token_present']);self.assertTrue(d['dependencies_available'])
        self.assertEqual(d['requested_scopes'],SCOPES);self.assertNotIn('synthetic-only',json.dumps(d))

    def test_partial_missing_or_wrong_account_reconsent_preserves_old_token(self):
        home=self.home/'profiles/one'
        original={'token':'working-readonly','refresh_token':'synthetic-refresh',
                  'scopes':['https://www.googleapis.com/auth/gmail.readonly'],
                  '_hermes_gmail_verification':{'account':'original@example.com'}}
        old=json.dumps(original)
        for granted in [[], ['https://mail.google.com/'], SCOPES]:
            (home/'google_token.json').write_text(old)
            credentials=types.SimpleNamespace(to_json=lambda:json.dumps({
                'token':'synthetic-new','refresh_token':'synthetic-refresh','client_id':'test.apps.googleusercontent.com',
                'client_secret':'synthetic-only','scopes':SCOPES}),granted_scopes=granted)
            with patch.object(FakeFlow,'credentials',credentials):
                started=self.start()
                with self.assertRaisesRegex(ValueError,'google_operation_failed'):
                    self.op('submit',sid=started['session_id'],value=self.callback())
            self.assertEqual((home/'google_token.json').read_text(),old)
            self.assertEqual(self.op('poll',sid=started['session_id'])['status'],'failed')
            self.op('cancel',sid=started['session_id'])

    def test_shared_root_lock_and_original_profile_operation_binding(self):
        import fcntl
        root_token=self.home/'google_token.json';root_token.write_text('{"scopes":[]}')
        self.op('save_client',scope='default',value=json.dumps(CLIENT))
        with (self.home/'.google-oauth.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            for scope in ['default','one','two']:
                with self.assertRaisesRegex(ValueError,'google_operation_busy'):self.op('status',scope=scope)
        start=self.op('start',scope='one')
        with self.assertRaisesRegex(ValueError,'google_operation_forbidden'):
            self.op('poll',scope='two',sid=start['session_id'])
        self.assertTrue((self.home/'google_oauth_pending.json').exists())
        self.assertFalse((self.home/'profiles/one/google_oauth_pending.json').exists())
        self.assertTrue(adapter.describe(self.home/'profiles/two')['shared'])
        self.assertFalse((self.home/'profiles/two/google_token.json').exists())

    def test_account_alias_cannot_be_a_path(self):
        with self.assertRaisesRegex(ValueError, "invalid_google_request"):
            worker.execute({"scope": "one", "owner_id": "owner-a", "action": "disconnect",
                            "account": "../google_token.json"})
        with self.assertRaisesRegex(ValueError, "invalid_google_request"):
            worker.execute({"scope": "one", "owner_id": "owner-a", "action": "start",
                            "account": "google_account_2"})


class IsolatedWorkerBoundaryTests(unittest.TestCase):
    setUp=NativeGoogleTests.setUp
    def test_stdin_worker_uses_actual_scoped_native_store(self):
        result=adapter.run_operation('two','owner-a','save_client',value=json.dumps(CLIENT))
        self.assertEqual(result,{'ok':True,'detail_code':'google_client_saved'})
        self.assertTrue((self.home/'profiles/two/google_client_secret.json').exists())
        self.assertFalse((self.home/'profiles/one/google_client_secret.json').exists())
        error=adapter.run_operation('two','owner-a','save_client',value='synthetic-invalid-secret')
        self.assertEqual(error,{'ok':False,'error':'invalid_google_client'})

if __name__=='__main__':unittest.main()
