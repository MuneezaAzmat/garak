"""Run an auditable evaluation in individually resumable stages."""

import argparse
import json
import sys
from pathlib import Path

from . import runner


def main(argv=None):
    # Use the pinned executor source even outside pytest's configured path.
    sys.path.insert(0, str(runner.ROOT / 'vendor/mini-agents/src'))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['doctor', 'controls', 'baseline', 'campaign', 'rejudge', 'report', 'run'])
    parser.add_argument('--config', type=Path, default=runner.ROOT / 'configs/ollama-qwen.json')
    parser.add_argument('--run-dir', type=Path, default=runner.ROOT / 'runs/qwen-pilot')
    parser.add_argument('--offline', action='store_true', help='Doctor: check source imports without contacting provider')
    parser.add_argument('--provider-only', action='store_true', help='Doctor: check authenticated provider access only')
    args = parser.parse_args(argv)
    runner.load_env()
    config = runner.load_config(args.config)
    directory, _ = runner.initialize_run(args.run_dir, config)
    if args.command == 'doctor':
        result = runner.doctor(directory, config, offline=args.offline, provider_only=args.provider_only)
        from .report import generate_report
        generate_report(directory)
        print(json.dumps(result, indent=2))
        return 0 if result['ok'] else 1
    from .report import generate_report
    if args.command == 'report':
        generate_report(directory)
        print(f'Report: {directory / "report.md"}')
        return 0
    if args.command == 'run':
        if not runner.doctor(directory, config)['ok']:
            manifest = json.loads((directory / 'manifest.json').read_text())
            for stage in ('baseline', 'campaign', 'rejudge'):
                if manifest['stages'][stage]['status'] in ('not_run', 'blocked'):
                    runner.stage_status(directory, stage, 'blocked', error='doctor_readiness_check_failed')
            generate_report(directory)
            print(f'Provider or source check failed. See {directory / "doctor.json"}')
            return 1
        manifest = json.loads((directory / 'manifest.json').read_text())
        if manifest['stages']['controls']['status'] == 'not_run':
            runner.controls(directory, config)
        manifest = json.loads((directory / 'manifest.json').read_text())
        if manifest['stages']['controls']['status'] != 'completed':
            generate_report(directory)
            print('Backend controls failed; live experiment stopped.')
            return 1
        for stage in ('baseline', 'campaign', 'rejudge'):
            getattr(runner, stage)(directory, config)
            generate_report(directory)
            manifest = json.loads((directory / 'manifest.json').read_text())
            if manifest['stages'][stage]['status'] != 'completed':
                print(f'{stage} incomplete; evidence and report saved in {directory}')
                return 1
    else:
        if args.command in ('baseline', 'campaign', 'rejudge'):
            doctor_path = directory / 'doctor.json'
            check = json.loads(doctor_path.read_text()) if doctor_path.exists() else {}
            if not check.get('ok') or check.get('offline') or check.get('provider_only'):
                print('Run doctor successfully (without --offline/--provider-only) before live stages.')
                return 1
        getattr(runner, args.command)(directory, config)
        generate_report(directory)
    print(f'Evidence and report: {directory}')
    manifest = json.loads((directory / 'manifest.json').read_text())
    stage = 'rejudge' if args.command == 'run' else args.command
    return 0 if manifest['stages'][stage]['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
