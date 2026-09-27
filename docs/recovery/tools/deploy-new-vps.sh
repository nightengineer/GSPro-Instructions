#!/usr/bin/env bash
# CHANGEABLE SETTINGS — review these before running on a NEW EMPTY VPS.
DOMAIN="gspro.store"
NEW_SERVER_IP="CHANGE_ME"
SNAPSHOT_DIR="/root/restore-prod11" # extracted, authenticated backup
APP_ROOT="/opt/gspro"
SSH_PORT="22"
SSH_PUBLIC_KEY_FILE="/root/owner-public-key.pub"
DOCKER_VERSION="5:29.8.1-1~ubuntu.24.04~noble"
COMPOSE_VERSION="5.5.1-1~ubuntu.24.04~noble"
CONTAINERD_VERSION="2.3.5-1~ubuntu.24.04~noble"
BUILDX_VERSION="0.37.1-1~ubuntu.24.04~noble"
DOCKER_IMAGE_ARCHIVE="$SNAPSHOT_DIR/images.tar.gz" # optional docker-save archive; otherwise builds from restored source
START_PUBLIC_SERVICES="false" # Reviewed copy supports staged restore only
MONITORING_IPS=(92.53.116.12 92.53.116.111 92.53.116.119)
# END CHANGEABLE SETTINGS. Internal constants below follow the restored project.
set -euo pipefail
RECOVERY_KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
umask 077
export GSPRO_APP_ROOT="$APP_ROOT"
export GSPRO_BASE_URL="https://$DOMAIN"
[[ $EUID = 0 && "$NEW_SERVER_IP" != CHANGE_ME ]] || { echo 'Fill variables and run as root'; exit 1; }
[[ "$START_PUBLIC_SERVICES" = false ]] || { echo 'Refused: use staged restore and a separate checked cutover'; exit 1; }
source /etc/os-release
[[ "$ID" = ubuntu && "$VERSION_ID" = 24.04 ]] || { echo 'Ubuntu 24.04 required'; exit 1; }
[[ ! -e "$APP_ROOT" && -s "$SSH_PUBLIC_KEY_FILE" ]] || { echo 'Existing app or missing owner SSH public key'; exit 1; }
cd "$SNAPSHOT_DIR";sha256sum -c SHA256SUMS
APPLICATION_ARCHIVE="$SNAPSHOT_DIR/application.tar.gz"
case "${RELEASE_VARIANT:-}" in
 "") ;;
 production-1.1|no-production-1.1) APPLICATION_ARCHIVE="$SNAPSHOT_DIR/variants/$RELEASE_VARIANT/application.tar.gz" ;;
 *) echo 'Unknown RELEASE_VARIANT'; exit 1 ;;
esac
[[ -s "$APPLICATION_ARCHIVE" ]] || { echo 'Selected release archive is missing'; exit 1; }
apt-get update
apt-get install -y ca-certificates curl openssl python3 python3-boto3 python3-cryptography rsync ufw fail2ban unattended-upgrades
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable\n' "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y "docker-ce=$DOCKER_VERSION" "docker-ce-cli=$DOCKER_VERSION" "containerd.io=$CONTAINERD_VERSION" "docker-buildx-plugin=$BUILDX_VERSION" "docker-compose-plugin=$COMPOSE_VERSION"
systemctl enable --now docker
install -d -m 700 /root/.ssh
touch /root/.ssh/authorized_keys;chmod 600 /root/.ssh/authorized_keys
grep -qxF "$(cat "$SSH_PUBLIC_KEY_FILE")" /root/.ssh/authorized_keys || cat "$SSH_PUBLIC_KEY_FILE" >> /root/.ssh/authorized_keys
cat > /etc/ssh/sshd_config.d/00-gspro-prod.conf <<EOF
Port $SSH_PORT
PermitRootLogin prohibit-password
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
sshd -t
ufw allow "$SSH_PORT/tcp"
ufw allow 80/tcp;ufw allow 443/tcp;ufw allow 443/udp
for ip in "${MONITORING_IPS[@]}";do ufw allow from "$ip" to any port 10050 proto tcp;done
ufw --force enable
systemctl reload ssh
printf '[sshd]\nenabled = true\nbackend = systemd\n' > /etc/fail2ban/jail.d/gspro.conf
systemctl enable --now fail2ban
printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades
if [[ ! -e /swapfile ]];then
 fallocate -l 2G /swapfile;chmod 600 /swapfile;mkswap /swapfile;swapon /swapfile
 printf '/swapfile none swap sw 0 0\n' >> /etc/fstab
fi
# Never overwrite all /etc on another machine: network, host keys and disk IDs differ.
mkdir -p "$(dirname "$APP_ROOT")"
tar -xzf "$APPLICATION_ARCHIVE" -C "$(dirname "$APP_ROOT")"
[[ -d "$APP_ROOT" ]] || { echo 'Archive application path mismatch'; exit 1; }
install -d -m 700 /root/backups
cp "$SNAPSHOT_DIR/system/config.json" /root/backups/
cp "$SNAPSHOT_DIR/system/recipient.crt" /root/backups/
cp "$SNAPSHOT_DIR/system/s3-config.json" /root/backups/
if [[ -f "$SNAPSHOT_DIR/system/regru-s3-config.json" ]]; then cp "$SNAPSHOT_DIR/system/regru-s3-config.json" /root/backups/; fi
if [[ -d "$SNAPSHOT_DIR/system/recovery-kit" ]]; then cp -a "$SNAPSHOT_DIR/system/recovery-kit" /root/backups/; fi
if [[ -f "$SNAPSHOT_DIR/system/recovery-private.encrypted.pem" ]]; then cp "$SNAPSHOT_DIR/system/recovery-private.encrypted.pem" /root/backups/; fi
cp -a "$SNAPSHOT_DIR/system/scripts" /root/backups/
if [[ ! -f /root/backups/regru-s3-config.json || ! -f /root/backups/scripts/multi_s3_store.py ]]; then
 # Current independent kit bridges snapshots created just before the second S3 was added.
 [[ -f "$APP_ROOT/deploy/backup-release-variants.py" ]] || { echo 'Older snapshot: adapt compatible backup scripts before enabling mirroring'; exit 1; }
 [[ -f "$RECOVERY_KIT_DIR/backup-system/regru-s3-config.json" ]] || { echo 'Download the complete independent recovery kit'; exit 1; }
 install -m 600 "$RECOVERY_KIT_DIR/backup-system/s3-config.json" /root/backups/s3-config.json
 install -m 600 "$RECOVERY_KIT_DIR/backup-system/regru-s3-config.json" /root/backups/regru-s3-config.json
 for script in backup.py s3_store.py multi_s3_store.py publish_recovery.py; do
  install -m 700 "$RECOVERY_KIT_DIR/backup-system/scripts/$script" "/root/backups/scripts/$script"
 done
fi
# Preserve the combined notification format even when restoring an older snapshot.
if [[ -f "$RECOVERY_KIT_DIR/backup-system/scripts/notify.mjs" ]]; then
 for script in backup.py notify.py notify.mjs; do
  install -m 700 "$RECOVERY_KIT_DIR/backup-system/scripts/$script" "/root/backups/scripts/$script"
 done
 install -m 700 "$RECOVERY_KIT_DIR/backup-system/scripts/notify.mjs" "$APP_ROOT/deploy/backup-notify.mjs"
fi
if [[ ! -d /root/backups/recovery-kit ]]; then
 install -d -m 700 /root/backups/recovery-kit
 cp "$RECOVERY_KIT_DIR/README.md" "$RECOVERY_KIT_DIR/download-backup.py" /root/backups/recovery-kit/
 cp -a "$RECOVERY_KIT_DIR/tools" /root/backups/recovery-kit/
fi
python3 - "$APP_ROOT" <<'PY'
import json,sys
from pathlib import Path
p=Path('/root/backups/config.json');c=json.loads(p.read_text());c['app_root']=sys.argv[1];c['deployment_root']='/opt/gspro/Восстановление';p.write_text(json.dumps(c,indent=2)+'\n');p.chmod(0o600)
PY
if [[ -d "$SNAPSHOT_DIR/system/deployment" ]];then mkdir -p /opt/gspro/Восстановление; cp -a "$SNAPSHOT_DIR/system/deployment/." /opt/gspro/Восстановление/;fi
# Install only explicitly selected GSPro units from the system configuration archive.
python3 - "$SNAPSHOT_DIR/system/etc.tar.gz" <<'PY'
import tarfile,sys
from pathlib import Path
with tarfile.open(sys.argv[1]) as t:
 for m in t:
  p=Path(m.name)
  if m.isfile() and p.parent.as_posix()=='etc/systemd/system' and p.name.startswith(('gspro-backup','gspro-platega-reconcile')) and p.suffix in ('.timer','.service'):
   target=Path('/etc/systemd/system')/p.name;target.write_bytes(t.extractfile(m).read());target.chmod(0o644)
PY
systemctl daemon-reload
python3 - "$APP_ROOT" "$DOMAIN" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1])/'.env';lines=p.read_text().splitlines()
lines=[('DOMAIN='+sys.argv[2]) if x.startswith('DOMAIN=') else x for x in lines]
p.write_text('\n'.join(lines)+'\n');p.chmod(0o600)
PY
bash "$APP_ROOT/deploy/fix-permissions.sh"
if [[ -n "$DOCKER_IMAGE_ARCHIVE" ]];then docker load -i "$DOCKER_IMAGE_ARCHIVE";fi
"$APP_ROOT/deploy/dc" up -d --wait db
bash "$APP_ROOT/deploy/restore.sh" "$SNAPSHOT_DIR"
python3 - "$APP_ROOT" <<'PY'
from pathlib import Path
import sys,subprocess
e=dict(x.split('=',1) for x in (Path(sys.argv[1])/'.env').read_text().splitlines() if '=' in x and not x.startswith('#'))
user=e['RUNTIME_DB_USER'];pwd=e['RUNTIME_DB_PASSWORD']
assert user=='gspro_runtime' and all(c in '0123456789abcdef' for c in pwd)
sql=f"CREATE ROLE {user} LOGIN PASSWORD '{pwd}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION; GRANT CONNECT ON DATABASE gspro TO {user}; GRANT USAGE ON SCHEMA public TO {user}; GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {user}; REVOKE INSERT,UPDATE,DELETE ON TABLE public.\"_prisma_migrations\" FROM {user}; GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {user}; ALTER DEFAULT PRIVILEGES FOR ROLE gspro IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO {user}; ALTER DEFAULT PRIVILEGES FOR ROLE gspro IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO {user};"
subprocess.run([str(Path(sys.argv[1])/'deploy/dc'),'exec','-T','db','psql','-U','gspro','-d','gspro','-v','ON_ERROR_STOP=1','--single-transaction'],input=sql,text=True,check=True,stdout=subprocess.DEVNULL)
PY
if [[ -z "$DOCKER_IMAGE_ARCHIVE" ]];then "$APP_ROOT/deploy/dc" build web telegram-admin telegram-bot;fi
if [[ -f "$APP_ROOT/app/scripts/purge-completed-passwords.mjs" ]];then
 "$APP_ROOT/deploy/dc" run --rm --no-deps --entrypoint node web scripts/purge-completed-passwords.mjs
fi
if [[ "$START_PUBLIC_SERVICES" = true ]];then
 "$APP_ROOT/deploy/dc" up -d --no-build --wait
 bash "$APP_ROOT/deploy/verify-miniapp.sh"
 systemctl enable --now gspro-platega-reconcile.timer
 python3 /root/backups/scripts/s3_store.py check
 systemctl start gspro-backup@daily.service
 systemctl enable --now gspro-backup-recover.service gspro-backup-daily.timer gspro-backup-monthly.timer gspro-backup-notify.timer gspro-backup-watchdog.timer
else
 echo 'Restored DB/SQLite. Public services and Telegram deliberately remain stopped until cutover.'
fi
echo "GSPro units restored selectively. In staged mode enable backup timers after cutover and a successful encrypted test backup. Configure provider monitoring separately for $NEW_SERVER_IP."
