"""Native regression tests. HERMES_REVIEW_SOURCE must name the reviewed checkout.
Every runtime import occurs in a fresh child with temporary HOME; never launches Chrome.
Run: HERMES_REVIEW_SOURCE=/release PYTHONDONTWRITEBYTECODE=1 python -m pytest THIS_FILE
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import pytest

SOURCE = Path(os.environ.get('HERMES_REVIEW_SOURCE', Path(__file__).resolve().parents[2])).resolve()
COMPUTER = {'computer_ensure','computer_status','computer_wake','computer_observe','computer_act'}


def probe(body):
    with tempfile.TemporaryDirectory(prefix='computer-regression-') as tmp:
        home = Path(tmp); (home/'.hermes').mkdir()
        env = {k:v for k,v in os.environ.items() if not k.startswith(('HERMES_', 'AGENT_BROWSER_'))}
        env.update(HOME=str(home), HERMES_HOME=str(home/'.hermes'), PYTHONPATH=str(SOURCE),
                   PYTHONDONTWRITEBYTECODE='1', PROBE_RESULT=str(home/'result.json'))
        prefix = '''import os,json
from pathlib import Path
home=Path(os.environ['HERMES_HOME'])
def finish(value): Path(os.environ['PROBE_RESULT']).write_text(json.dumps(value))
'''
        process = subprocess.run([sys.executable,'-c',prefix+body],cwd=SOURCE,env=env,
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=60)
        assert process.returncode == 0, process.stderr[-6000:]
        return json.loads((home/'result.json').read_text())


@pytest.fixture(scope='module')
def gates():
    return probe('''
from model_tools import get_tool_definitions
from tools.agent_computer_tool import check_agent_computer_requirements
cfg=home/'config.yaml'
def names(enabled, disabled=None):
 return sorted(x['function']['name'] for x in get_tool_definitions(enabled,disabled_toolsets=disabled,quiet_mode=False,skip_tool_search_assembly=True))
r={}
cfg.write_text('agent_computer:\\n  tools_enabled: false\\n')
r['off']=names(['agent_computer','connections'])
cfg.write_text('agent_computer:\\n  tools_enabled: true\\n')
r['on']=names(['agent_computer','connections'])
r['disabled']=names(['agent_computer','connections'],['agent_computer','connections'])
for value in ['', 'false','true']:
 os.environ['HERMES_AGENT_COMPUTER_TOOLS']=value
 r['override_'+value]=check_agent_computer_requirements()
os.environ.pop('HERMES_AGENT_COMPUTER_TOOLS')
# Actual same-process enable -> disable recheck must not retain a positive TTL.
cfg.write_text('agent_computer:\\n  tools_enabled: false\\n')
r['off_again']=names(['agent_computer'])
from tui_gateway import server
os.environ['HERMES_TUI_TOOLSETS']='web'
r['pinned']=server._load_enabled_toolsets('desktop')
assert not (home/'agent-computers').exists()
finish(r)
''')


def test_disabled_profile_does_not_expose_computer_even_when_toolset_selected(gates):
    assert set(gates['off']) == {'list_connections','request_connection'}


def test_enabled_profile_exposes_all_five_without_creating_computer(gates):
    assert set(gates['on']) == COMPUTER | {'list_connections','request_connection'}


def test_disabled_toolsets_are_final_subtraction(gates):
    assert gates['disabled'] == []


def test_profile_disable_is_not_positive_ttl_cached(gates):
    assert gates['off_again'] == []


def test_explicit_environment_false_empty_and_true(gates):
    assert gates['override_'] is False
    assert gates['override_false'] is False
    assert gates['override_true'] is True


def test_explicit_tui_pin_is_not_expanded(gates):
    assert gates['pinned'] == ['web']


def test_chromium_launch_argv_uses_dedicated_profile_and_loopback():
    result=probe('''
from gateway.agent_computer.adapter import chromium_launch_argv
from unittest.mock import patch
# Any future accidental process launch through this public helper fails the test.
with patch('subprocess.Popen',side_effect=AssertionError('Chrome launch forbidden')):
 argv=chromium_launch_argv('/fake/chrome',str(home/'identities/bi_test'),sandbox_bypass=False)
finish({'argv':argv,'profile':str(home/'identities/bi_test')})
''')
    argv=result['argv']
    assert argv[0]=='/fake/chrome' and argv[-1]=='about:blank'
    assert '--remote-debugging-port=0' in argv
    assert '--remote-debugging-address=127.0.0.1' in argv
    assert f"--user-data-dir={result['profile']}" in argv
    assert '--headless=new' in argv
    assert '--no-sandbox' not in argv


def test_chromium_explicit_sandbox_fallback_argv():
    argv=probe('''
from gateway.agent_computer.adapter import chromium_launch_argv
finish(chromium_launch_argv('/fake/chrome',str(home/'dedicated'),sandbox_bypass=True,extra_args=['--disable-dev-shm-usage']))
''')
    assert argv.count('--no-sandbox')==1
    assert argv.count('--disable-dev-shm-usage')==1


@pytest.mark.parametrize('method', ['computer.takeover','computer.takeover.connect',
    'computer.give_back','computer.identity.create','computer.owner_disconnect'])
def test_registered_rpc_owner_denial_is_structured_not_nameerror(method):
    result=probe('''
from tui_gateway import server
from gateway.agent_computer.errors import AgentComputerError
assert server.AgentComputerError is AgentComputerError
# No registered contract is allowed to be evaluated: denial precedes state access.
def forbidden_contract(): raise AssertionError('contract touched before owner authentication')
handler=server._methods['''+repr(method)+''']
result=handler(91,{},_contract=forbidden_contract)
assert not (home/'agent-computers').exists()
finish(result)
''')
    assert result['id']==91 and 'result' not in result
    assert 'owner authentication required' in result['error']['message']
    assert result['error']['data']['ok'] is False
