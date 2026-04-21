import json
import os
import re
import time
import secrets
import string
from datetime import datetime
from decimal import Decimal

# レイヤーからのインポート
from factorio_common.utils import JST, get_client, fetch_config_from_ssm, run_rcon_command, format_msg, notify_via_lambda

def get_msg(category, key, locale='ja', **kwargs):
    return format_msg({}, category, key, locale, **kwargs)

COLOR_GREEN, COLOR_BLUE = 0x2ECC71, 0x3498DB

config = {"initialized": False}
factorio_state_table = None
_suppressed_logs = []

def _dynamo_to_json_safe(val):
    """integration テスト応答を JSON 直列化可能にする。"""
    if isinstance(val, Decimal):
        return str(val)
    if isinstance(val, dict):
        return {k: _dynamo_to_json_safe(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_dynamo_to_json_safe(v) for v in val]
    return val


def get_s3_latest_save_entry():
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


def handle_integration_query_save_state(event):
    """scripts/test_runner 用: S3 最新バージョンと LatestSaveInfo を返す（Regist 権限不要）。"""
    try:
        latest = get_s3_latest_save_entry()
        item = None
        try:
            res = factorio_state_table.get_item(Key={'ConfigKey': 'LatestSaveInfo'})
            raw = res.get('Item')
            if raw:
                item = _dynamo_to_json_safe(raw)
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


def handle_integration_restore_save_state(event):
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
        current = get_s3_latest_save_entry()
        if not current or current.get('VersionId') != baseline_vid:
            restored = False
            print("⚠️ Latest S3 save version did not return to baseline.")

    return {"ok": restored, "errors": errors}


def update_latest_save_info(timestamp_iso):
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


def _infer_env_label():
    """SSM_PARAMETER_PATH から dev/prod を推定する。"""
    path = (os.getenv('SSM_PARAMETER_PATH') or '/factorio/').strip().lower()
    if 'dev' in path:
        return 'dev'
    return 'prod'


def _run_ssm_shell_and_wait(ssm, instance_id, commands, timeout_seconds=180):
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

    # Lambda timeout を超えにくくするため、待機余裕は最小限にする
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
            # Invocation がまだ反映されていない瞬間は再試行
            if "InvocationDoesNotExist" in msg:
                pass
            else:
                # 権限不足など恒久エラーは待機せず即失敗
                return False, "GetInvocationFailed", "", msg
        time.sleep(3)

    return False, "WaitTimeout", "", f"SSM command did not finish within {timeout_seconds + 10}s"


def _resolve_service_unit_name():
    """
    停止対象の systemd ユニット名を解決する。
    優先: SERVICE_UNIT_NAME（SSM経由: service_unit_name）
    後方互換: 未設定時は従来の候補チェーンを使う。
    """
    unit = (config.get('service_unit_name') or '').strip()
    if unit:
        return f"sudo systemctl stop {unit}", False
    return "sudo systemctl stop factorio-dev || sudo systemctl stop factorio-prod || sudo systemctl stop factorio", True


def _ensure_s3files_runtime_paths(ssm, instance_id):
    """
    起動後に S3 Files マウントと必須パスを確認する。
    ID:033 で saves/mods/config を S3 Files 側に寄せるため、RCON待機前に確認する。
    """
    command = (
        "set -e; "
        "sudo mkdir -p /mnt/factorio-data /mnt/factorio-data/saves /mnt/factorio-data/mods /mnt/factorio-data/config; "
        "mountpoint -q /mnt/factorio-data || sudo mount -a; "
        "mountpoint -q /mnt/factorio-data; "
        "test -d /mnt/factorio-data/mods; "
        "test -f /mnt/factorio-data/config/server-settings.json"
    )
    return _run_ssm_shell_and_wait(ssm, instance_id, [command], timeout_seconds=45)

def init_config():
    try:
        global _suppressed_logs
        _suppressed_logs = []

        ssm_config = fetch_config_from_ssm()
        if not ssm_config:
            print(f"⚠️ Warning: No config found in SSM. Check SSM_PARAMETER_PATH: {os.getenv('SSM_PARAMETER_PATH')}")
            return

        config.update(ssm_config)
        
        table_name = config.get('dynamodb_table_name')
        if table_name:
            global factorio_state_table
            factorio_state_table = get_client('dynamodb', True).Table(table_name)

        config["initialized"] = True
    except Exception as e:
        print(f"❌ Failed to initialize config: {e}")

def notify(content, mode='followup', event=None, embeds=None, components=None):
    # テストモード時は Discord への通知処理をスキップ
    notifier_name = config.get('notifier_lambda_name')
    if config.get("test_mode"):
        if content:
            _suppressed_logs.append(content)
        print(f"DEBUG: [Test Mode] Notification suppressed: {content}")
        return

    # グローバルなテストセッションフラグをチェック
    if factorio_state_table:
        try:
            res = factorio_state_table.get_item(Key={'ConfigKey': 'TestSessionActive'})
            item = res.get('Item')
            if item:
                # 有効期限が設定されており、かつ期限が切れている場合はフラグを無視
                exp = item.get('ExpiresAt')
                if exp and int(exp) < int(time.time()):
                    print("DEBUG: [Global Test Session] Flag expired, ignoring.")
                else:
                    if content:
                        _suppressed_logs.append(content)
                    print(f"DEBUG: [Global Test Session] Notification suppressed: {content}")
                    return
        except Exception as e:
            print(f"⚠️ Failed to check global test flag: {e}")

    if not notifier_name:
        print(f"⚠️ Cannot notify: notifier_lambda_name is missing. Content: {content}")
        return

    notify_via_lambda(
        notifier_name,
        content,
        mode=mode,
        event=event,
        embeds=embeds,
        components=components
    )

# --- アクションハンドラ定義 ---

def handle_status(event, ec2, inst, state, ip, locale):
        # カタログから現在のセーブ時刻を取得 (全状態で共通)
        save_time = "-"
        save_size = "-"
        res_cat = {}
        try:
            res_cat = factorio_state_table.get_item(Key={'ConfigKey': 'LatestSaveInfo'})
            if 'Item' in res_cat:
                save_time = datetime.fromisoformat(res_cat['Item']['Timestamp']).strftime('%Y/%m/%d %H:%M:%S')
                save_size = res_cat['Item'].get('FileSize', "-")
        except: pass

        # 1. 進行中プロセスの検知 (不整合を防ぐため、実際の状態と組み合わせて判定)
        try:
            # 停止処理中: インスタンスがまだ完全に停止していない場合のみマーカーを有効とする
            if state != 'stopped':
                res_stop = factorio_state_table.get_item(Key={'ConfigKey': 'StopStartTime'})
                if 'Item' in res_stop:
                    start_time_str = res_stop['Item'].get('Timestamp')
                    if start_time_str:
                        elapsed = int(time.time() - float(start_time_str))
                        return get_msg("status", "stopping", locale, elapsed=elapsed, save_time=save_time, size=save_size)

            # 起動処理中: 起動完了(RCON成功)で削除されるため、存在すれば表示
            res_start = factorio_state_table.get_item(Key={'ConfigKey': 'StartStartTime'})
            if 'Item' in res_start:
                if state != 'stopped': # 停止中なのに起動マーカーがある場合は無視
                    start_time_str = res_start['Item'].get('Timestamp')
                    if start_time_str:
                        elapsed = int(time.time() - float(start_time_str))
                        return get_msg("status", "starting", locale, elapsed=elapsed, save_time=save_time, size=save_size, ip=ip, port=config.get('factorio_game_port', 34197))
        except Exception as e:
            print(f"In-progress check error: {e}")

        # 2. 停止中の場合は S3 を完全にスキップ (早期リターン)
        if state == 'stopped':
            return get_msg("status", "stopped", locale, save_time=save_time, size=save_size)

        # 3. 稼働中または遷移中の場合のみ S3 を確認して同期状態を判定
        status_prefix = ""
        try:
            db_timestamp = res_cat.get('Item', {}).get('Timestamp')

            s3 = get_client('s3')
            s3_meta = s3.head_object(Bucket=config['s3_bucket_name'], Key=config['save_file_key'])
            
            # 容量の取得とMB変換
            size_bytes = s3_meta.get('ContentLength', 0)
            size_mb = size_bytes / (1024 * 1024)
            save_size = round(size_mb, 1)

            s3_dt = s3_meta['LastModified'].astimezone(JST)
            save_time = s3_dt.strftime('%Y/%m/%d %H:%M:%S')

            # 秒単位で比較するために時刻を正規化
            db_dt_norm = datetime.fromisoformat(db_timestamp).replace(microsecond=0) if db_timestamp else None
            s3_dt_norm = s3_dt.replace(microsecond=0)

            if db_dt_norm and db_dt_norm > s3_dt_norm:
                status_prefix = get_msg("status", "syncing", locale)
                # 同期中の場合はカタログ（セーブ実行時）の時刻を表示に採用
                save_time = db_dt_norm.strftime('%Y/%m/%d %H:%M:%S')

            # --- ベストプラクティス: カタログの自動更新 (副作用) ---
            # S3の方が新しい場合のみカタログを更新。この失敗は表示を妨げてはならない。
            if not config.get("test_mode") and (not db_timestamp or s3_dt.isoformat() > db_timestamp):
                try:
                    factorio_state_table.update_item(
                        Key={'ConfigKey': 'LatestSaveInfo'},
                        UpdateExpression="set #ts = :val, #sz = :sz",
                        ExpressionAttributeNames={'#ts': 'Timestamp', '#sz': 'FileSize'},
                        ExpressionAttributeValues={':val': s3_dt.isoformat(), ':sz': str(save_size)}
                    )
                    print(f"ℹ️ Catalog auto-synced with S3: {save_time}")
                except Exception as db_update_err:
                    # ログ出力のみ行い、処理は続行する
                    print(f"⚠️ Non-critical: Failed to update DynamoDB catalog: {db_update_err}")

        except Exception as e:
            print(f"S3/Catalog Sync Error: {e}")

        display_save_time = f"{status_prefix}{save_time}"

        if state == 'running':
            res_rcon = run_rcon_command(ip, config['rcon_port'], config['rcon_password'], "/players online")
            # 改行を含む全出力を対象にし、名前部分を抽出
            match = re.search(r"Online players \((\d+)\):?([\s\S]*)", res_rcon)
            p_count = match.group(1) if match else "?"
            raw_names = match.group(2) if match else ""
            # 各行から (online) 等のステータス表記を除去し、リスト化
            names_list = [re.sub(r"\s*\(.*?\)", "", n).strip() for n in raw_names.splitlines() if n.strip()]
            p_names = ", ".join(names_list) if names_list else "-"
            return get_msg("status", "running", locale, ip=ip, port=config.get('factorio_game_port', 34197), players=p_count, names=p_names, save_time=display_save_time, size=save_size)
        
        return get_msg("status", "transition", locale, state=state, save_time=display_save_time, size=save_size)

def handle_start(event, ec2, inst, state, ip, locale):
        # TODO ID:006: SERVER_RUN_MODE=STATIC/DYNAMICで起動方式を分岐し、DYNAMIC時は起動テンプレート(run_instances)で作成したInstanceIdをセッション管理へ保存する
        if state == 'stopped':
            # 1. パスワードの取得または生成
            # SSMにあるのは「設定（固定か空か）」、DynamoDBに保存するのが「現在のセッション用」と分離します
            new_pwd = (config.get('game_password') or "").strip("'\" ")

            if not new_pwd or "your_in_game" in new_pwd:
                # 設定が空の場合はランダム生成
                chars = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
                new_pwd = ''.join(secrets.choice(chars) for _ in range(12))
            
            # 2. 現在のパスワードを DynamoDB に「セッションパスワード」として保存
            # これにより SSM を汚さず、起動のたびに new_pwd が生成される条件(空)を維持できる
            try:
                factorio_state_table.update_item(
                    Key={'ConfigKey': 'ActivePassword'},
                    UpdateExpression="set #val = :v",
                    ExpressionAttributeNames={'#val': 'Value'},
                    ExpressionAttributeValues={':v': new_pwd}
                )
            except Exception as e:
                print(f"⚠️ Failed to cache active password to DynamoDB: {e}")

            start_process_time = time.time()
            # 以前の停止処理マーカーが残っている可能性があるため強制削除
            factorio_state_table.delete_item(Key={'ConfigKey': 'StopStartTime'})

            # 各種カウントをリセット
            try:
                factorio_state_table.update_item(Key={'ConfigKey': 'ZeroPlayerCount'}, UpdateExpression="set CountValue = :v", ExpressionAttributeValues={':v': 0})
                factorio_state_table.update_item(Key={'ConfigKey': 'OfflineCount'}, UpdateExpression="set CountValue = :v", ExpressionAttributeValues={':v': 0})
                factorio_state_table.update_item(Key={'ConfigKey': 'AutoRestartCount'}, UpdateExpression="set CountValue = :v", ExpressionAttributeValues={':v': 0})
                print("ℹ️ Counters reset for new session.")
            except Exception as e:
                print(f"⚠️ Failed to reset counters: {e}")

            # 無人停止カウントおよびRCON無応答カウントをリセット
            try:
                factorio_state_table.update_item(Key={'ConfigKey': 'ZeroPlayerCount'}, UpdateExpression="set CountValue = :v", ExpressionAttributeValues={':v': 0})
                factorio_state_table.update_item(Key={'ConfigKey': 'OfflineCount'}, UpdateExpression="set CountValue = :v", ExpressionAttributeValues={':v': 0})
                print("ℹ️ Counters (ZeroPlayerCount, OfflineCount) reset for new session.")
            except Exception as e:
                print(f"⚠️ Failed to reset counters: {e}")

            # イベント検知時の経過時間算出用に開始時刻を記録
            factorio_state_table.update_item(
                Key={'ConfigKey': 'StartStartTime'},
                UpdateExpression="set #ts = :val",
                ExpressionAttributeNames={'#ts': 'Timestamp'},
                ExpressionAttributeValues={':val': str(start_process_time)}
            )

            ec2.start_instances(InstanceIds=[config['instance_id']])
            ssm = get_client('ssm')
            runtime_paths_ready = False
            runtime_check_error_logged = False
            
            # 内部仕様: 起動完了までポーリング待機 (Lambdaタイムアウトに注意)
            # RCONが通る＝ゲームプロセス起動完了とみなす
            time.sleep(10) # 起動直後の待機
            max_attempts = int(config.get('rcon_ready_check_max_attempts', 12))
            interval = int(config.get('rcon_ready_check_interval_seconds', 10))
            
            for _ in range(max_attempts):
                # IPがまだ取れない場合があるため再取得
                inst_refresh = ec2.describe_instances(InstanceIds=[config['instance_id']])['Reservations'][0]['Instances'][0]
                current_ip = inst_refresh.get('PublicIpAddress')
                if current_ip:
                    if not runtime_paths_ready:
                        ready_ok, ready_status, ready_out, ready_err = _ensure_s3files_runtime_paths(ssm, config['instance_id'])
                        if not ready_ok:
                            print(f"⚠️ Runtime path check failed before RCON. status={ready_status} stderr={ready_err}")
                            if ready_out:
                                print(f"⚠️ Runtime path check output: {ready_out}")
                            if not runtime_check_error_logged:
                                notify(
                                    f"⚠️ [LOG] Startup storage check failed. status={ready_status} stderr={ready_err}",
                                    mode='log'
                                )
                                runtime_check_error_logged = True
                            time.sleep(interval)
                            continue
                        runtime_paths_ready = True
                    check = run_rcon_command(current_ip, config['rcon_port'], config['rcon_password'], "/version")
                    if "Error" not in check:
                        # 3. 起動完了後、RCON経由でゲーム内パスワードを適用
                        pwd_res = run_rcon_command(current_ip, config['rcon_port'], config['rcon_password'], f"/config set password {new_pwd}")
                        
                        # パスワード設定の成否をログに記録 (失敗時のみ通知)
                        if "Error" in pwd_res or "Unknown" in pwd_res:
                            notify(f"⚠️ [LOG] Failed to apply game password via RCON: {pwd_res}", mode='log')
                        
                        elapsed = int(time.time() - start_process_time)
                        notify(f"🚀 [LOG] Factorio server is ready (Time: {elapsed}s)", mode='log')
                        
                        # 起動が確認できたので、ステータス表示用の起動マーカーを削除
                        factorio_state_table.delete_item(Key={'ConfigKey': 'StartStartTime'})

                        # 生成した新パスワードとポート情報を取得して完了メッセージを生成
                        port = config.get('factorio_game_port', 34197)
                        return {
                            "embeds": [{
                                "title": get_msg("start", "completed_title", locale),
                                "description": get_msg("start", "completed", locale, ip=current_ip, port=port, pwd=new_pwd),
                                "color": COLOR_GREEN
                            }]
                        }
                time.sleep(interval)

            if not runtime_paths_ready:
                factorio_state_table.delete_item(Key={'ConfigKey': 'StartStartTime'})
                notify(
                    "⚠️ [LOG] Startup aborted due to storage path check failure. Please verify S3 Files mount and config path.",
                    mode='log'
                )
                return (
                    "⚠️ 起動後のストレージ確認に失敗しました。S3 Files のマウントと /mnt/factorio-data/config/server-settings.json を確認してください。"
                    if locale == 'ja'
                    else "⚠️ Startup storage check failed. Please verify S3 Files mount and /mnt/factorio-data/config/server-settings.json."
                )
            return get_msg("start", "success", locale)
        return get_msg("start", "already", locale)

def handle_stop(event, ec2, inst, state, ip, locale):
        # TODO ID:006: SERVER_RUN_MODE=STATICはstop_instances、DYNAMICはterminate_instancesへ分岐し、終了対象InstanceIdをセッション情報から解決する
        if state == 'running':
            start_stop_time = time.time()

            # 以前の起動処理マーカーが残っている可能性があるため強制削除
            factorio_state_table.delete_item(Key={'ConfigKey': 'StartStartTime'})

            # イベント検知時の経過時間算出用に開始時刻を記録
            factorio_state_table.update_item(
                Key={'ConfigKey': 'StopStartTime'},
                UpdateExpression="set #ts = :val",
                ExpressionAttributeNames={'#ts': 'Timestamp'},
                ExpressionAttributeValues={':val': str(start_stop_time)}
            )

            # 1. 先行 /server-save は行わない。
            # 停止時の SIGTERM で Factorio 本体が save.zip を保存するため、
            # ここではプロセス停止完了後にカタログ更新のみ実施する。
            if config.get("test_mode"):
                print("DEBUG: [Test Mode] Skipping save catalog update in stop sequence.")

            # 2. Factorioサーバー停止 (SSM)
            ssm = get_client('ssm')
            stop_command, used_fallback = _resolve_service_unit_name()
            if used_fallback:
                notify("⚠️ [LOG] SERVICE_UNIT_NAME is not set. Using fallback service chain (factorio-dev/prod/default).", mode='log')

            # systemctl stop が長時間ブロックする環境があるため、no-block で発行して
            # is-active のポーリングで停止完了を判定する。
            target_unit = config.get('service_unit_name', '').strip() or 'factorio-dev'
            stop_poll_command = (
                f"sudo systemctl --no-block stop {target_unit}; "
                f"for i in $(seq 1 30); do "
                f"  state=$(systemctl is-active {target_unit} 2>/dev/null || true); "
                f"  echo \"[stop-poll] unit={target_unit} attempt=${{i}} state=${{state}}\"; "
                f"  if [ \"${{state}}\" = \"inactive\" ] || [ \"${{state}}\" = \"failed\" ]; then exit 0; fi; "
                f"  sleep 2; "
                f"done; "
                f"echo \"service stop polling timeout (last_state=$(systemctl is-active {target_unit} 2>/dev/null || true))\"; "
                f"exit 124"
            )
            stop_ok, stop_status, stop_out, stop_err = _run_ssm_shell_and_wait(
                ssm,
                config['instance_id'],
                [stop_poll_command],
                timeout_seconds=75
            )
            if not stop_ok:
                if stop_out:
                    notify(f"⚠️ [LOG] Stop polling output before failure: {stop_out}", mode='log')
                notify(f"⚠️ [LOG] Failed to stop Factorio service before EC2 stop. status={stop_status} stderr={stop_err}", mode='log')
                return (
                    "⚠️ 停止前のFactorioサービス停止に失敗しました。EC2停止を中断しました。管理者にログ確認を依頼してください。必要に応じて再度 /stop を実行してください。"
                    if locale == 'ja'
                    else "⚠️ Failed to stop Factorio service before shutdown. EC2 stop was aborted. Please ask an administrator to check logs and run /stop again if needed."
                )

            # 3. 停止完了後の save 時刻をカタログへ反映
            if not config.get("test_mode"):
                update_latest_save_info(datetime.now(JST).isoformat())

            # 4. logs を S3 へ同期 (SSM)
            if not config.get("test_mode"):
                bucket = (config.get('s3_bucket_name') or '').strip()
                if not bucket:
                    notify("⚠️ [LOG] s3_bucket_name is missing. Skipping EC2 stop to avoid log loss.", mode='log')
                    return (
                        "⚠️ S3バケット設定が見つからないため、ログ退避漏れ防止のためEC2停止を中断しました。管理者にログ確認を依頼してください。必要に応じて再度 /stop を実行してください。"
                        if locale == 'ja'
                        else "⚠️ S3 bucket config is missing. EC2 stop was aborted to avoid log loss. Please ask an administrator to check logs and run /stop again if needed."
                    )

                sync_cmd = (
                    "SESSION_ID=$(date -u +%Y%m%dT%H%M%SZ) && "
                    "LOG_DATE=$(date -u +%F) && "
                    f"DEST=s3://{bucket}/logs/date=${{LOG_DATE}}/ && "
                    "SYNCED=0 && "
                    "for src in /opt/factorio/logs/factorio-*.log; do "
                    "  [ -f \"$src\" ] || continue; "
                    "  base=$(basename \"$src\" .log); "
                    "  aws s3 cp \"$src\" \"${DEST}${base}__session-${SESSION_ID}.log\" >/dev/null && SYNCED=1; "
                    "done && "
                    "UTC_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ) && "
                    "JST_TS=$(TZ=Asia/Tokyo date +%Y-%m-%dT%H:%M:%S%z) && "
                    "JST_DATE=$(TZ=Asia/Tokyo date +%F) && "
                    "printf '{\"session_id\":\"%s\",\"utc_timestamp\":\"%s\",\"jst_timestamp\":\"%s\",\"utc_date\":\"%s\",\"jst_date\":\"%s\",\"instance_id\":\"%s\"}\\n' "
                    f"\"$SESSION_ID\" \"$UTC_TS\" \"$JST_TS\" \"$LOG_DATE\" \"$JST_DATE\" \"{config['instance_id']}\" > /tmp/session-${{SESSION_ID}}.json && "
                    "aws s3 cp /tmp/session-${SESSION_ID}.json \"${DEST}session-${SESSION_ID}.json\" >/dev/null && "
                    "rm -f /tmp/session-${SESSION_ID}.json && "
                    "if [ \"$SYNCED\" -ne 1 ]; then echo \"no factorio-*.log found\"; fi"
                )
                sync_ok, sync_status, sync_out, sync_err = _run_ssm_shell_and_wait(
                    ssm,
                    config['instance_id'],
                    [sync_cmd],
                    timeout_seconds=70
                )
                if not sync_ok:
                    notify(f"⚠️ [LOG] Failed to sync logs before EC2 stop. status={sync_status} stderr={sync_err}", mode='log')
                    return (
                        "⚠️ logs の S3 同期に失敗したため、EC2停止を中断しました。管理者にログ確認を依頼してください。必要に応じて再度 /stop を実行してください。"
                        if locale == 'ja'
                        else "⚠️ Failed to sync logs to S3. EC2 stop was aborted. Please ask an administrator to check logs and run /stop again if needed."
                    )
            else:
                print("DEBUG: [Test Mode] Skipping logs sync in stop sequence.")

            # 5. アンマウント実行 (SSM)
            # サービス停止後、少し待ってから実行
            time.sleep(5)
            _run_ssm_shell_and_wait(
                ssm,
                config['instance_id'],
                ["sudo umount /mnt/factorio-data || true"],
                timeout_seconds=20
            )
            
            # 6. EC2停止
            ec2.stop_instances(InstanceIds=[config['instance_id']])
            
            # 7. セッションパスワードのクリア
            try:
                factorio_state_table.delete_item(Key={'ConfigKey': 'ActivePassword'})
                # 停止時にもカウントをリセット
                factorio_state_table.update_item(Key={'ConfigKey': 'ZeroPlayerCount'}, UpdateExpression="set CountValue = :v", ExpressionAttributeValues={':v': 0})
                factorio_state_table.update_item(Key={'ConfigKey': 'OfflineCount'}, UpdateExpression="set CountValue = :v", ExpressionAttributeValues={':v': 0})
                factorio_state_table.update_item(Key={'ConfigKey': 'AutoRestartCount'}, UpdateExpression="set CountValue = :v", ExpressionAttributeValues={':v': 0})
            except Exception as e:
                print(f"⚠️ Failed to clear active password: {e}")

            # 8. メインチャットへの完了報告 (これによって Interactor のメッセージが PATCH される)
            return get_msg("stop", "process_stopped", locale)
        return get_msg("stop", "already", locale)

def handle_save(event, ec2, inst, state, ip, locale):
        if state != 'running': return get_msg("common", "server_offline", locale)
        # テストモード以外の場合のみ、実際のセーブ処理を実行
        if not config.get("test_mode"):
            run_rcon_command(ip, config['rcon_port'], config['rcon_password'], "/server-save")
            # カタログ (DynamoDB) を更新
            update_latest_save_info(datetime.now(JST).isoformat())
        else:
            print("DEBUG: [Test Mode] Skipping RCON save and catalog update.")
        return get_msg("save", "success", locale)

def handle_pass(event, ec2, inst, state, ip, locale):
    # 1. 停止中、または停止処理中の判定
    if state == 'stopped':
        return get_msg("common", "server_offline", locale)

    try:
        # インスタンスが動いていても、停止マーカーがある場合はオフライン扱いにする
        res_stop = factorio_state_table.get_item(Key={'ConfigKey': 'StopStartTime'})
        if 'Item' in res_stop:
            return get_msg("common", "server_offline", locale)
    except: pass

    # 2. DynamoDBから「現在のセッションで有効なパスワード」を取得する
    try:
        res = factorio_state_table.get_item(Key={'ConfigKey': 'ActivePassword'})
        pwd = res.get('Item', {}).get('Value')
    except Exception as e:
        print(f"⚠️ Failed to fetch active password from DynamoDB: {e}")
        pwd = config.get("game_password")

    pwd = (pwd or "").strip("'\" ")
    if not pwd:
        return get_msg("pass", "not_set", locale)

    # 3. 起動処理中の判定
    try:
        res_start = factorio_state_table.get_item(Key={'ConfigKey': 'StartStartTime'})
        if 'Item' in res_start:
            # パスワードは表示しつつ、準備中であることを伝える
            return get_msg("pass", "starting", locale, pwd=pwd)
    except: pass

    return get_msg("pass", "display", locale, pwd=pwd)

def handle_license(event, ec2, inst, state, ip, locale):
        return {
            "embeds": [{
                "title": "⚖️ MIT License",
                "description": get_msg("license", "content", locale),
                "footer": {"text": "ky0709/factorio-server-manager"},
                "color": COLOR_BLUE
            }]
        }

# アクションとハンドラの紐付け
ACTION_HANDLERS = {
    'status': handle_status,
    'start': handle_start,
    'stop': handle_stop,
    'save': handle_save,
    'pass': handle_pass,
    'license': handle_license
}

def execute_ec2_command(action, event):
    locale = event.get('locale', 'ja')
    ec2 = get_client('ec2')
    instance_id = (config.get('instance_id') or '').strip().strip("'\"")
    if not instance_id:
        print(f"❌ INSTANCE_ID is missing. SSM_PARAMETER_PATH={os.getenv('SSM_PARAMETER_PATH', '/factorio/')}")
        # TODO ID:031: 設定不備エラーのユーザー向け応答を locale に応じて日本語/英語で返す
        return "❌ サーバー設定の取得に失敗しました。管理者に設定内容の確認を依頼してください。"
    # TODO ID:006: DYNAMIC対応時は固定config['instance_id']前提を廃止し、現在アクティブなInstanceIdをDynamoDB等から解決する
    inst = ec2.describe_instances(InstanceIds=[instance_id])['Reservations'][0]['Instances'][0]
    state = inst['State']['Name']
    ip = inst.get('PublicIpAddress')

    handler = ACTION_HANDLERS.get(action)
    if handler:
        return handler(event, ec2, inst, state, ip, locale)
    return get_msg("common", "unknown_cmd", locale)

def lambda_handler(event, context):
    if not config["initialized"]: init_config()

    # 診断用ログ: EventBridge からの呼び出しを含め、すべてのイベントを CloudWatch に記録
    print(f"DEBUG: Received Event: {json.dumps(event)}")

    # invocation ごとに test_mode をリセット（ステート汚染防止）
    action = event.get('action')
    test_mode = event.get('test_mode', False)
    config["test_mode"] = test_mode

    # scripts/test_runner 用: S3 状態の参照・巻き戻し（Executor ロールで実行。Regist に S3 削除権限を広げない）
    if action in ('integration_query_save_state', 'integration_restore_save_state'):
        if not test_mode:
            return {"ok": False, "error": "test_mode is required"}
        if not factorio_state_table:
            return {"ok": False, "error": "DynamoDB not initialized"}
        if action == 'integration_query_save_state':
            return handle_integration_query_save_state(event)
        return handle_integration_restore_save_state(event)

    # EventBridge からの EC2 状態変更通知の処理
    if event.get('source') == 'aws.ec2' and event.get('detail-type') == 'EC2 Instance State-change Notification':
        detail = event.get('detail', {})
        instance_id = detail.get('instance-id') or detail.get('instanceId') or ''

        # グローバルテストフラグのチェックを関数化
        is_suppressed = False
        if factorio_state_table:
            res = factorio_state_table.get_item(Key={'ConfigKey': 'TestSessionActive'})
            if 'Item' in res: is_suppressed = True
        
        # state が辞書形式 {'name': 'stopped'} か、文字列 "stopped" かを判定して取得
        raw_state = detail.get('state')
        if isinstance(raw_state, dict):
            state_name = raw_state.get('name')
        else:
            state_name = raw_state

        # デバッグログ: 受信したイベントの内容を出力
        print(f"DEBUG: Received EC2 event for {instance_id} state={state_name}. Expected ID={config.get('instance_id')}")

        # インスタンスIDの比較 (常に正規化して比較)
        local_id = str(config.get("instance_id") or "").strip().strip("'\"").lower()
        remote_id = str(instance_id or "").strip().strip("'\"").lower()

        if not local_id:
            print(f"⚠️ Event ignored: local_id (instance_id) is empty. SSM path: {os.getenv('SSM_PARAMETER_PATH', '/factorio/')} (Config keys: {list(config.keys())})")
        elif not factorio_state_table:
            print("❌ Event ignored: factorio_state_table is not initialized. Skipping DB operations.")
        elif remote_id != local_id:
            print(f"ℹ️ Event ignored: ID mismatch. Remote={remote_id}, Local={local_id}")
        else:
            try:
                if state_name == 'stopped':
                    # 停止開始時刻を DynamoDB から取得して経過時間を算出
                    res = factorio_state_table.get_item(Key={'ConfigKey': 'StopStartTime'})
                    start_time_str = res.get('Item', {}).get('Timestamp')
                    
                    elapsed_msg = ""
                    if start_time_str:
                        elapsed = int(time.time() - float(start_time_str))
                        elapsed_msg = f" (Total sequence time: {elapsed}s)"
                    msg = f"🔌 [LOG] EC2 Instance ({instance_id}) has stopped. Shutdown sequence completed.{elapsed_msg}"
                    if is_suppressed: print(f"🔕 [SILENT MODE] Suppressed Notification: {msg}")
                    else: notify(msg, mode='log')
                    factorio_state_table.delete_item(Key={'ConfigKey': 'StopStartTime'})
                    return {"status": "ok", "suppressed_msg": msg if is_suppressed else None}
                elif state_name == 'running':
                    # 起動開始時刻を DynamoDB から取得して経過時間を算出
                    res = factorio_state_table.get_item(Key={'ConfigKey': 'StartStartTime'})
                    start_time_str = res.get('Item', {}).get('Timestamp')
                    
                    elapsed_msg = ""
                    if start_time_str:
                        elapsed = int(time.time() - float(start_time_str))
                        elapsed_msg = f" (EC2 boot time: {elapsed}s)"
                    msg = f"🚀 [LOG] EC2 Instance ({instance_id}) is now running.{elapsed_msg}"
                    if is_suppressed: print(f"🔕 [SILENT MODE] Suppressed Notification: {msg}")
                    else: notify(msg, mode='log')
                    factorio_state_table.delete_item(Key={'ConfigKey': 'StartStartTime'})
                    return {"status": "ok", "suppressed_msg": msg if is_suppressed else None}
            except Exception as e:
                print(f"❌ Error processing EC2 state change: {e}")
        
        # イベント対象外であっても、EventBridgeイベントであるならここで終了させる
        return {"status": "event_handled_or_ignored"}

    locale = event.get('locale', 'ja')
    # 更新(PATCH)対象の判定
    notify_mode = 'patch' if action in ['status', 'pass', 'license'] else 'followup'
    
    result = execute_ec2_command(action, event)
    
    # If in test_mode, return the result directly for inspection
    if test_mode:
        resp = {"suppressed_logs": _suppressed_logs, "mode": notify_mode}
        if isinstance(result, dict): # For embeds
            resp.update({"content": None, "embeds": result.get('embeds')})
        else: # For plain text
            resp.update({"content": result, "embeds": None})
        return resp

    # Discord Interaction (Tokenが存在する) 場合のみ、応答を返す
    if event.get('token'):
        if isinstance(result, dict):
            notify(None, mode=notify_mode, event=event, embeds=result.get('embeds'))
        else:
            notify(result, mode=notify_mode, event=event)
        
    return {"status": "ok"}