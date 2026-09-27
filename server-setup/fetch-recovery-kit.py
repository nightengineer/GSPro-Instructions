#!/usr/bin/env python3
"""Download the private recovery kit on an Ubuntu server; never save S3 keys."""
import getpass
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import warnings

MAX_KIT_BYTES = 128 * 1024 * 1024
KIT_KEY = 'gspro/recovery-kit/recovery-kit.tar.gz'
PROVIDERS = {
    'regru': ('https://s3.regru.cloud', 'us-east-1', 'backups-gspro'),
    'timeweb': ('https://s3.twcstorage.ru', 'ru-1', ''),
}

def ask(label, default=''):
    value = input(f'{label}' + (f' [{default}]' if default else '') + ': ').strip()
    value = value or default
    if not value:
        raise ValueError('Required value is empty')
    return value

def secret(label):
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        value = getpass.getpass(label + ': ')
    if not value:
        raise ValueError('Required secret is empty')
    return value

def secure_directory(destination):
    if not destination.is_absolute():
        raise ValueError('Destination must be absolute')
    if destination.is_symlink():
        raise ValueError('Destination cannot be a symlink')
    for ancestor in [*reversed(destination.parents), destination]:
        if not os.path.lexists(ancestor):
            continue
        info = ancestor.lstat()
        sticky_root = info.st_uid == 0 and stat.S_ISDIR(info.st_mode) and bool(info.st_mode & stat.S_ISVTX)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()) or (info.st_mode & 0o022 and not sticky_root):
            raise ValueError('Unsafe destination ancestor')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolved = destination.resolve(strict=True)
    if resolved != destination:
        raise ValueError('Destination contains a symlink')
    info = destination.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Destination must be owned by you with mode 700')
    for name in ('recovery-kit.tar.gz', 'recovery-kit.tar.gz.sha256'):
        if os.path.lexists(destination / name):
            raise ValueError('Existing files will not be overwritten')

def download(client, bucket, destination):
    secure_directory(destination)
    checksum_object = client.get_object(Bucket=bucket, Key=KIT_KEY + '.sha256')
    with checksum_object['Body'] as body:
        raw = body.read(1025)
    text = raw.decode('ascii').strip()
    match = re.fullmatch(r'([a-fA-F0-9]{64})\s+\*?recovery-kit\.tar\.gz', text)
    if not match:
        raise ValueError('Invalid checksum file')
    expected = match.group(1).lower()
    result = client.get_object(Bucket=bucket, Key=KIT_KEY)
    size = result.get('ContentLength', 0)
    if not 0 < size <= MAX_KIT_BYTES:
        result['Body'].close()
        raise ValueError('Recovery kit size is outside the allowed range')
    fd, temp_name = tempfile.mkstemp(prefix='.kit-', dir=destination)
    temporary = Path(temp_name)
    final = destination / 'recovery-kit.tar.gz'
    committed = []
    temporary_files = [temporary]
    try:
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(fd, 'wb') as output, result['Body'] as body:
            while block := body.read(1024 * 1024):
                total += len(block)
                if total > MAX_KIT_BYTES or total > size:
                    raise ValueError('Recovery kit exceeded the announced size')
                output.write(block)
                digest.update(block)
            output.flush()
            os.fsync(output.fileno())
        if total != size or digest.hexdigest() != expected:
            raise ValueError('Recovery kit checksum or size mismatch')
        checksum_fd, checksum_temp_name = tempfile.mkstemp(prefix='.checksum-', dir=destination)
        checksum_temporary = Path(checksum_temp_name)
        temporary_files.append(checksum_temporary)
        with os.fdopen(checksum_fd, 'w', encoding='ascii') as output:
            output.write(expected + '  recovery-kit.tar.gz\n')
            output.flush()
            os.fsync(output.fileno())
        # Hard links commit both staged files without replacing any path.
        checksum_path = destination / 'recovery-kit.tar.gz.sha256'
        for source, target in ((temporary, final), (checksum_temporary, checksum_path)):
            os.link(source, target)
            committed.append((target, source.stat().st_dev, source.stat().st_ino))
        return expected
    except Exception:
        for target, device, inode in reversed(committed):
            try:
                info = target.lstat()
                if (info.st_dev, info.st_ino) == (device, inode):
                    target.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        for path in temporary_files:
            path.unlink(missing_ok=True)

def main():
    if sys.platform != 'linux' or not sys.stdin.isatty():
        raise ValueError('Run interactively in an Ubuntu SSH terminal')
    os.umask(0o077)
    import boto3
    from botocore.config import Config
    print('Скачивание recovery-kit. Ключи S3 не сохраняются и не выводятся.')
    provider = ask('S3: regru или timeweb', 'regru').lower()
    if provider not in PROVIDERS:
        raise ValueError('Select regru or timeweb')
    endpoint, region, bucket = PROVIDERS[provider]
    endpoint = ask('S3 endpoint', endpoint)
    if not endpoint.startswith('https://'):
        raise ValueError('S3 endpoint must use HTTPS')
    bucket = ask('S3 bucket', bucket)
    region = ask('S3 region', region)
    access = secret('S3 Access Key')
    key = secret('S3 Secret Key')
    destination = Path(ask('Каталог загрузки', '/root/gspro-recovery'))
    client = boto3.client('s3', endpoint_url=endpoint, region_name=region,
        aws_access_key_id=access, aws_secret_access_key=key,
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'},
            connect_timeout=15, read_timeout=120, retries={'max_attempts': 4}))
    digest = download(client, bucket, destination)
    print('Готово: recovery-kit.tar.gz и checksum, SHA256 совпал.')
    print('Каталог: ' + str(destination))
    print('SHA256: ' + digest)
    print('Продолжите раздел 4 инструкции: распаковка и скачивание полного снимка.')

if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\nОтменено. Ключи не сохранены.', file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        # SDK exceptions can contain request details; print only their type.
        print('Загрузка не выполнена (' + type(error).__name__ + '). '
            'Проверьте введённые поля, доступ S3, каталог 700 и отсутствие старых файлов. '
            'Подробности запроса и секреты не выводятся.', file=sys.stderr)
        sys.exit(1)
