import importlib.util
import json
import time
from pathlib import Path
from datetime import datetime, timezone
import pytest

path=Path(__file__).resolve().parents[1]/'operator/wave3/listener_probe.py'
spec=importlib.util.spec_from_file_location('listener_security_probe',path)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
def iso(t):return datetime.fromtimestamp(t,timezone.utc).isoformat()
def setup(tmp_path):
    config=dict(run_id='run',generation=2,lease_path=str(tmp_path/'lease.json'),receipt_path=str(tmp_path/'receipt.json'))
    doc=dict(version=1,run_id='run',generation=2,current=dict(token='a'*43,epoch=1,expires_at=iso(time.time()+3600)))
    save(config,doc)
    calls=[]
    def request(config,method,path,token=None,generation=None,body=None):
        calls.append((method,path,token,generation))
        current=json.loads(Path(config['lease_path']).read_text())['current']['token']
        return (200,{'ok':True}) if token==current and generation==2 else (401,{'error':'unauthorized'})
    return config,doc,calls,request
def save(config,doc):
    p=Path(config['lease_path']);p.write_text(json.dumps(doc));p.chmod(0o600)

@pytest.mark.parametrize('case',['missing','wrong','stale'])
def test_actual_positive_control_precedes_rejection(tmp_path,case):
    config,doc,calls,request=setup(tmp_path)
    result=m.probe(config,case,'abort','command',request)
    assert result['status']==401
    assert [x[0] for x in calls]==['GET','POST']
    assert doc['current']['token'] not in json.dumps(result)

def test_expiry_requires_previously_accepted_token_and_elapsed_overlap(tmp_path):
    config,doc,calls,request=setup(tmp_path)
    proof=m.prepare(config,request)
    assert 'token' not in proof
    assert Path(config['receipt_path']).stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError,match='not expired'):m.probe(config,'expired','pause','one',request)
    assert not any(x[0]=='POST' for x in calls)
    now=time.time();old=doc['current']
    doc.update(current=dict(token='b'*43,epoch=2,expires_at=iso(now+3600)),
        previous={**old,'valid_until':iso(now-1)},staged_at=iso(now-31))
    save(config,doc)
    result=m.probe(config,'expired','pause','two',request)
    assert result['status']==401
    assert result['credential_proof']['token_sha256']==proof['token_sha256']
    assert old['token'] not in json.dumps(result)
    assert calls[-1][2]==old['token']

@pytest.mark.parametrize('bad',['foreign','public_file','bad_positive'])
def test_refuses_unbound_or_unverified_credential(tmp_path,bad):
    config,doc,calls,request=setup(tmp_path)
    if bad=='foreign':doc['run_id']='other';save(config,doc)
    if bad=='public_file':Path(config['lease_path']).chmod(0o644)
    if bad=='bad_positive':request=lambda *args:(401,{})
    with pytest.raises(ValueError):m.prepare(config,request)
    assert not Path(config['receipt_path']).exists()

def test_preserves_observed_overlap_across_later_rotation_without_rewriting_acceptance(tmp_path,monkeypatch):
    config,doc,calls,request=setup(tmp_path)
    m.prepare(config,request)
    original=Path(config['receipt_path']).read_bytes()
    now=time.time();monkeypatch.setattr(m.time,'time',lambda:now)
    old=doc['current'];doc.update(current=dict(token='b'*43,epoch=2,expires_at=iso(now+3600)),
        previous={**old,'valid_until':iso(now+20)},staged_at=iso(now-10))
    save(config,doc)
    with pytest.raises(ValueError,match='not expired'):m.probe(config,'expired','pause','one',request)
    window=Path(config['receipt_path']+'.rotation.json');recorded=window.read_bytes()
    assert window.stat().st_mode & 0o777==0o600
    doc.update(current=dict(token='c'*43,epoch=3,expires_at=iso(now+3600)),
        previous={**doc['current'],'valid_until':iso(now+330)},staged_at=iso(now+300))
    save(config,doc);monkeypatch.setattr(m.time,'time',lambda:now+301)
    result=m.probe(config,'expired','pause','two',request)
    assert result['status']==401
    assert result['credential_proof']['expired_at']==iso(now+20)
    assert window.read_bytes()==recorded
    assert Path(config['receipt_path']).read_bytes()==original


def test_foreign_rotation_archive_cannot_establish_expiry(tmp_path):
    config,doc,calls,request=setup(tmp_path);m.prepare(config,request)
    window=Path(config['receipt_path']+'.rotation.json');window.write_text(json.dumps({'run_id':'foreign'}));window.chmod(0o600)
    with pytest.raises(ValueError,match='identity'):m.probe(config,'expired','pause','one',request)
    assert not any(x[0]=='POST' for x in calls)
