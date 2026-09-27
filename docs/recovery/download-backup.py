#!/usr/bin/env python3
"""Download a backup and encrypted recovery key without the original VPS."""
import argparse
import getpass
import hashlib
import os
from pathlib import Path
import re
import sys
import warnings

PROVIDERS = {
    'timeweb': ('https://s3.twcstorage.ru', 'ru-1', 'c621752d-bf59-48fc-bc71-eaad18817ae8'),
    'regru': ('https://s3.regru.cloud', 'us-east-1', 'backups-gspro'),
}

def fetch(client, bucket, key, target, expected=None):
    target = Path(target)
    if target.exists() or target.is_symlink():
        raise FileExistsError('Destination must be new')
    partial = target.with_name(target.name + '.partial')
    response = client.get_object(Bucket=bucket, Key=key)
    h = hashlib.sha256()
    count = 0
    try:
        with partial.open('xb') as stream:
            for chunk in iter(lambda: response['Body'].read(1024 * 1024), b''):
                stream.write(chunk)
                h.update(chunk)
                count += len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if count != response['ContentLength']:
            raise ValueError('Incomplete download')
        expected = expected or response.get('Metadata', {}).get('sha256')
        if not expected or h.hexdigest() != expected:
            raise ValueError('SHA256 mismatch or missing integrity metadata')
        partial.rename(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        response['Body'].close()
    return h.hexdigest()

def main():
    import boto3
    from botocore.config import Config
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('provider', choices=PROVIDERS)
    parser.add_argument('--kind', choices=['daily', 'monthly', 'pre-release'], default='daily')
    parser.add_argument('--snapshot-key', help='Exact object key; otherwise choose interactively')
    parser.add_argument('--output', default='./downloaded-backup', help='New output directory')
    args = parser.parse_args()
    os.umask(0o077)
    warnings.simplefilter('error', getpass.GetPassWarning)
    endpoint, region, bucket = PROVIDERS[args.provider]
    access = getpass.getpass('S3 Access Key: ').strip()
    secret = getpass.getpass('S3 Secret Key: ').strip()
    client = boto3.client('s3', endpoint_url=endpoint, region_name=region,
        aws_access_key_id=access, aws_secret_access_key=secret,
        config=Config(signature_version='s3v4', s3={'addressing_style':'path'},
                      connect_timeout=15, read_timeout=120, retries={'max_attempts':4}))
    prefix = 'gspro/backups/' + args.kind + '/'
    objects = []
    for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix):
        objects.extend(o for o in page.get('Contents', []) if o['Key'].endswith('.cms'))
    objects.sort(key=lambda o: o['LastModified'], reverse=True)
    if not objects:
        raise ValueError('No snapshots of this kind exist in this provider')
    if args.snapshot_key:
        key = args.snapshot_key
        if key not in {o['Key'] for o in objects}:
            raise ValueError('The requested key is not in the selected backup prefix')
    else:
        for i, obj in enumerate(objects, 1):
            print(f"{i}. {obj['Key']}  {obj['Size']} bytes  {obj['LastModified']}")
        selected = int(input('Snapshot number to restore: '))
        if not 1 <= selected <= len(objects):
            raise ValueError('Invalid snapshot number')
        key = objects[selected - 1]['Key']
    output = Path(args.output)
    if output.exists() or output.is_symlink():
        raise FileExistsError('Use a new output directory')
    manifest_response = client.get_object(Bucket=bucket, Key=key + '.sha256')
    try:
        manifest = manifest_response['Body'].read(4097)
    finally:
        manifest_response['Body'].close()
    if len(manifest) > 4096:
        raise ValueError('Oversized checksum manifest')
    match = re.fullmatch(rb'([a-fA-F0-9]{64})\s+([^\r\n]+)\r?\n?', manifest)
    if not match or match.group(2).decode() != key.rsplit('/', 1)[-1]:
        raise ValueError('Invalid checksum manifest')
    output.mkdir(mode=0o700, parents=True)
    digest = fetch(client, bucket, key, output / 'snapshot.cms', match.group(1).decode().lower())
    (output / 'snapshot.cms.sha256').write_text(digest + '  snapshot.cms\n')
    fetch(client, bucket, 'gspro/recovery-kit/keys/recovery-private.encrypted.pem', output / 'private.encrypted.pem')
    (output / 'source.txt').write_text('Endpoint: '+endpoint+'\nBucket: '+bucket+'\nKey: '+key+'\n')
    print('Downloaded and SHA256 verified. Encrypted key still requires its separate password.')

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Cancelled.', file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        print('Recovery download failed: ' + type(error).__name__ + '. No credentials printed.', file=sys.stderr)
        raise SystemExit(1)
