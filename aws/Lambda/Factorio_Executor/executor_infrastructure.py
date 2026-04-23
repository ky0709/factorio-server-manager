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


def ensure_s3files_runtime_paths(config, ssm, instance_id, run_fn=None):
    """
    起動後に S3 Files マウントと必須パスを確認する。
    run_fn: 既定で run_ssm_shell_and_wait
    """
    run = run_fn or run_ssm_shell_and_wait
    server_settings_path = resolve_server_settings_path(config)
    command = (
        "set -e; "
        "sudo mkdir -p /mnt/factorio-data /mnt/factorio-data/saves /mnt/factorio-data/mods /mnt/factorio-data/config; "
        "mountpoint -q /mnt/factorio-data || sudo mount -a; "
        "mountpoint -q /mnt/factorio-data; "
        "test -d /mnt/factorio-data/mods; "
        f"test -f {server_settings_path}"
    )
    return run(ssm, instance_id, [command], timeout_seconds=45)


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
