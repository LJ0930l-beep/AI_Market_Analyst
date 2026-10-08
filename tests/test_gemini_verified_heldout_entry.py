"""No model calls: mandatory audit, pinned selection and unchanged forwarding."""
from copy import deepcopy
import json
import sys

import pytest

from core.replay.ai_history import digest
from scripts import run_gemini_heldout_research_verified as verified
from scripts import run_gemini_heldout_research as frozen


def fixture(tmp_path, monkeypatch):
    source = tmp_path/'optimization'; source.mkdir()
    target = tmp_path/'validation'
    artifact = {'synthetic': 'candidate', 'production_strategy_writes': 0}
    (source/'strategy-candidates.json').write_text(json.dumps(artifact),encoding='utf-8')
    expected = digest(artifact)
    proof = {'status': 'CANDIDATE_PROVENANCE_PASS_NOT_STRATEGY_OR_PROFIT_ACCEPTANCE',
             'candidate_artifact_sha256': expected, 'private_exchange_calls': 0, 'model_calls': 0}
    calls = []
    def audit(path):
        assert path == source
        calls.append('audit')
        return deepcopy(proof)
    def run():
        assert calls == ['audit']
        calls.append('frozen')
        assert sys.argv[1:5] == ['--optimization-directory',str(source),'--directory',str(target)]
        assert sys.argv[5:] == ['--phase','validation','--run','--max-decisions','50']
        target.mkdir()
        plan = {'candidate_artifact_sha256': expected}
        (target/'research-plan.json').write_text(json.dumps({'plan':plan,'plan_sha256':digest(plan)}),encoding='utf-8')
        return 0
    monkeypatch.setattr(verified, 'audit_directory', audit)
    monkeypatch.setattr(frozen, 'main', run)
    return source,target,artifact,calls,run


def test_audit_is_mandatory_before_same_frozen_runner_and_arguments_are_restored(tmp_path,monkeypatch):
    source,target,_,calls,_ = fixture(tmp_path,monkeypatch)
    original = list(sys.argv)
    assert verified.run_verified(source,target,['--phase','validation','--run','--max-decisions','50']) == 0
    assert calls == ['audit','frozen'] and sys.argv == original
    proof = json.loads((target/'candidate-provenance-audit.json').read_text(encoding='utf-8'))
    assert proof['profit_acceptance_passed'] is False and proof['complete_heldout_acceptance_still_required'] is True


def test_failed_audit_cannot_freeze_or_start_heldout(tmp_path,monkeypatch):
    source,target,_,calls,_ = fixture(tmp_path,monkeypatch)
    def failure(_): raise ValueError('CANDIDATE_OUTPUT_CHANGED_FROM_MODEL_RESPONSE')
    monkeypatch.setattr(verified,'audit_directory',failure)
    with pytest.raises(ValueError,match='OUTPUT_CHANGED'):
        verified.run_verified(source,target,[])
    assert calls == [] and not target.exists()


@pytest.mark.parametrize('defect',['audit_hash','during_run','selected_plan','plan_hash'])
def test_candidate_changes_or_wrong_freeze_binding_fail_without_pass_receipt(tmp_path,monkeypatch,defect):
    source,target,artifact,calls,run = fixture(tmp_path,monkeypatch)
    if defect == 'audit_hash':
        monkeypatch.setattr(verified,'audit_directory',lambda _: {'candidate_artifact_sha256':'f'*64})
    else:
        def altered():
            result = run()
            if defect == 'during_run':
                artifact['synthetic'] = 'changed'
                (source/'strategy-candidates.json').write_text(json.dumps(artifact),encoding='utf-8')
            else:
                p=target/'research-plan.json';d=json.loads(p.read_text(encoding='utf-8'))
                if defect == 'selected_plan':
                    d['plan']['candidate_artifact_sha256']='b'*64;d['plan_sha256']=digest(d['plan'])
                else: d['plan_sha256']='c'*64
                p.write_text(json.dumps(d),encoding='utf-8')
            return result
        monkeypatch.setattr(frozen,'main',altered)
    with pytest.raises(ValueError):
        verified.run_verified(source,target,['--phase','validation','--run','--max-decisions','50'])
    assert not (target/'candidate-provenance-audit.json').exists()


@pytest.mark.parametrize('relation',['same','child','parent'])
def test_source_or_nested_directory_is_refused_before_audit(tmp_path,monkeypatch,relation):
    source,target,_,calls,_ = fixture(tmp_path,monkeypatch)
    target=source if relation=='same' else source/'validation' if relation=='child' else tmp_path
    with pytest.raises(ValueError,match='DISJOINT'):
        verified.run_verified(source,target,[])
    assert calls == []
