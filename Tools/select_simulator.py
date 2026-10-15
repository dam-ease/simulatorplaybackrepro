#!/usr/bin/env python3
import argparse
import json
import subprocess

parser=argparse.ArgumentParser()
parser.add_argument('--os', help='Require an installed runtime, for example 26.2.')
parser.add_argument('--family', choices=['iphone', 'ipad'], default='iphone')
args=parser.parse_args()
required=tuple(int(n) for n in args.os.split('.')) if args.os else None
devices=json.loads(subprocess.check_output(['xcrun','simctl','list','devices','available','--json']))['devices']
candidates=[]
for runtime, entries in devices.items():
    if 'iOS-' not in runtime:
        continue
    version=tuple(int(n) for n in runtime.split('iOS-')[-1].split('-'))
    if version[0]<18:
        continue
    if required and version[:len(required)]!=required:
        continue
    for device in entries:
        prefix = 'iPad' if args.family == 'ipad' else 'iPhone'
        if device.get('isAvailable') and device['name'].startswith(prefix):
            large = '13-inch' in device['name'] if args.family == 'ipad' else 'Pro Max' in device['name']
            candidates.append((version, large, device['name'], device['udid']))
if not candidates:
    raise SystemExit(f'Install an {args.family} Simulator running iOS {args.os or "18 or later"} in Xcode Settings > Components.')
print(sorted(candidates,reverse=True)[0][-1])
