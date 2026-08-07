"""Factorio_Executor: SSM / DynamoDB マーカー・起動ロック等の下位処理（同一 Lambda 内分割）。"""
import os
import secrets
import time
from botocore.exceptions import ClientError


def infer_env_label():
    """SSM_PARAMETER_PATH から dev/prod を推定する。"""
    path = (os.getenv('SSM_PARAMETER_PATH') or '/factorio/').strip().lower()
    if 'dev' in path:
        return 'dev'
    return 'prod'


def run_ssm_shell_and_wait(ssm, instance_id, commands, timeout_seconds=180):
    """
    SSM RunShellScript を実行し、完了まで待機する。
    戻り値: (ok: bool, status: str, stdout: str, stderr: str)
    """
    try:
        sent = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={'commands': commands},
            TimeoutSeconds=timeout_seconds
        )
        command_id = sent['Command']['CommandId']
    except Exception as e:
        return False, "SendCommandFailed", "", str(e)

    deadline = time.time() + timeout_seconds + 10
    while time.time() < deadline:
        try:
            inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
            status = inv.get('Status', 'Unknown')
            if status in ('Success', 'Failed', 'TimedOut', 'Cancelled'):
                return (
                    status == 'Success',
                    status,
                    (inv.get('StandardOutputContent') or '').strip(),
                    (inv.get('StandardErrorContent') or '').strip()
                )
        except Exception as e:
            msg = str(e)
            if "InvocationDoesNotExist" in msg:
                pass
            else:
                return False, "GetInvocationFailed", "", msg
        time.sleep(3)

    return False, "WaitTimeout", "", f"SSM command did not finish within {timeout_seconds + 10}s"


def resolve_service_unit_name(config):
    """
    停止対象の systemd ユニット名を解決する。
    戻り値: (ssm 用コマンド文字列, used_fallback: bool)
    """
    unit = (config.get('service_unit_name') or '').strip()
    if unit:
        return f"sudo systemctl stop {unit}", False
    return "sudo systemctl stop factorio-dev || sudo systemctl stop factorio-prod || sudo systemctl stop factorio", True


def start_factorio_service(config, ssm, instance_id, run_fn=None, timeout_seconds=45):
    """
    ID:087: ストレージ確認後に Factorio ユニットを明示起動する。
    戻り値: (ok, status, stdout, stderr, used_fallback)
    """
    run = run_fn or run_ssm_shell_and_wait
    unit = (config.get('service_unit_name') or '').strip()
    if unit:
        command = (
            f"unit='{unit}'; "
            "sudo systemctl start \"$unit\"; "
            "state=$(systemctl is-active \"$unit\" 2>/dev/null || true); "
            "echo \"unit=$unit state=$state\"; "
            "case \"$state\" in active|activating) exit 0;; *) exit 1;; esac"
        )
        used_fallback = False
    else:
        command = (
            "for unit in factorio-prod factorio-dev factorio; do "
            "  if systemctl cat \"${unit}.service\" >/dev/null 2>&1; then "
            "    sudo systemctl start \"${unit}\"; "
            "    state=$(systemctl is-active \"${unit}\" 2>/dev/null || true); "
            "    echo \"unit=${unit} state=${state}\"; "
            "    case \"${state}\" in active|activating) exit 0;; esac; "
            "  fi; "
            "done; "
            "echo 'no factorio unit started'; exit 1"
        )
        used_fallback = True
    ok, status, out, err = run(ssm, instance_id, [command], timeout_seconds=timeout_seconds)
    return ok, status, out, err, used_fallback


def upgrade_factorio_headless(config, ssm, instance_id, run_fn=None, timeout_seconds=240, force=False, restart=True):
    """
    ID:088: Factorio headless を目標バージョンへ更新する。
    FACTORIO_VERSION が stable / 空なら stable チャンネル、それ以外は get-download/<ver>/headless。
    force=False のとき、現在のバイナリ出力に目標バージョン文字列が含まれていればスキップ
    （stable 指定時は force でない限りスキップしない＝呼び出し側で force を制御）。
    戻り値: (ok, status, stdout, stderr, skipped)
    """
    run = run_fn or run_ssm_shell_and_wait
    unit = (config.get('service_unit_name') or '').strip() or 'factorio-prod'
    target = (config.get('factorio_version') or 'stable').strip().strip("'\"")
    if not target:
        target = 'stable'
    force_flag = "1" if force else "0"
    restart_flag = "1" if restart else "0"
    if target.lower() == 'stable':
        download_url = "https://factorio.com/get-download/stable/headless/linux64"
        # stable はビルド番号が不定のため、force 時のみ更新。非 force は現状維持。
        match_needle = ""
    else:
        download_url = f"https://factorio.com/get-download/{target}/headless/linux64"
        match_needle = target

    command = f"""
set -e
unit='{unit}'
target='{target}'
force='{force_flag}'
restart='{restart_flag}'
url='{download_url}'
needle='{match_needle}'

current_out="$(/opt/factorio/bin/x64/factorio --version 2>&1 || true)"
echo "current_version_out=$current_out"

if [ "$force" != "1" ] && [ -n "$needle" ]; then
  if echo "$current_out" | grep -F "$needle" >/dev/null 2>&1; then
    echo "skip_upgrade=1 reason=already_on_target"
    if [ "$restart" = "1" ]; then
      sudo systemctl start "$unit" || true
    fi
    exit 0
  fi
fi

if [ "$force" != "1" ] && [ "$target" = "stable" ]; then
  echo "skip_upgrade=1 reason=stable_requires_force"
  exit 0
fi

echo "upgrade_begin target=$target force=$force"
sudo systemctl stop "$unit" || true
tmp_dir="$(mktemp -d /tmp/factorio_upgrade.XXXXXX)"
chmod 755 "$tmp_dir"
tmp_tar="$tmp_dir/factorio_headless.tar.xz"
# wget may not exist on minimal images; prefer curl
if command -v curl >/dev/null 2>&1; then
  curl -fsSL -o "$tmp_tar" "$url"
else
  wget -q -O "$tmp_tar" "$url"
fi
chmod 644 "$tmp_tar"
sudo -u factorio tar -xJf "$tmp_tar" -C /opt/factorio --strip-components=1
rm -rf "$tmp_dir"
new_out="$(/opt/factorio/bin/x64/factorio --version 2>&1 || true)"
echo "new_version_out=$new_out"
if [ -n "$needle" ] && ! echo "$new_out" | grep -F "$needle" >/dev/null 2>&1; then
  echo "upgrade_verify_failed expected_contains=$needle"
  exit 2
fi
if [ "$restart" = "1" ]; then
  sudo systemctl start "$unit"
  state=$(systemctl is-active "$unit" 2>/dev/null || true)
  echo "unit=$unit state=$state"
fi
echo "upgrade_done=1"
""".strip()

    ok, status, out, err = run(ssm, instance_id, [command], timeout_seconds=timeout_seconds)
    skipped = bool(out and "skip_upgrade=1" in out)
    return ok, status, out, err, skipped


def set_factorio_server_name(config, ssm, instance_id, server_name, run_fn=None, timeout_seconds=90, restart=True):
    """
    server-settings の name を更新し、必要ならサービスを再起動する。
    戻り値: (ok, status, stdout, stderr)
    """
    run = run_fn or run_ssm_shell_and_wait
    unit = (config.get('service_unit_name') or '').strip() or 'factorio-prod'
    settings_path = resolve_server_settings_path(config)
    # shell 安全のためシングルクォートをエスケープ
    safe_name = str(server_name or '').replace("'", "'\"'\"'")
    restart_flag = "1" if restart else "0"
    command = f"""
set -e
settings_path='{settings_path}'
unit='{unit}'
restart='{restart_flag}'

if [ ! -f "$settings_path" ]; then
  echo "missing_settings=$settings_path"
  exit 1
fi

sudo -u factorio python3 - <<'PY'
import json
from pathlib import Path
path = Path({settings_path!r})
data = json.loads(path.read_text(encoding='utf-8'))
old = data.get('name')
data['name'] = {server_name!r}
path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\\n", encoding='utf-8')
print(f"name_updated old={{old!r}} new={{data['name']!r}} path={{path}}")
PY

if [ "$restart" = "1" ]; then
  sudo systemctl restart "$unit"
  state=$(systemctl is-active "$unit" 2>/dev/null || true)
  echo "unit=$unit state=$state"
fi
""".strip()
    return run(ssm, instance_id, [command], timeout_seconds=timeout_seconds)


def resolve_server_settings_path(config):
    """
    起動前チェックで参照する server-settings ファイルパスを解決する。
    """
    file_name = (config.get('server_settings_file_name') or 'server-settings.json').strip()
    if not file_name:
        file_name = 'server-settings.json'
    return f"/mnt/factorio-data/config/{file_name}"


def consume_event_marker(factorio_state_table, marker_key):
    """
    EventBridge の重複配信対策:
    マーカーを条件付きで削除し、先着 1 実行だけ通知を許可する。
    戻り値: (acquired: bool, start_time_str)
    """
    res = factorio_state_table.get_item(Key={'ConfigKey': marker_key})
    start_time_str = res.get('Item', {}).get('Timestamp')
    try:
        factorio_state_table.delete_item(
            Key={'ConfigKey': marker_key},
            ConditionExpression="attribute_exists(ConfigKey)"
        )
        return True, start_time_str
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code == 'ConditionalCheckFailedException':
            return False, start_time_str
        raise


def acquire_start_lock(factorio_state_table, ttl_seconds=240):
    """
    /start 同時実行ガード。戻り値: (acquired: bool, token | None)
    """
    if not factorio_state_table:
        return True, None

    lock_key = 'StartActionLock'
    now = int(time.time())
    token = secrets.token_hex(8)
    expires_at = now + ttl_seconds
    try:
        factorio_state_table.put_item(
            Item={
                'ConfigKey': lock_key,
                'LockToken': token,
                'Timestamp': str(now),
                'ExpiresAt': expires_at,
            },
            ConditionExpression="attribute_not_exists(ConfigKey) OR ExpiresAt < :now",
            ExpressionAttributeValues={':now': now},
        )
        return True, token
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code == 'ConditionalCheckFailedException':
            return False, None
        raise


def release_start_lock(factorio_state_table, token):
    if not factorio_state_table or not token:
        return
    try:
        factorio_state_table.delete_item(
            Key={'ConfigKey': 'StartActionLock'},
            ConditionExpression="LockToken = :token",
            ExpressionAttributeValues={':token': token},
        )
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code != 'ConditionalCheckFailedException':
            print(f"⚠️ Failed to release StartActionLock: {e}")


def force_release_start_lock(factorio_state_table):
    if not factorio_state_table:
        return {"ok": False, "error": "DynamoDB not initialized"}
    try:
        factorio_state_table.delete_item(Key={'ConfigKey': 'StartActionLock'})
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def query_start_lock_state(factorio_state_table):
    if not factorio_state_table:
        return {"ok": False, "error": "DynamoDB not initialized"}
    try:
        res = factorio_state_table.get_item(Key={'ConfigKey': 'StartActionLock'})
        item = res.get('Item')
        if not item:
            return {"ok": True, "locked": False}
        return {
            "ok": True,
            "locked": True,
            "lock_token": item.get('LockToken'),
            "expires_at": item.get('ExpiresAt'),
            "timestamp": item.get('Timestamp'),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def ensure_s3files_runtime_paths(config, ssm, instance_id, run_fn=None, timeout_seconds=45):
    """
    起動後に S3 Files マウントと必須パスを確認する。
    run_fn: 既定で run_ssm_shell_and_wait
    """
    run = run_fn or run_ssm_shell_and_wait
    server_settings_path = resolve_server_settings_path(config)
    s3_files_system_id = (config.get('s3_files_system_id') or '').strip()
    fs_entry_fix_cmd = ""
    if s3_files_system_id:
        # /etc/fstab の /mnt/factorio-data s3files エントリを強制再生成する
        fs_entry_fix_cmd = (
            "tmp_fstab=$(mktemp); "
            "sudo awk '$2 != \"/mnt/factorio-data\" {print}' /etc/fstab | sudo tee \"$tmp_fstab\" >/dev/null; "
            f"echo '{s3_files_system_id}:/  /mnt/factorio-data  s3files  _netdev,rw  0  0' | sudo tee -a \"$tmp_fstab\" >/dev/null; "
            "sudo cp /etc/fstab /etc/fstab.bak.factorio >/dev/null 2>&1 || true; "
            "sudo mv \"$tmp_fstab\" /etc/fstab; "
        )
    command = (
        "set -e; "
        f"{fs_entry_fix_cmd}"
        "sudo mkdir -p /mnt/factorio-data /mnt/factorio-data/saves /mnt/factorio-data/mods /mnt/factorio-data/config; "
        "mountpoint -q /mnt/factorio-data || sudo mount -a; "
        "mountpoint -q /mnt/factorio-data; "
        f"if [ ! -f {server_settings_path} ]; then "
        "  for src in /opt/factorio/config/server-settings.json /opt/factorio/data/server-settings.example.json; do "
        "    if [ -f \"$src\" ]; then sudo cp \"$src\" "
        f"{server_settings_path}"
        "; break; fi; "
        "  done; "
        f"  sudo chown factorio:factorio {server_settings_path} >/dev/null 2>&1 || true; "
        "fi; "
        "test -d /mnt/factorio-data/mods; "
        f"test -f {server_settings_path}; "
        # factorio ユーザーが save.zip を更新できるよう権限を合わせる（root 所有のままだとオートセーブがローカルへ逃げる）
        "sudo chown -R factorio:factorio /mnt/factorio-data/saves /mnt/factorio-data/config /mnt/factorio-data/mods >/dev/null 2>&1 || true; "
        "sudo chmod -R u+rwX /mnt/factorio-data/saves /mnt/factorio-data/config /mnt/factorio-data/mods >/dev/null 2>&1 || true"
    )
    return run(ssm, instance_id, [command], timeout_seconds=timeout_seconds)


def update_latest_save_info(config, factorio_state_table, get_client, timestamp_iso):
    """最新セーブの時刻とサイズをカタログへ保存する。"""
    expr_names = {'#ts': 'Timestamp'}
    expr_values = {':val': timestamp_iso}
    update_expr = "set #ts = :val"

    try:
        s3 = get_client('s3')
        s3_meta = s3.head_object(Bucket=config['s3_bucket_name'], Key=config['save_file_key'])
        size_mb = round(s3_meta.get('ContentLength', 0) / (1024 * 1024), 1)
        update_expr += ", #sz = :sz"
        expr_names['#sz'] = 'FileSize'
        expr_values[':sz'] = str(size_mb)
    except Exception as e:
        print(f"⚠️ Could not fetch save size for LatestSaveInfo update: {e}")

    factorio_state_table.update_item(
        Key={'ConfigKey': 'LatestSaveInfo'},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values
    )
