import os
import time
from decimal import Decimal


def dynamo_to_json_safe(val):
    """integration テスト応答を JSON 直列化可能にする。"""
    if isinstance(val, Decimal):
        return str(val)
    if isinstance(val, dict):
        return {k: dynamo_to_json_safe(v) for k, v in val.items()}
    if isinstance(val, list):
        return [dynamo_to_json_safe(v) for v in val]
    return val


def get_s3_latest_save_entry(config, get_client):
    """S3 バージョニング有効バケットにおける save キーの最新エントリ（Version または DeleteMarker）。"""
    s3 = get_client('s3')
    bucket = config.get('s3_bucket_name')
    key = config.get('save_file_key')
    if not bucket or not key:
        return None
    resp = s3.list_object_versions(Bucket=bucket, Prefix=key)
    versions = [v for v in resp.get('Versions', []) if v.get('Key') == key]
    delete_markers = [m for m in resp.get('DeleteMarkers', []) if m.get('Key') == key]
    latest_entry = None
    if versions:
        latest_entry = versions[0]
    if delete_markers and (not latest_entry or delete_markers[0]['LastModified'] > latest_entry['LastModified']):
        latest_entry = delete_markers[0]
    return latest_entry


def handle_integration_query_save_state(config, factorio_state_table, get_client):
    """scripts/test_runner 用: S3 最新バージョンと LatestSaveInfo を返す（Regist 権限不要）。"""
    try:
        latest = get_s3_latest_save_entry(config, get_client)
        item = None
        try:
            res = factorio_state_table.get_item(Key={'ConfigKey': 'LatestSaveInfo'})
            raw = res.get('Item')
            if raw:
                item = dynamo_to_json_safe(raw)
        except Exception as e:
            print(f"⚠️ LatestSaveInfo get failed: {e}")
        lm = latest.get('LastModified') if latest else None
        return {
            "ok": True,
            "version_id": latest.get('VersionId') if latest else None,
            "last_modified": lm.isoformat() if lm else None,
            "save_info": item,
        }
    except Exception as e:
        print(f"❌ integration_query_save_state: {e}")
        return {"ok": False, "error": str(e)}


def handle_integration_restore_save_state(event, config, factorio_state_table, get_client):
    """scripts/test_runner 用: テストで増えた S3 バージョン削除と LatestSaveInfo の巻き戻し。"""
    data = event.get('data') or {}
    delete_ids = data.get('delete_version_ids') or []
    baseline_vid = data.get('baseline_version_id')
    baseline_save_info = data.get('baseline_save_info')
    restored = True
    errors = []
    s3 = get_client('s3')
    bucket = config.get('s3_bucket_name')
    key = config.get('save_file_key')
    if not bucket or not key:
        return {"ok": False, "error": "s3_bucket_name or save_file_key missing", "errors": []}

    for vid in reversed(delete_ids):
        if not vid:
            continue
        try:
            s3.delete_object(Bucket=bucket, Key=key, VersionId=vid)
            print(f"🧹 Deleted test save version: {vid}")
        except Exception as e:
            restored = False
            errors.append(str(e))
            print(f"⚠️ Failed to delete test save version {vid}: {e}")

    if baseline_save_info:
        expr_values = {':ts': baseline_save_info['Timestamp']}
        update_expr = "set #ts = :ts"
        expr_names = {'#ts': 'Timestamp'}
        if 'FileSize' in baseline_save_info:
            update_expr += ", #sz = :sz"
            expr_names['#sz'] = 'FileSize'
            expr_values[':sz'] = baseline_save_info['FileSize']
        try:
            factorio_state_table.update_item(
                Key={'ConfigKey': 'LatestSaveInfo'},
                UpdateExpression=update_expr,
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values
            )
            print("🧹 Restored LatestSaveInfo to baseline.")
        except Exception as e:
            restored = False
            errors.append(str(e))
            print(f"⚠️ Failed to restore LatestSaveInfo: {e}")
    else:
        try:
            factorio_state_table.delete_item(Key={'ConfigKey': 'LatestSaveInfo'})
            print("🧹 Removed LatestSaveInfo to match baseline absence.")
        except Exception as e:
            restored = False
            errors.append(str(e))
            print(f"⚠️ Failed to delete LatestSaveInfo: {e}")

    if baseline_vid:
        current = get_s3_latest_save_entry(config, get_client)
        if not current or current.get('VersionId') != baseline_vid:
            restored = False
            print("⚠️ Latest S3 save version did not return to baseline.")

    return {"ok": restored, "errors": errors}


def handle_integration_detect_startup_failure(event, config, get_client):
    """
    scripts/test_runner 用:
    直近ログから起動後ストレージ確認失敗を検出する。
    """
    try:
        lookback_seconds = int((event.get('data') or {}).get('lookback_seconds', 240))
    except Exception:
        lookback_seconds = 240

    function_name = os.getenv('AWS_LAMBDA_FUNCTION_NAME') or config.get('executor_lambda_name') or 'Factorio_Executor'
    log_group_name = f"/aws/lambda/{function_name}"
    start_ms = int((time.time() - max(30, lookback_seconds)) * 1000)

    try:
        logs = get_client('logs')
        resp = logs.filter_log_events(
            logGroupName=log_group_name,
            startTime=start_ms,
            interleaved=True
        )
        hit_messages = []
        for ev in resp.get('events', []):
            msg = ev.get('message', '')
            if (
                "Startup storage check failed" in msg
                or "Startup aborted due to storage path check failure" in msg
                or "ストレージ確認に失敗" in msg
            ):
                hit_messages.append(msg)
        return {
            "ok": True,
            "detected": bool(hit_messages),
            "messages": hit_messages[-10:]
        }
    except Exception as e:
        print(f"⚠️ integration_detect_startup_failure failed: {e}")
        return {"ok": False, "error": str(e)}


def handle_integration_upgrade_factorio(event, config, get_client, factorio_state_table, upgrade_fn):
    """
    ID:088 運用用: ActiveInstanceId（または data.instance_id）上の headless を更新する。
    test_mode 必須。
    """
    data = event.get('data') or {}
    instance_id = (data.get('instance_id') or '').strip()
    if not instance_id and factorio_state_table:
        try:
            res = factorio_state_table.get_item(Key={'ConfigKey': 'ActiveInstanceId'})
            instance_id = (res.get('Item') or {}).get('Value') or ''
            instance_id = str(instance_id).strip()
        except Exception as e:
            return {"ok": False, "error": f"Failed to read ActiveInstanceId: {e}"}
    if not instance_id:
        return {"ok": False, "error": "instance_id is required (or ActiveInstanceId must be set)"}

    # per-call override of target version
    version_override = (data.get('version') or '').strip()
    if version_override:
        config = dict(config)
        config['factorio_version'] = version_override

    force = bool(data.get('force', True))
    restart = bool(data.get('restart', True))
    try:
        timeout_seconds = int(data.get('timeout_seconds', 240))
    except Exception:
        timeout_seconds = 240

    ssm = get_client('ssm')
    ok, status, out, err, skipped = upgrade_fn(
        config,
        ssm,
        instance_id,
        timeout_seconds=timeout_seconds,
        force=force,
        restart=restart,
    )
    return {
        "ok": ok,
        "status": status,
        "stdout": out,
        "stderr": err,
        "skipped": skipped,
        "instance_id": instance_id,
        "version": (config.get('factorio_version') or 'stable'),
    }


def handle_integration_set_server_name(event, config, get_client, factorio_state_table, set_name_fn):
    """test_mode 運用用: server-settings の name を更新する。"""
    data = event.get('data') or {}
    server_name = (data.get('name') or '').strip()
    if not server_name:
        return {"ok": False, "error": "data.name is required"}

    instance_id = (data.get('instance_id') or '').strip()
    if not instance_id and factorio_state_table:
        try:
            res = factorio_state_table.get_item(Key={'ConfigKey': 'ActiveInstanceId'})
            instance_id = str((res.get('Item') or {}).get('Value') or '').strip()
        except Exception as e:
            return {"ok": False, "error": f"Failed to read ActiveInstanceId: {e}"}
    if not instance_id:
        return {"ok": False, "error": "instance_id is required (or ActiveInstanceId must be set)"}

    restart = bool(data.get('restart', True))
    try:
        timeout_seconds = int(data.get('timeout_seconds', 90))
    except Exception:
        timeout_seconds = 90

    ssm = get_client('ssm')
    ok, status, out, err = set_name_fn(
        config,
        ssm,
        instance_id,
        server_name,
        timeout_seconds=timeout_seconds,
        restart=restart,
    )
    return {
        "ok": ok,
        "status": status,
        "stdout": out,
        "stderr": err,
        "instance_id": instance_id,
        "name": server_name,
    }


def _resolve_active_instance_id(event, factorio_state_table):
    data = event.get('data') or {}
    instance_id = (data.get('instance_id') or '').strip()
    if instance_id:
        return instance_id
    if not factorio_state_table:
        return ''
    try:
        res = factorio_state_table.get_item(Key={'ConfigKey': 'ActiveInstanceId'})
        return str((res.get('Item') or {}).get('Value') or '').strip()
    except Exception:
        return ''


def handle_integration_inspect_saves(event, config, get_client, factorio_state_table, run_ssm_fn):
    """test_mode 用: セーブパスとサービス設定を収集する。"""
    instance_id = _resolve_active_instance_id(event, factorio_state_table)
    if not instance_id:
        return {"ok": False, "error": "instance_id is required (or ActiveInstanceId must be set)"}
    unit = (config.get('service_unit_name') or '').strip() or 'factorio-prod'
    settings_path = f"/mnt/factorio-data/config/{(config.get('server_settings_file_name') or 'server-settings.json').strip()}"
    command = f"""
set +e
echo '===unit==='
systemctl cat {unit} 2>&1 | head -80
echo '===is-active==='
systemctl is-active {unit} 2>&1
echo '===mount==='
mountpoint /mnt/factorio-data 2>&1; mount | grep factorio-data || true
echo '===saves==='
ls -la --full-time /mnt/factorio-data/saves 2>&1 | head -40
echo '===local_saves==='
ls -la --full-time /opt/factorio/saves 2>&1 | head -20
echo '===settings_name_autosave==='
python3 - <<'PY'
import json
from pathlib import Path
p=Path({settings_path!r})
if p.exists():
    d=json.loads(p.read_text(encoding='utf-8'))
    print('name=', d.get('name'))
    print('autosave_interval=', d.get('autosave_interval'))
    print('autosave_slots=', d.get('autosave_slots'))
    print('auto_pause=', d.get('auto_pause'))
else:
    print('missing', p)
PY
echo '===journal==='
journalctl -u {unit} -n 40 --no-pager 2>&1
""".strip()
    ssm = get_client('ssm')
    ok, status, out, err = run_ssm_fn(ssm, instance_id, [command], timeout_seconds=60)
    return {
        "ok": ok,
        "status": status,
        "stdout": out,
        "stderr": err,
        "instance_id": instance_id,
    }


def handle_integration_fix_save_permissions(event, config, get_client, factorio_state_table, run_ssm_fn):
    """
    test_mode 用: S3 Files 上の saves を factorio 書き込み可にし、
    ローカル最新オートセーブがあれば save.zip に反映する。
    """
    instance_id = _resolve_active_instance_id(event, factorio_state_table)
    if not instance_id:
        return {"ok": False, "error": "instance_id is required (or ActiveInstanceId must be set)"}
    data = event.get('data') or {}
    promote_autosave = bool(data.get('promote_autosave', True))
    restart = bool(data.get('restart', False))
    run_server_save = bool(data.get('run_server_save', False))
    unit = (config.get('service_unit_name') or '').strip() or 'factorio-prod'
    restart_cmd = f"sudo systemctl restart {unit}; systemctl is-active {unit}" if restart else "echo restart_skipped=1"
    promote_flag = "1" if promote_autosave else "0"
    server_save_flag = "1" if run_server_save else "0"
    command = f"""
set -e
echo '===before==='
ls -la --full-time /mnt/factorio-data/saves 2>&1 | head -20 || true
ls -la --full-time /opt/factorio/saves 2>&1 | head -20 || true
sudo mkdir -p /mnt/factorio-data/saves /mnt/factorio-data/config /mnt/factorio-data/mods
sudo chown -R factorio:factorio /mnt/factorio-data/saves /mnt/factorio-data/config /mnt/factorio-data/mods
sudo chmod -R u+rwX /mnt/factorio-data/saves /mnt/factorio-data/config /mnt/factorio-data/mods
if [ "{promote_flag}" = "1" ]; then
  latest="$(ls -1t /opt/factorio/saves/_autosave*.zip 2>/dev/null | head -1 || true)"
  if [ -n "$latest" ]; then
    echo "promote_from=$latest"
    sudo -u factorio cp -f "$latest" /mnt/factorio-data/saves/save.zip
  else
    echo "promote_from=none"
  fi
fi
if [ "{server_save_flag}" = "1" ]; then
  echo '===server-save==='
  set +e
  set -a
  # shellcheck source=/etc/factorio.prod.env
  . /etc/factorio.prod.env 2>/dev/null || . /etc/factorio.env 2>/dev/null || true
  set +a
  python3 - <<'PY'
import os, socket, struct
ip = '127.0.0.1'
port_raw = (os.environ.get('RCON_PORT') or '26247').strip()
port_raw = port_raw.replace(chr(39), '').replace(chr(34), '')
port = int(port_raw)
password = (os.environ.get('RCON_PASSWORD') or '').strip()
password = password.replace(chr(39), '').replace(chr(34), '')
command = '/server-save'
print('rcon_target=127.0.0.1:%s pwd_len=%s' % (port, len(password)))
try:
    with socket.create_connection((ip, port), timeout=15) as sock:
        def send_packet(p_type, p_body):
            p_id = 0x1234
            packet = struct.pack('<ii', p_id, p_type) + p_body.encode('utf-8') + b'\\x00\\x00'
            sock.sendall(struct.pack('<i', len(packet)) + packet)
            header = sock.recv(4)
            if not header:
                return None
            p_len = struct.unpack('<i', header)[0]
            p_data = sock.recv(p_len)
            return p_data[8:-2].decode('utf-8')
        send_packet(3, password)
        print('rcon_server_save=', send_packet(2, command))
except Exception as e:
    print('rcon_server_save_error=', e)
PY
  set -e
fi
echo '===after==='
ls -la --full-time /mnt/factorio-data/saves 2>&1 | head -20
{restart_cmd}
""".strip()
    ssm = get_client('ssm')
    ok, status, out, err = run_ssm_fn(ssm, instance_id, [command], timeout_seconds=120)
    return {
        "ok": ok,
        "status": status,
        "stdout": out,
        "stderr": err,
        "instance_id": instance_id,
        "run_server_save": run_server_save,
        "promote_autosave": promote_autosave,
    }
