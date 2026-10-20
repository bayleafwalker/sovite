#!/usr/bin/env python3
"""Sign deployment evidence on the operator host through a loopback DB tunnel.

The private key never enters a workload. The database connection uses the tunnel,
while the signed runtime identity retains the deployment's actual database host.
"""
import argparse
import json
from pathlib import Path
import subprocess

import yaml
from sqlalchemy.engine import make_url
from sovite.db import Database, MetadataRow
from sovite.policy import load_bundle
from sovite.qualification import issue_qualification, runtime_settings_fingerprint
from sovite.settings import Settings


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--context', required=True)
    p.add_argument('--namespace', required=True)
    p.add_argument('--bundle', type=Path, required=True)
    p.add_argument('--values', type=Path, required=True, help='Rendered effective values')
    p.add_argument('--runtime-env', type=Path, required=True)
    p.add_argument('--private-key', type=Path, required=True)
    p.add_argument('--public-key', type=Path, required=True)
    p.add_argument('--evidence', type=Path, required=True)
    p.add_argument('--database-port', type=int, required=True)
    p.add_argument('--valid-days', type=int, default=1)
    args = p.parse_args()
    values = yaml.safe_load(args.values.read_text())
    credentials = json.loads(args.runtime_env.read_text())
    bundle, generation = load_bundle(args.bundle)
    settings = Settings(database_url=credentials['SOVITE_DATABASE_URL'],
        database_schema=values['database']['schema'] or None, database_migrate=values['database']['migrate'],
        artifact_backend='s3', s3_endpoint=values['artifacts']['endpoint'] or f"http://{values['fullnameOverride']}-garage:3900",
        s3_bucket=values['artifacts']['bucket'], s3_region=values['artifacts']['region'],
        public_origin=values['publicOrigin'], secure_cookies=values['secureCookies'],
        require_qualification=values['qualification']['required'], deployment_binding=values['deploymentBinding'],
        qualification_private_key=str(args.private_key), qualification_public_keys=[str(args.public_key)])
    observed = subprocess.run(['kubectl', '--context', args.context, '-n', args.namespace, 'exec',
        f"deploy/{values['fullnameOverride']}-control", '--', 'sovite', 'runtime-binding'],
        check=True, capture_output=True, text=True)
    remote = json.loads(observed.stdout)['runtime_binding']
    if remote != runtime_settings_fingerprint(settings):
        raise ValueError('Live runtime differs from the reviewed settings; refusing to sign')
    url = make_url(settings.database_url).set(host='127.0.0.1', port=args.database_port)
    db = Database(url.render_as_string(hide_password=False), schema=settings.database_schema)
    db.check()
    with db.sessions() as session:
        active = session.get(MetadataRow, 'active_generation')
        if not active or active.value['generation'] != generation:
            raise ValueError('Live generation differs from the reviewed bundle; refusing to sign')
    evidence = json.loads(args.evidence.read_text())
    row = issue_qualification(db, settings, generation, bundle['default_profile'], evidence,
                              valid_days=args.valid_days, issuer='appservice-operator')
    print(json.dumps({'id': row.id, 'passed': row.passed, 'runtime_binding': remote,
                      'generation': generation, 'expires_at': row.expires_at}))


if __name__ == '__main__':
    main()
