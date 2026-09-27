#!/usr/bin/env python3
"""GSPro recovery configuration wizard. Python 3.10+, standard library only.

Preparation reads authenticated recovery material and writes a new private directory.
--apply writes env, S3 configs/credentials and recovery downloader/docs after staged restore.
Neither mode contacts a network, installs packages, starts services or rotates data keys.
"""
import argparse
import ast
import base64
import contextlib
import datetime
import getpass
import hashlib
import ipaddress
import json
import os
import posixpath
import re
import shlex
import stat
import subprocess
import sys
import tarfile
import tempfile
import warnings
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

VERSION = '1.0.0'
SUPPORTED_DOMAIN = 'gspro.store'
APP_ROOT = '/opt/gspro'
BACKUP_ROOT = '/root/backups'
ENV_FILES = ('.env', '.env.runtime', '.env.telegram')
EXCLUDED_RUNTIME = {'POSTGRES_PASSWORD', 'POSTGRES_USER', 'POSTGRES_DB', 'DATABASE_URL'}
FROZEN = {
    'POSTGRES_PASSWORD', 'RUNTIME_DB_USER', 'RUNTIME_DB_PASSWORD',
    'APP_ENCRYPTION_KEY_BASE64', 'GAME_CREDENTIALS_KEY_BASE64', 'MAIL_ENCRYPTION_KEY',
    'SESSION_SECRET', 'UID_HMAC_SECRET', 'IP_HASH_SECRET', 'CHECKOUT_TOKEN_SECRET',
    'TG_BRIDGE_SECRET', 'TG_MINIAPP_SECRET',
    'APP_TAG', 'TG_ADMIN_TAG', 'TG_BOT_TAG', 'RELEASE_VERSION',
}
EDITABLE = {
    'SELLER_BRAND': ('Бренд магазина', 'text'),
    'SELLER_EMAIL': ('Контактный email продавца', 'email'),
    'PD_OPERATOR_REGISTRY_NUMBER': ('Номер в реестре операторов ПД, если применим', 'text'),
    'MIN_ORDER_RUB': ('Минимальная сумма заказа, руб.', 'decimal'),
    'MAX_ORDER_RUB': ('Максимальная сумма заказа, руб.', 'decimal'),
    'DEFAULT_MARKUP_PERCENT': ('Прежняя наценка по умолчанию, %', 'decimal'),
    'ORDER_EXPIRY_MINUTES': ('Срок неоплаченного заказа, минут', 'positive'),
    'UNPAID_ORDER_RETENTION_DAYS': ('Хранение неоплаченных заказов, дней', 'positive'),
    'SUPPORT_TICKET_RETENTION_DAYS': ('Хранение обращений, дней', 'positive'),
    'AUDIT_RETENTION_DAYS': ('Хранение аудита, дней', 'positive'),
    'ACCOUNT_DELETE_DELAY_DAYS': ('Отсрочка удаления аккаунта, дней', 'positive'),
    'EMAIL_FROM': ('Резервный EMAIL_FROM; SMTP из БД имеет приоритет', 'text'),
    'EMAIL_API_URL': ('Резервный HTTPS email API; SMTP из БД имеет приоритет', 'optional_url'),
    'EMAIL_API_TOKEN': ('Токен резервного email API', 'secret'),
    'PLATEGA_MERCHANT_ID': ('Merchant ID Platega', 'secret'),
    'PLATEGA_SECRET': ('Секрет Platega', 'secret'),
    'TELEGRAM_BOT_TOKEN': ('Токен старого контура уведомлений сайта', 'secret'),
    'TELEGRAM_CHAT_ID': ('Chat ID старого контура сайта', 'integer'),
    'TG_BOT_TOKEN': ('Токен действующего Telegram worker / Mini App', 'secret'),
    'TG_CHAT_ID': ('Chat ID действующего Telegram-контура', 'integer'),
    'TG_AUDIT_ACTOR_ID': ('Telegram ID актора аудита', 'positive'),
}


class SetupError(Exception):
    pass


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def value_digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def reject_symlinks(path):
    path = Path(path).absolute()
    for item in (path, *path.parents):
        if item.is_symlink():
            raise SetupError('Символические ссылки в рабочих путях запрещены: ' + str(item))
    return path


def verify_manifest(root, filename, required=()):
    root = reject_symlinks(root).resolve()
    manifest = reject_symlinks(root / filename)
    if not manifest.is_file():
        raise SetupError('Отсутствует манифест: ' + str(manifest))
    count = 0
    members = set()
    for line in manifest.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not re.fullmatch(r'[0-9a-fA-F]{64}', parts[0]):
            raise SetupError('Некорректный манифест SHA256')
        relative = parts[1].lstrip('*').removeprefix('./')
        pure = PurePosixPath(relative)
        if pure.is_absolute() or '..' in pure.parts or '\\' in relative:
            raise SetupError('Небезопасный путь в манифесте')
        canonical = pure.as_posix()
        if canonical in members:
            raise SetupError('Повтор файла в манифесте')
        members.add(canonical)
        target = reject_symlinks(root / relative)
        if not target.is_file() or digest(target) != parts[0].lower():
            raise SetupError('SHA256 не совпал: ' + relative)
        count += 1
    if not count:
        raise SetupError('Пустой манифест')
    if not set(required) <= members:
        raise SetupError('Манифест не покрывает все используемые обязательные файлы')
    return count


def parse_env(text):
    result = {}
    for line in text.splitlines():
        match = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$', line)
        if not match:
            if line.strip() and not line.lstrip().startswith('#'):
                raise SetupError('Неподдерживаемая строка env; проверьте исходный снимок')
            continue
        key, value = match.groups()
        value = value.strip()
        if value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("\\'", "'")
        elif value.startswith('"') and value.endswith('"'):
            value = re.sub(r'\\([\\"$])', lambda m: m[1], value[1:-1])
        if key in result:
            raise SetupError('Повтор переменной в исходном env: ' + key)
        result[key] = value
    return result


def encode_env(value):
    if any(c in value for c in '\r\n\x00'):
        raise SetupError('Многострочные значения env запрещены')
    # Compose dotenv: escaped dollar prevents interpolation; quoted escapes also
    # support a final backslash, which cannot be represented safely single-quoted.
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$') + '"'


def update_env(original, updates):
    lines = []
    seen = set()
    for line in original.splitlines():
        match = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
        key = match[1] if match else None
        if key in updates:
            lines.append(key + '=' + encode_env(updates[key]))
            seen.add(key)
        else:
            lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            lines.append(key + '=' + encode_env(value))
    return '\n'.join(lines) + '\n'


def read_snapshot(snapshot):
    checked = verify_manifest(snapshot, 'SHA256SUMS', ['application.tar.gz', 'images.tar.gz', 'images.json', 'host-packages.txt', 'database.dump', 'telegram-state.tar.gz'])
    archive = snapshot / 'application.tar.gz'
    files = {}
    with tarfile.open(archive, 'r:gz') as tar:
        for member in tar:
            name = member.name.removeprefix('./')
            pure = PurePosixPath(name)
            if pure.is_absolute() or '..' in pure.parts or '\\' in name or not (name == 'gspro' or name.startswith('gspro/')):
                raise SetupError('Небезопасная структура application.tar.gz')
            if member.isdev() or member.isfifo():
                raise SetupError('Специальные файлы в application.tar.gz запрещены')
            if member.issym() or member.islnk():
                link = member.linkname
                target = posixpath.normpath(posixpath.join(posixpath.dirname(name), link)) if member.issym() else posixpath.normpath(link)
                if link.startswith('/') or not target.startswith('gspro/'):
                    raise SetupError('Ссылка выходит за пределы приложения')
            if name in {'gspro/' + n for n in ENV_FILES}:
                if not member.isfile() or member.size > 1024 * 1024 or name in files:
                    raise SetupError('Некорректный env в архиве')
                files[name] = tar.extractfile(member).read().decode('utf-8-sig')
    if set(files) != {'gspro/' + n for n in ENV_FILES}:
        raise SetupError('Нужны три исходных env-файла в полном снимке')
    texts = {name: files['gspro/' + name] for name in ENV_FILES}
    envs = {name: parse_env(text) for name, text in texts.items()}
    env = envs['.env']
    for key in ['APP_ENCRYPTION_KEY_BASE64', 'GAME_CREDENTIALS_KEY_BASE64', 'MAIL_ENCRYPTION_KEY']:
        try:
            valid = len(base64.b64decode(env.get(key, ''), validate=True)) == 32
        except ValueError:
            valid = False
        if not valid:
            raise SetupError('Неверный исходный ключ: ' + key)
    for key in ['SESSION_SECRET', 'UID_HMAC_SECRET', 'IP_HASH_SECRET', 'CHECKOUT_TOKEN_SECRET', 'POSTGRES_PASSWORD']:
        if len(env.get(key, '')) < 32:
            raise SetupError('Неверное исходное значение: ' + key)
    if env.get('RUNTIME_DB_USER') != 'gspro_runtime' or not re.fullmatch(r'[0-9a-f]{32,}', env.get('RUNTIME_DB_PASSWORD', '')):
        raise SetupError('Неожиданная runtime роль / пароль')
    for key in FROZEN & envs['.env.runtime'].keys() & env.keys():
        if envs['.env.runtime'][key] != env[key]:
            raise SetupError('Исходные .env и .env.runtime различаются: ' + key)
    for key in ['TG_BRIDGE_SECRET', 'TG_MINIAPP_SECRET']:
        if len(envs['.env.telegram'].get(key, '')) < 32:
            raise SetupError('Неверное исходное значение: ' + key)
    images = json.loads((snapshot / 'images.json').read_text(encoding='utf-8'))
    tags = {tag: image['Id'] for image in images for tag in image.get('RepoTags', [])}
    merged = env | envs['.env.telegram']
    needed = ['gspro-web:' + merged['APP_TAG'], 'gspro-mini:' + merged['TG_ADMIN_TAG'], 'gspro-bot:' + merged['TG_BOT_TAG'], 'postgres:17-alpine', 'caddy:2-alpine']
    if any(tag not in tags for tag in needed):
        raise SetupError('Теги env не совпали с images.json')
    return texts, envs, {tag: tags[tag] for tag in needed}, checked


def validate_application(env, telegram):
    if env.get('DOMAIN') != SUPPORTED_DOMAIN:
        raise SetupError('Этот мастер поддерживает проверенные образы gspro.store; другой домен требует пересборки и отдельной проверки')
    if env.get('DEPLOY_MODE') != 'production' or env.get('COOKIE_SECURE') != 'true':
        raise SetupError('Для этого production-снимка нужны DEPLOY_MODE=production и COOKIE_SECURE=true')
    for key in ['DATA_RESIDENCY_CONFIRMED', 'LEGAL_TEXTS_APPROVED', 'PAYMENT_FISCALIZATION_CONFIGURED', 'PROCESSOR_AGREEMENTS_CONFIRMED', 'PLATEGA_ENABLED']:
        if env.get(key) != 'true':
            raise SetupError('Нужно исходное проверенное подтверждение: ' + key)
    if env.get('TELEGRAM_SEND_UID') == 'true' and env.get('CROSS_BORDER_TRANSFER_REVIEWED') != 'true':
        raise SetupError('Нужно CROSS_BORDER_TRANSFER_REVIEWED=true для передачи UID')
    validate('email', env.get('SELLER_EMAIL', ''))
    preflight_keys = ['SESSION_SECRET', 'APP_ENCRYPTION_KEY_BASE64', 'UID_HMAC_SECRET', 'IP_HASH_SECRET', 'CHECKOUT_TOKEN_SECRET', 'SELLER_EMAIL']
    if any(re.search(r'example|000000|укажите|заполня|test', env.get(k, ''), re.I) for k in preflight_keys):
        raise SetupError('Обязательное значение содержит демонстрационный маркер; production preflight отклонит запуск')
    for key in ['PLATEGA_MERCHANT_ID', 'PLATEGA_SECRET']:
        if not env.get(key):
            raise SetupError('Не заполнено: ' + key)
    secrets = [env[k] for k in ['SESSION_SECRET', 'UID_HMAC_SECRET', 'IP_HASH_SECRET', 'CHECKOUT_TOKEN_SECRET']]
    if len(set(secrets)) != len(secrets):
        raise SetupError('SESSION/HMAC/token secrets должны быть независимыми')
    minimum = float(validate('decimal', env.get('MIN_ORDER_RUB', '100')))
    maximum = float(validate('decimal', env.get('MAX_ORDER_RUB', '150000')))
    if minimum <= 0 or maximum <= minimum:
        raise SetupError('MAX_ORDER_RUB должен превышать положительный MIN_ORDER_RUB')
    for key in ['TG_BOT_TOKEN', 'TG_CHAT_ID', 'TG_AUDIT_ACTOR_ID']:
        if not telegram.get(key):
            raise SetupError('Не заполнено: ' + key)
    if not re.fullmatch(r'\d{5,}:[A-Za-z0-9_-]{20,}', telegram['TG_BOT_TOKEN']):
        raise SetupError('Неожиданный формат TG_BOT_TOKEN')
    if not re.fullmatch(r'-100\d+', telegram['TG_CHAT_ID']):
        raise SetupError('TG_CHAT_ID должен быть ID супергруппы вида -100...')
    validate('positive', telegram['TG_AUDIT_ACTOR_ID'])


def validate(kind, value):
    if any(c in value for c in '\r\n\x00'):
        raise SetupError('Допустима одна строка')
    if kind == 'ip':
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            raise SetupError('Нужен IP-адрес') from None
        if not ip.is_global:
            raise SetupError('Нужен публичный IP нового VPS')
    elif kind in ('positive', 'port', 'integer'):
        if not re.fullmatch(r'-?\d+', value):
            raise SetupError('Нужно целое число')
        number = int(value)
        if kind == 'positive' and number <= 0:
            raise SetupError('Число должно быть положительным')
        if kind == 'port' and not 1 <= number <= 65535:
            raise SetupError('Порт должен быть от 1 до 65535')
    elif kind == 'decimal':
        if not re.fullmatch(r'\d+(?:\.\d+)?', value):
            raise SetupError('Нужно неотрицательное число; дробная часть через точку')
    elif kind in ('url', 'optional_url') and value:
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in value) or '|' in value:
            raise SetupError('Нужен HTTPS URL без пароля, query и fragment')
    elif kind == 'email':
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
            raise SetupError('Нужен корректный email')
    elif kind == 'domain':
        if not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', value):
            raise SetupError('Нужно имя домена без https://, пути и порта')
    elif kind == 's3_prefix':
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/_-]*', value) or '..' in value or value.endswith('/'):
            raise SetupError('Некорректный S3 prefix')
    elif kind == 'bucket':
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', value):
            raise SetupError('Некорректное имя бакета')
    elif kind == 'region':
        if not re.fullmatch(r'[A-Za-z0-9_-]+', value):
            raise SetupError('Некорректный регион подписи')
    elif kind == 'version':
        if not re.fullmatch(r'[A-Za-z0-9:~.+_-]+', value):
            raise SetupError('Некорректная версия пакета')
    return value


def ask(label, default=None, kind='text', secret=False, required=True):
    while True:
        shown = '[сохранённое значение]' if secret and default else default
        hint = ' [' + str(shown) + ']' if shown is not None else ''
        if secret:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error', getpass.GetPassWarning)
                    raw = getpass.getpass(label + hint + ': ')
            except getpass.GetPassWarning:
                raise SetupError('Скрытый ввод недоступен; запустите мастер в настоящем SSH-терминале') from None
        else:
            raw = input(label + hint + ': ')
        value = (raw if secret else raw.strip()) or (default if default is not None else '')
        if not value and required:
            print('Значение обязательно.')
            continue
        try:
            return validate(kind, value) if value else ''
        except SetupError as error:
            print(str(error))


def yes(label):
    return input(label + ' [нет; да/yes для согласия]: ').strip().lower() in ('да', 'yes', 'y')


def read_public_key(path):
    path = reject_symlinks(path)
    raw = path.read_text(encoding='utf-8').strip()
    parts = raw.split()
    if len(parts) < 2 or parts[0] not in ['ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521', 'sk-ssh-ed25519@openssh.com'] or '\n' in raw:
        raise SetupError('Нужен один публичный OpenSSH ключ, без options/private key')
    try:
        blob = base64.b64decode(parts[1], validate=True)
        size = int.from_bytes(blob[:4], 'big')
        if blob[4:4 + size].decode('ascii') != parts[0] or len(blob) < 40:
            raise ValueError
    except (ValueError, UnicodeError):
        raise SetupError('Повреждённый публичный SSH-ключ') from None
    return raw + '\n'


def patch_installer(source, settings, kit_path):
    for key, value in settings.items():
        replacement = key + '=' + shlex.quote(str(value))
        source, count = re.subn(r'^' + re.escape(key) + r'=.*$', lambda _: replacement, source, count=1, flags=re.M)
        if count != 1:
            raise SetupError('В установщике нет параметра: ' + key)
    source, count = re.subn(r'^RECOVERY_KIT_DIR=.*$', lambda _: 'RECOVERY_KIT_DIR=' + shlex.quote(str(kit_path)), source, count=1, flags=re.M)
    if count != 1:
        raise SetupError('Неизвестная версия установщика')
    # The wizard verifies the one current root archive, never old release variants.
    return source.replace('set -euo pipefail\n', "set -euo pipefail\nexport RELEASE_VARIANT=''\n", 1)


def configured_downloader(source):
    tree = ast.parse(source)
    assignments = [n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'PROVIDERS' for t in n.targets)]
    if len(assignments) != 1:
        raise SetupError('Неизвестный формат downloader')
    node = assignments[0]
    replacement = '''def configured_providers():
    import json
    root = Path(__file__).resolve().parent
    config_root = root / 'backup-system'
    if not (config_root / 's3-config.json').is_file():
        config_root = root.parent
    result = {}
    for provider, name in [('timeweb', 's3-config.json'), ('regru', 'regru-s3-config.json')]:
        config = json.loads((config_root / name).read_text(encoding='utf-8'))
        result[provider] = (config['endpoint'], config['region'], config['bucket'])
    return result

PROVIDERS = configured_providers()'''
    lines = source.splitlines()
    return '\n'.join(lines[:node.lineno - 1] + [replacement] + lines[node.end_lineno:]) + '\n'


def configured_readme(source, providers):
    for provider, label in [('timeweb', 'Timeweb'), ('regru', 'REG.RU')]:
        cfg = providers[provider][1]
        row = '| ' + label + ' | ' + cfg['endpoint'] + ' | ' + cfg['bucket'] + ' | ' + cfg['region'] + ' |'
        source, count = re.subn(r'^\| ' + re.escape(label) + r' \|.*$', lambda _: row, source, count=1, flags=re.M)
        if count != 1:
            raise SetupError('Неизвестная таблица S3 в README')
    return source + '\n\nDownloader этой редакции получает endpoint/bucket/region из backup-system JSON выбранного комплекта; live-копия на VPS может читать конфигурации из /root/backups. Credentials отдельно запрашиваются скрыто.\n'


def secure_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    reject_symlinks(path)
    fd, temporary = tempfile.mkstemp(prefix='.gspro-', dir=path.parent)
    try:
        os.fchmod(fd, mode) if hasattr(os, 'fchmod') else os.chmod(temporary, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data.encode('utf-8') if isinstance(data, str) else data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(output, kit, snapshot):
    output = reject_symlinks(output).resolve(strict=False)
    if output.exists():
        raise SetupError('Каталог результата уже существует; выберите новый --output')
    kit = reject_symlinks(kit).resolve()
    snapshot = reject_symlinks(snapshot).resolve()
    for forbidden in (kit, snapshot, Path(APP_ROOT), Path(BACKUP_ROOT)):
        if output == forbidden or forbidden in output.parents or output in forbidden.parents:
            raise SetupError('Каталог настройки должен быть отдельно от kit/snapshot/app/backups')
    kit_count = verify_manifest(kit, 'MANIFEST.sha256', ['README.md', 'download-backup.py', 'tools/deploy-new-vps.sh', 'backup-system/s3-config.json', 'backup-system/regru-s3-config.json'])
    texts, envs, images, snap_count = read_snapshot(snapshot)
    env = envs['.env']
    domain = validate('domain', env['DOMAIN'])
    print('\nПроверено файлов: снимок', snap_count, '/ комплект', kit_count)
    validate_application(env, envs['.env.telegram'])
    print('DOMAIN из снимка:', domain, '(для проверенных образов этого комплекта; сохраняется)')
    print('Оригинальные crypto/session/HMAC/DB ключи и теги сохраняются автоматически.')
    server_ip = ask('Публичный IP НОВОГО VPS', kind='ip')
    ssh_port = ask('Порт SSH на новом VPS', '22', 'port')
    key_path = Path(ask('Файл ПУБЛИЧНОГО SSH-ключа владельца', '/root/owner-public-key.pub'))
    public_key = read_public_key(key_path)
    default_monitor = '92.53.116.12,92.53.116.111,92.53.116.119'
    monitor = ask('IP мониторинга через запятую; - отключает доступ 10050', default_monitor)
    monitor_ips = [] if monitor == '-' else [validate('ip', x.strip()) for x in monitor.split(',')]
    packages = {}
    for line in (snapshot / 'host-packages.txt').read_text(encoding='utf-8').splitlines():
        bits = line.split()
        if len(bits) >= 2:
            packages[bits[0]] = bits[1]
    versions = {'DOCKER_VERSION': packages.get('docker-ce'), 'COMPOSE_VERSION': packages.get('docker-compose-plugin'), 'CONTAINERD_VERSION': packages.get('containerd.io'), 'BUILDX_VERSION': packages.get('docker-buildx-plugin')}
    if any(not v for v in versions.values()):
        raise SetupError('В host-packages.txt нет закреплённых версий Docker/Compose/containerd/buildx')
    print('Версии пакетов берутся из host-packages.txt; сетевую доступность проверяют перед установкой.')
    if yes('Изменить закреплённые версии (только после отдельной проверки совместимости)'):
        versions = {k: ask(k, v, 'version') for k, v in versions.items()}
    updates = {}
    tg_updates = {}
    if yes('Дополнительно изменить контакты, ограничения или токены транспорта'):
        print('Enter сохраняет исходное. Ключи шифрования, пароли БД и теги не редактируются.')
        for name, (description, kind) in EDITABLE.items():
            target = envs['.env.telegram'] if name.startswith('TG_') else env
            if name not in target:
                continue
            old = target[name]
            new = ask(name + ' - ' + description, old, kind, secret=(kind == 'secret'), required=False)
            if new != old:
                (tg_updates if name.startswith('TG_') else updates)[name] = new
    effective = env | updates
    validate_application(effective, envs['.env.telegram'] | tg_updates)
    root_env = update_env(texts['.env'], updates)
    runtime_env = '\n'.join(line for line in root_env.splitlines() if line.split('=', 1)[0].strip() not in EXCLUDED_RUNTIME) + '\n'
    tg_env = update_env(texts['.env.telegram'], tg_updates)
    provider_data = {}
    for provider, filename in [('timeweb', 's3-config.json'), ('regru', 'regru-s3-config.json')]:
        print('\nНастройка хранилища:', provider)
        config = json.loads((kit / 'backup-system' / filename).read_text(encoding='utf-8'))
        for key, kind in [('endpoint', 'url'), ('bucket', 'bucket'), ('region', 'region')]:
            config[key] = ask(provider + ' ' + key, config[key], kind)
        config['prefix'] = 'gspro/backups'
        config['backup_root'] = BACKUP_ROOT
        credentials_name = 's3-credentials.json' if provider == 'timeweb' else 'regru-s3-credentials.json'
        config['credentials_file'] = BACKUP_ROOT + '/' + credentials_name
        if provider == 'timeweb':
            config['additional_config_files'] = [BACKUP_ROOT + '/regru-s3-config.json']
        else:
            config['receipt_identity'] = True
        credentials = {'access_key': ask(provider + ' Access Key', secret=True), 'secret_key': ask(provider + ' Secret Key', secret=True)}
        provider_data[provider] = (filename, config, credentials_name, credentials)
    first = provider_data['timeweb'][1]
    second = provider_data['regru'][1]
    if (first['endpoint'], first['bucket']) == (second['endpoint'], second['bucket']):
        raise SetupError('Два хранилища должны быть независимыми')
    settings = {'DOMAIN': domain, 'NEW_SERVER_IP': server_ip, 'SNAPSHOT_DIR': str(snapshot), 'APP_ROOT': APP_ROOT, 'SSH_PORT': ssh_port, 'SSH_PUBLIC_KEY_FILE': str(output / 'owner-public-key.pub'), 'DOCKER_IMAGE_ARCHIVE': str(snapshot / 'images.tar.gz'), 'START_PUBLIC_SERVICES': 'false', **versions}
    installer = patch_installer((kit / 'tools' / 'deploy-new-vps.sh').read_text(encoding='utf-8'), settings, kit)
    installer, count = re.subn(r'^MONITORING_IPS=.*$', lambda _: 'MONITORING_IPS=(' + ' '.join(shlex.quote(x) for x in monitor_ips) + ')', installer, count=1, flags=re.M)
    if count != 1:
        raise SetupError('В установщике не найдена настройка мониторинга')
    print('\nБудет подготовлено:', output)
    print('IP:', server_ip, '| DOMAIN:', domain, '| SSH:', ssh_port)
    print('START_PUBLIC_SERVICES=false. Подготовка не запускает установку и не обращается в сеть.')
    if not yes('Сохранить подготовленные файлы'):
        print('Отмена. Файлы не созданы.')
        return None
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = Path(tempfile.mkdtemp(prefix='.gspro-setup-', dir=output.parent))
    os.chmod(stage, 0o700)
    try:
        payload = {'app/.env': root_env, 'app/.env.runtime': runtime_env, 'app/.env.telegram': tg_env}
        payload['backups/recovery-kit/download-backup.py'] = configured_downloader((kit / 'download-backup.py').read_text(encoding='utf-8'))
        payload['backups/recovery-kit/README.md'] = configured_readme((kit / 'README.md').read_text(encoding='utf-8'), provider_data)
        for _, (filename, config, credentials_name, credentials) in provider_data.items():
            payload['backups/' + filename] = json.dumps(config, ensure_ascii=False, indent=2) + '\n'
            payload['backups/' + credentials_name] = json.dumps(credentials, ensure_ascii=False, indent=2) + '\n'
        files = {}
        for relative, text in payload.items():
            secure_write(stage / relative, text)
            files[relative] = digest(stage / relative)
        secure_write(stage / 'owner-public-key.pub', public_key)
        secure_write(stage / 'deploy-new-vps.configured.sh', installer, 0o700)
        metadata = {'format': 1, 'wizard_version': VERSION, 'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'domain': domain, 'new_server_ip': server_ip, 'ssh_port': int(ssh_port), 'kit': str(kit), 'snapshot': str(snapshot), 'app_root': APP_ROOT, 'backup_root': BACKUP_ROOT, 'public_services': False, 'files': files, 'images': images, 'frozen_hashes': {name: {k: value_digest(v) for k, v in values.items() if k in FROZEN} for name, values in envs.items()}, 'editable_changed': sorted(updates) + sorted(tg_updates), 'manifest_checks': {'kit': kit_count, 'snapshot': snap_count}}
        secure_write(stage / 'setup.json', json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
        summary = '# Подготовленная настройка GSPro\n\nIP: ' + server_ip + '\n\nDOMAIN: ' + domain + '\n\nSSH port: ' + ssh_port + '\n\nПубличные службы выключены до переключения.\n\nИзменённые переменные: ' + (', '.join(metadata['editable_changed']) or 'нет') + '\n\nСледующие команды выполняются владельцем на НОВОМ сервере.\n\n```bash\nbash ' + shlex.quote(str(output / 'deploy-new-vps.configured.sh')) + '\npython3 ' + shlex.quote(str(Path(__file__).resolve())) + ' --apply ' + shlex.quote(str(output)) + '\n```\n\nЗатем сверить ID образов, DNS, SMTP, Mini App и S3 по полной инструкции.\n'
        secure_write(stage / 'setup-summary.md', summary)
        if output.exists():
            raise SetupError('Каталог результата появился во время ввода; запись остановлена')
        os.rename(stage, output)
    except BaseException:
        # Keep private partial material for owner inspection; never delete unrelated paths.
        print('При незавершённой записи приватный временный каталог:', stage)
        raise
    print('Готово:', output / 'setup-summary.md')
    return metadata


def check_linux_root():
    if os.name != 'posix' or not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise SetupError('Мастер выполняется root на новом Linux VPS')
    release = parse_env(Path('/etc/os-release').read_text(encoding='utf-8'))
    if release.get('ID') != 'ubuntu' or release.get('VERSION_ID') != '24.04':
        raise SetupError('Нужен Ubuntu 24.04')


def check_root_owned(path, private=False):
    path = reject_symlinks(path)
    for item in (path, *path.parents):
        info = item.stat()
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            raise SetupError('Путь должен принадлежать root и не разрешать запись другим: ' + str(item))
    if private and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SetupError('Приватный файл/каталог разрешает доступ другим: ' + str(path))


def frozen_fingerprints(envs):
    return {name: {k: value_digest(v) for k, v in values.items() if k in FROZEN} for name, values in envs.items()}


@contextlib.contextmanager
def configuration_locks(app):
    import fcntl
    with contextlib.ExitStack() as stack:
        for path in (app / '.deploy.lock', Path('/run/lock/gspro-backup.lock')):
            reject_symlinks(path)
            if path.parent == Path('/run/lock'):
                # Ubuntu's root-owned /run/lock may legitimately be sticky 1777.
                for parent in (path.parent, *path.parent.parents):
                    info = parent.stat()
                    sticky_lock_dir = parent == Path('/run/lock') and info.st_mode & stat.S_ISVTX
                    if info.st_uid != 0 or (stat.S_IMODE(info.st_mode) & 0o022 and not sticky_lock_dir):
                        raise SetupError('Небезопасный каталог блокировок')
            else:
                check_root_owned(path.parent)
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
            lock = stack.enter_context(os.fdopen(fd, 'a'))
            info = os.fstat(lock.fileno())
            if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o022:
                raise SetupError('Небезопасный файл блокировки')
            os.fchmod(lock.fileno(), 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SetupError('Развёртывание или backup выполняется; повторите после завершения') from None
        yield


def apply(directory):
    check_linux_root()
    directory = reject_symlinks(directory).resolve()
    check_root_owned(directory, private=True)
    app = reject_symlinks(APP_ROOT)
    if not (app / 'deploy/dc').is_file():
        raise SetupError('Сначала выполните staged restore штатным установщиком')
    check_root_owned(app)
    with configuration_locks(app):
        return apply_locked(directory)


def apply_locked(directory):
    if (directory / 'applied.json').exists():
        raise SetupError('Эта настройка уже применена; повторная запись запрещена')
    check_root_owned(directory / 'setup.json', private=True)
    metadata = json.loads((directory / 'setup.json').read_text(encoding='utf-8'))
    if metadata.get('format') != 1 or metadata.get('app_root') != APP_ROOT or metadata.get('backup_root') != BACKUP_ROOT or metadata.get('public_services') is not False:
        raise SetupError('Неверный формат подготовленной настройки')
    expected_files = {'app/' + n for n in ENV_FILES} | {'backups/s3-config.json', 'backups/regru-s3-config.json', 'backups/s3-credentials.json', 'backups/regru-s3-credentials.json', 'backups/recovery-kit/download-backup.py', 'backups/recovery-kit/README.md'}
    if set(metadata.get('files', {})) != expected_files:
        raise SetupError('Неверный набор файлов настройки')
    # The editable JSON cannot choose which data keys we protect.
    _, original_envs, original_images, _ = read_snapshot(Path(metadata['snapshot']))
    fingerprints = frozen_fingerprints(original_envs)
    if metadata.get('frozen_hashes') != fingerprints or metadata.get('images') != original_images:
        raise SetupError('Карта неизменяемых значений/образов отличается от проверенного исходного снимка')
    app = reject_symlinks(APP_ROOT)
    backups = reject_symlinks(BACKUP_ROOT)
    if not (app / 'deploy/dc').is_file() or not (backups / 'recovery-kit/download-backup.py').is_file():
        raise SetupError('Сначала выполните staged restore штатным установщиком')
    running = subprocess.run(['docker', 'ps', '--format', '{{.Label "com.docker.compose.service"}}|{{.Label "com.docker.compose.project.working_dir"}}'], capture_output=True, text=True)
    if running.returncode:
        raise SetupError('Docker не готов; применение остановлено')
    for line in running.stdout.splitlines():
        service, working_dir = line.split('|', 1)
        if working_dir == APP_ROOT and service not in ('db', ''):
            raise SetupError('Публичные службы уже запущены; сначала остановите их по инструкции')
    for relative, checksum in metadata['files'].items():
        source = reject_symlinks(directory / relative)
        check_root_owned(source, private=True)
        if stat.S_IMODE(source.stat().st_mode) & 0o077 or digest(source) != checksum:
            raise SetupError('Права или checksum подготовленного файла неверны: ' + relative)
        if relative.startswith('app/'):
            name = relative.split('/', 1)[1]
            live = parse_env((app / name).read_text(encoding='utf-8'))
            prepared = parse_env(source.read_text(encoding='utf-8'))
            for key, frozen in fingerprints[name].items():
                if value_digest(live.get(key, '')) != frozen or value_digest(prepared.get(key, '')) != frozen:
                    raise SetupError('Оригинальное значение не совпало: ' + key)
        target = (app if relative.startswith('app/') else backups) / relative.split('/', 1)[1]
        reject_symlinks(target)
        if target.exists() and not target.is_file():
            raise SetupError('Ожидался обычный файл назначения')
        if target.exists():
            check_root_owned(target)
    prepared_envs = {name: parse_env((directory / 'app' / name).read_text(encoding='utf-8')) for name in ENV_FILES}
    validate_application(prepared_envs['.env'], prepared_envs['.env.telegram'])
    if prepared_envs['.env.runtime'] != {k: v for k, v in prepared_envs['.env'].items() if k not in EXCLUDED_RUNTIME}:
        raise SetupError('Prepared runtime должен точно соответствовать .env без административных DB-переменных')
    print('Будут записаны три env, четыре S3-файла и два файла recovery-kit; службы не запускаются.')
    if not yes('Применить подготовленные настройки к этому восстановленному VPS'):
        print('Отмена. Файлы проекта не изменены.')
        return
    before = directory / 'previous-config'
    if before.exists():
        raise SetupError('Обнаружен предыдущий или частично выполненный apply; нужна ручная проверка')
    before.mkdir(mode=0o700)
    written = []
    try:
        for relative in sorted(expected_files):
            target = (app if relative.startswith('app/') else backups) / relative.split('/', 1)[1]
            existed = target.exists()
            original = target.read_bytes() if existed else None
            if existed:
                secure_write(before / relative, original)
            secure_write(target, (directory / relative).read_bytes())
            written.append((target, original))
        secure_write(directory / 'applied.json', json.dumps({'applied_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'files': sorted(expected_files), 'services_started': False}, indent=2) + '\n')
    except BaseException:
        failures = []
        for target, original in reversed(written):
            try:
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    secure_write(target, original)
            except BaseException:
                failures.append(str(target))
        if failures:
            raise SetupError('Применение и частичный rollback остановлены. Восстановите из previous-config: ' + ', '.join(failures)) from None
        raise SetupError('Применение остановлено; уже записанные файлы возвращены. Проверьте previous-config.') from None
    print('Настройки применены. Исходные файлы сохранены в previous-config с правами 600.')


def main():
    parser = argparse.ArgumentParser(description='Интерактивная подготовка настройки GSPro для нового Ubuntu 24.04 VPS')
    parser.add_argument('--version', action='version', version=VERSION)
    parser.add_argument('--kit', default='/root/gspro-recovery/recovery-kit')
    parser.add_argument('--snapshot', default='/root/restore-prod11')
    parser.add_argument('--output', default='/root/gspro-setup')
    parser.add_argument('--apply', metavar='DIRECTORY', help='Применить ранее подготовленные настройки после staged restore')
    args = parser.parse_args()
    try:
        os.umask(0o077)
        if args.apply:
            apply(Path(args.apply))
        else:
            check_linux_root()
            if not sys.stdin.isatty():
                raise SetupError('Запустите мастер в терминале; скрытый ввод секретов требует TTY')
            prepare(Path(args.output), Path(args.kit), Path(args.snapshot))
    except (SetupError, OSError, ValueError, KeyError, tarfile.TarError, json.JSONDecodeError) as error:
        # Never emit raw OS/provider errors, entered secrets, traceback or env values.
        print('Остановка:', str(error) if isinstance(error, SetupError) else type(error).__name__, file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print('\nОтмена ввода.', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
