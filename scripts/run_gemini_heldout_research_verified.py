"""Audit the candidate before calling the unchanged frozen heldout runner."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.replay.ai_history import digest
from scripts.audit_gemini_candidate_provenance import audit_directory


def candidate_hash(source):
    return digest(json.loads((source/'strategy-candidates.json').read_text(encoding='utf-8')))


def run_verified(source, target, forwarded_args):
    # Original runner remains frozen; this wrapper never edits its source or
    # skips protocol/priority checks, creates retries, or activates trading.
    source, target = Path(source).resolve(), Path(target).resolve()
    if target == source or source in target.parents or target in source.parents:
        raise ValueError('VERIFIED_HELDOUT_SOURCE_TARGET_MUST_BE_DISJOINT')
    proof = audit_directory(source)
    expected = proof['candidate_artifact_sha256']
    if candidate_hash(source) != expected:
        raise ValueError('VERIFIED_HELDOUT_CANDIDATE_CHANGED_AFTER_AUDIT')
    from scripts import run_gemini_heldout_research as frozen
    previous = sys.argv
    try:
        sys.argv = ['run_gemini_heldout_research', '--optimization-directory', str(source),
                    '--directory', str(target), *forwarded_args]
        result = frozen.main()
    finally:
        sys.argv = previous
    if candidate_hash(source) != expected:
        raise ValueError('VERIFIED_HELDOUT_SOURCE_CANDIDATE_CHANGED_DURING_RUN')
    registered = json.loads((target/'research-plan.json').read_text(encoding='utf-8'))
    if registered['plan_sha256'] != digest(registered['plan']):
        raise ValueError('VERIFIED_HELDOUT_PLAN_HASH_INVALID')
    if registered['plan'].get('candidate_artifact_sha256') != expected:
        raise ValueError('VERIFIED_HELDOUT_SELECTED_CANDIDATE_MISMATCH')
    # Each bounded return is still not an economic/whole-stage acceptance.
    proof = {**proof, 'scope': 'CANDIDATE_AUDITED_BEFORE_AND_BOUND_TO_HELDOUT_PLAN_NOT_PROFIT_ACCEPTANCE',
        'heldout_plan_sha256': registered['plan_sha256'], 'profit_acceptance_passed': False,
        'complete_heldout_acceptance_still_required': True}
    (target/'candidate-provenance-audit.json').write_text(json.dumps(proof,indent=2),encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__,
        epilog='Other arguments are forwarded unchanged to run_gemini_heldout_research.py.')
    parser.add_argument('--optimization-directory', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    args, forwarded = parser.parse_known_args()
    return run_verified(args.optimization_directory, args.directory, forwarded)


if __name__ == '__main__':
    raise SystemExit(main())
