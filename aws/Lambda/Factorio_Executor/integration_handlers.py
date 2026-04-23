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
