import json
import os
import re
import time
import secrets
import string
from datetime import datetime
# レイヤーからのインポート
from factorio_common.utils import JST, get_client, fetch_config_from_ssm, run_rcon_command, format_msg, notify_via_lambda
from integration_handlers import (
    handle_integration_detect_startup_failure,
    handle_integration_fix_save_permissions,
    handle_integration_inspect_saves,
    handle_integration_query_save_state,
    handle_integration_restore_save_state,
    handle_integration_set_server_name,
    handle_integration_upgrade_factorio,
)
from executor_infrastructure import (
    acquire_start_lock,
    ensure_s3files_runtime_paths,
    force_release_start_lock,
    query_start_lock_state,
    release_start_lock,
    resolve_server_settings_path,
    resolve_service_unit_name,
    run_ssm_shell_and_wait,
    set_factorio_server_name,
    start_factorio_service,
    upgrade_factorio_headless,
    update_latest_save_info as _update_latest_save_info_impl,
)
from executor_eventbridge import handle_ec2_instance_state_event, handle_managed_capacity_signal

def get_msg(category, key, locale='ja', **kwargs):
    return format_msg({}, category, key, locale, **kwargs)

COLOR_GREEN, COLOR_BLUE = 0x2ECC71, 0x3498DB

config = {"initialized": False}
factorio_state_table = None
_suppressed_logs = []


def update_latest_save_info(timestamp_iso):
    """最新セーブの時刻とサイズをカタログへ保存する。"""
    return _update_latest_save_info_impl(config, factorio_state_table, get_client, timestamp_iso)


def _run_ssm_shell_and_wait(ssm, instance_id, commands, timeout_seconds=180):
    return run_ssm_shell_and_wait(ssm, instance_id, commands, timeout_seconds=timeout_seconds)


def _resolve_service_unit_name():
    return resolve_service_unit_name(config)


def _resolve_server_settings_path():
    return resolve_server_settings_path(config)


def _acquire_start_lock(ttl_seconds=240):
    return acquire_start_lock(factorio_state_table, ttl_seconds)


def _release_start_lock(token):
    return release_start_lock(factorio_state_table, token)


def _force_release_start_lock():
    return force_release_start_lock(factorio_state_table)


def _query_start_lock_state():
    return query_start_lock_state(factorio_state_table)


def _ensure_s3files_runtime_paths(ssm, instance_id, timeout_seconds=45):
    return ensure_s3files_runtime_paths(config, ssm, instance_id, timeout_seconds=timeout_seconds)


def _ensure_s3_files_system_id(ssm):
    """SSM から S3_FILES_SYSTEM_ID を補完する。"""
    if (config.get('s3_files_system_id') or '').strip():
        return
    base_path = (os.getenv('SSM_PARAMETER_PATH') or '/factorio/').strip()
    if not base_path.endswith('/'):
        base_path += '/'
    param_name = f"{base_path}S3_FILES_SYSTEM_ID"
    try:
        res = ssm.get_parameter(Name=param_name)
        value = (res.get('Parameter', {}) or {}).get('Value', '').strip()
        if value:
            config['s3_files_system_id'] = value
    except Exception as e:
        print(f"⚠️ Failed to fetch S3_FILES_SYSTEM_ID from SSM fallback: {e}")


def _get_server_run_mode():
    """
    ID:006 で本実装予定の運用モード。
    現時点では枠組みのみ: STATIC 以外はメンテナンス応答でガードする。
    """
    mode = (config.get('server_run_mode') or 'STATIC').strip().upper()
    return mode if mode else 'STATIC'


def _get_dynamic_capacity_mode():
    mode = (config.get('dynamic_capacity_mode') or 'ONDEMAND').strip().upper()
    return mode if mode else 'ONDEMAND'


def _get_instance_lifecycle_mode():
    mode = (config.get('instance_lifecycle_mode') or 'PERSISTENT').strip().upper()
    return mode if mode else 'PERSISTENT'


def _get_active_instance_id():
    """
    ID:006 用: DYNAMIC で利用中のインスタンスIDを取得する。
    未設定時は None を返す。
    """
    if not factorio_state_table:
        return None
    try:
        res = factorio_state_table.get_item(Key={'ConfigKey': 'ActiveInstanceId'})
        return (res.get('Item', {}) or {}).get('Value')
    except Exception as e:
        print(f"⚠️ Failed to read ActiveInstanceId: {e}")
        return None


def _set_active_instance_id(instance_id, run_mode=None, capacity_mode=None):
    """
    ID:006 用: DYNAMIC 実行対象インスタンス情報を保存する。
    """
    if not factorio_state_table or not instance_id:
        return
    now = datetime.now(JST).isoformat()
    try:
        factorio_state_table.put_item(
            Item={
                'ConfigKey': 'ActiveInstanceId',
                'Value': str(instance_id),
                'RunMode': run_mode or _get_server_run_mode(),
                'CapacityMode': capacity_mode or _get_dynamic_capacity_mode(),
                'Timestamp': now,
            }
        )
    except Exception as e:
        print(f"⚠️ Failed to persist ActiveInstanceId: {e}")


def _clear_active_instance_id():
    if not factorio_state_table:
        return
    try:
        factorio_state_table.delete_item(Key={'ConfigKey': 'ActiveInstanceId'})
    except Exception as e:
        print(f"⚠️ Failed to clear ActiveInstanceId: {e}")


def _split_csv(raw_value):
    return [v.strip() for v in str(raw_value or '').split(',') if v.strip()]


def _build_dynamic_run_instances_input():
    """
    ID:006 用: DYNAMIC 起動用の run_instances 入力を構築する。
    LaunchTemplate が指定されている場合はそれを優先する。
    """
    params = {"MinCount": 1, "MaxCount": 1}
    launch_template_id = (config.get('launch_template_id') or '').strip()
    launch_template_version = (config.get('launch_template_version') or '').strip() or '$Latest'
    if launch_template_id:
        params["LaunchTemplate"] = {
            "LaunchTemplateId": launch_template_id,
            "Version": launch_template_version,
        }
    else:
        image_id = (config.get('base_ami_id') or '').strip()
        if not image_id:
            return None, "BASE_AMI_ID or LAUNCH_TEMPLATE_ID is required for SERVER_RUN_MODE=DYNAMIC."
        params["ImageId"] = image_id
        params["InstanceType"] = (config.get('instance_type') or 't3.medium').strip()

        subnet_id = (config.get('subnet_id') or '').strip()
        if subnet_id:
            params["SubnetId"] = subnet_id
        security_group_ids = _split_csv(config.get('security_group_ids'))
        if security_group_ids:
            params["SecurityGroupIds"] = security_group_ids

        key_name = (config.get('key_name') or '').strip()
        if key_name:
            params["KeyName"] = key_name
        profile_name = (config.get('ec2_instance_profile_name') or '').strip()
        if profile_name:
            params["IamInstanceProfile"] = {"Name": profile_name}

    if _get_dynamic_capacity_mode() == 'SPOT':
        params["InstanceMarketOptions"] = {
            "MarketType": "spot",
            "SpotOptions": {
                "SpotInstanceType": "one-time",
                "InstanceInterruptionBehavior": "terminate"
            }
        }
    return params, None


def _create_dynamic_instance(locale, ec2):
    params, build_error = _build_dynamic_run_instances_input()
    if build_error:
        return None, (
            f"⚠️ DYNAMIC起動設定が不足しています: {build_error}"
            if locale == 'ja'
            else f"⚠️ Missing DYNAMIC launch settings: {build_error}"
        )
    try:
        resp = ec2.run_instances(**params)
        instances = resp.get('Instances') or []
        if not instances:
            return None, (
                "⚠️ DYNAMIC起動に失敗しました（instance作成結果が空です）。"
                if locale == 'ja'
                else "⚠️ Failed to create DYNAMIC instance (empty result)."
            )
        instance_id = instances[0].get('InstanceId')
        if not instance_id:
            return None, (
                "⚠️ DYNAMIC起動に失敗しました（InstanceIdが取得できません）。"
                if locale == 'ja'
                else "⚠️ Failed to create DYNAMIC instance (missing InstanceId)."
            )
        _set_active_instance_id(
            instance_id,
            run_mode='DYNAMIC',
            capacity_mode=_get_dynamic_capacity_mode()
        )
        print(f"ℹ️ DYNAMIC instance created: {instance_id}")
        _sync_ec2_state_rule_target_instance(instance_id)
        return instance_id, None
    except Exception as e:
        print(f"❌ DYNAMIC run_instances failed: {e}")
        return None, (
            f"⚠️ DYNAMIC起動に失敗しました。設定とIAM権限を確認してください。({e})"
            if locale == 'ja'
            else f"⚠️ Failed to start in DYNAMIC mode. Check settings and IAM permissions. ({e})"
        )


def _sync_ec2_state_rule_target_instance(instance_id):
    """
    ID:006 用: DYNAMIC 起動後に EventBridge EC2 state-change ルールの
    監視対象 instance-id を最新の ActiveInstanceId へ追従させる。
    """
    if not instance_id:
        return False

    region = (config.get('region') or os.getenv('AWS_REGION') or os.getenv('AWS_DEFAULT_REGION') or 'ap-northeast-1').strip()
    env_label = 'dev' if 'dev' in (os.getenv('SSM_PARAMETER_PATH') or '').lower() else 'prod'
    suffix = '-dev' if env_label == 'dev' else ''
    rule_name = (config.get('ec2_state_rule_name') or f'Factorio-EC2StateChange{suffix}').strip()
    executor_lambda_name = (config.get('executor_lambda_name') or os.getenv('AWS_LAMBDA_FUNCTION_NAME') or '').strip()
    if not executor_lambda_name:
        print("⚠️ Skip EventBridge rule sync: executor_lambda_name is empty.")
        return False

    events = get_client('events')
    awslambda = get_client('lambda')
    sts = get_client('sts')
    account_id = sts.get_caller_identity().get('Account')
    role_name = (config.get('eventbridge_role_name') or f'FactorioEventBridgeRole{suffix}').strip()
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    target_arn = f"arn:aws:lambda:{region}:{account_id}:function:{executor_lambda_name}"
    new_pattern = {
        "source": ["aws.ec2"],
        "detail-type": ["EC2 Instance State-change Notification"],
        "detail": {
            "instance-id": [instance_id],
            "state": sorted(["running", "stopped", "terminated"])
        }
    }

    try:
        current_state = 'ENABLED'
        try:
            current_rule = events.describe_rule(Name=rule_name)
            current_state = current_rule.get('State', 'ENABLED')
        except Exception:
            pass

        events.put_rule(
            Name=rule_name,
            EventPattern=json.dumps(new_pattern),
            State=current_state
        )
        events.put_targets(
            Rule=rule_name,
            Targets=[{'Id': '1', 'Arn': target_arn, 'RoleArn': role_arn}]
        )
        print(f"ℹ️ EventBridge rule synced for dynamic instance: rule={rule_name} instance={instance_id}")

        # ルール新規作成時の invoke 権限不足を避けるため、必要なら追加（既存ならスキップ）
        statement_id = f"AllowEventBridgeNotify-{env_label}"
        rule_arn = f"arn:aws:events:{region}:{account_id}:rule/{rule_name}"
        try:
            policy_resp = awslambda.get_policy(FunctionName=executor_lambda_name)
            policy_dict = json.loads(policy_resp['Policy'])
            already = False
            for stmt in policy_dict.get('Statement', []):
                sid = stmt.get('Sid')
                src = (stmt.get('Condition', {}).get('ArnLike', {}) or {}).get('AWS:SourceArn')
                if sid in [statement_id, f"AlllowEventBridgeNotify-{env_label}"] and src == rule_arn:
                    already = True
                    break
            if not already:
                awslambda.add_permission(
                    FunctionName=executor_lambda_name,
                    StatementId=statement_id,
                    Action='lambda:InvokeFunction',
                    Principal='events.amazonaws.com',
                    SourceArn=rule_arn
                )
        except Exception as e:
            # 既存衝突・権限不足は起動自体の致命傷にしない
            print(f"⚠️ EventBridge invoke permission check/add skipped: {e}")
        return True
    except Exception as e:
        print(f"⚠️ Failed to sync EventBridge EC2 state rule for dynamic instance: {e}")
        return False


# ID:091: EC2 操作が不要なコマンドは instance 未解決でもハンドラへ進める
_OFFLINE_OK_ACTIONS = frozenset({'status', 'pass', 'license'})


def _resolve_instance_id_for_action(action, locale, ec2):
    """
    ID:006 用: 実行対象インスタンスIDを運用モードに応じて解決する。
    戻り値: (instance_id | None, error_message | None)
    ID:091: status/pass/license は instance_id=None を許す（error なし）。
    """
    run_mode = _get_server_run_mode()
    offline_ok = action in _OFFLINE_OK_ACTIONS

    if run_mode == 'STATIC':
        instance_id = (config.get('instance_id') or '').strip().strip("'\"")
        if not instance_id:
            print(f"❌ INSTANCE_ID is missing. SSM_PARAMETER_PATH={os.getenv('SSM_PARAMETER_PATH', '/factorio/')}")
            if offline_ok:
                return None, None
            return None, "❌ サーバー設定の取得に失敗しました。管理者に設定内容の確認を依頼してください。"
        return instance_id, None

    if action == 'start':
        active_instance_id = (_get_active_instance_id() or '').strip()
        if active_instance_id:
            return active_instance_id, None
        return _create_dynamic_instance(locale, ec2)

    active_instance_id = (_get_active_instance_id() or '').strip()
    if not active_instance_id:
        if offline_ok:
            return None, None
        return None, get_msg("common", "server_offline", locale)
    return active_instance_id, None


def _integration_query_target_instance(event, ec2):
    """test_runner 用: 実行対象インスタンスIDと状態を返す。"""
    data = event.get('data', {}) if isinstance(event, dict) else {}
    prefer_active = bool(data.get('prefer_active', False))
    run_mode = _get_server_run_mode()
    env_instance_id = (config.get('instance_id') or '').strip().strip("'\"")
    active_instance_id = (_get_active_instance_id() or '').strip()

    target_instance_id = None
    source = 'none'
    if run_mode == 'DYNAMIC' or prefer_active:
        if active_instance_id:
            target_instance_id = active_instance_id
            source = 'active'
        elif env_instance_id and run_mode != 'DYNAMIC':
            target_instance_id = env_instance_id
            source = 'env'
    elif env_instance_id:
        target_instance_id = env_instance_id
        source = 'env'

    if not target_instance_id:
        return {"ok": True, "instance_id": None, "state": None, "source": source, "run_mode": run_mode}

    try:
        inst = ec2.describe_instances(InstanceIds=[target_instance_id])['Reservations'][0]['Instances'][0]
        return {
            "ok": True,
            "instance_id": target_instance_id,
            "state": inst.get('State', {}).get('Name'),
            "source": source,
            "run_mode": run_mode
        }
    except Exception as e:
        return {
            "ok": False,
            "instance_id": target_instance_id,
            "state": None,
            "source": source,
            "run_mode": run_mode,
            "error": str(e)
        }


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
        target_instance_id = inst.get('InstanceId') or (config.get('instance_id') or '').strip().strip("'\"")
        if config.get("test_mode"):
            _force_release_start_lock()
        start_lock_acquired, start_lock_token = _acquire_start_lock()
        if not start_lock_acquired:
            print("ℹ️ /start skipped: StartActionLock is already held.")
            config["start_lock_denied"] = True
            return get_msg("start", "already", locale)

        try:
            run_mode = _get_server_run_mode()
            can_start_flow = state == 'stopped' or (run_mode == 'DYNAMIC' and state in ('pending', 'running'))
            if can_start_flow:
                phase_started_at = time.time()
                timing = {
                    "start_invoke_to_ready_seconds": 0.0,
                    "ec2_boot_wait_seconds": 0.0,
                    "storage_check_seconds": 0.0,
                    "rcon_ready_wait_seconds": 0.0,
                }

                def _emit_start_timing(stage):
                    timing["start_invoke_to_ready_seconds"] = round(time.time() - phase_started_at, 2)
                    print(
                        "⏱️ START_TIMING "
                        f"stage={stage} "
                        f"total={timing['start_invoke_to_ready_seconds']}s "
                        f"ec2_boot_wait={round(timing['ec2_boot_wait_seconds'], 2)}s "
                        f"storage_check={round(timing['storage_check_seconds'], 2)}s "
                        f"rcon_wait={round(timing['rcon_ready_wait_seconds'], 2)}s"
                    )

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
                try:
                    factorio_state_table.delete_item(Key={'ConfigKey': 'StopTargetInstanceId'})
                except Exception:
                    pass

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

                if state == 'stopped':
                    boot_wait_started_at = time.time()
                    ec2.start_instances(InstanceIds=[target_instance_id])
                    timing["ec2_boot_wait_seconds"] += time.time() - boot_wait_started_at
                ssm = get_client('ssm')
                _ensure_s3_files_system_id(ssm)
                runtime_paths_ready = False
                runtime_check_error_logged = False
                service_start_attempted = False
                
                # 内部仕様: 起動完了までポーリング待機 (Lambdaタイムアウトに注意)
                # RCONが通る＝ゲームプロセス起動完了とみなす
                # ID:087: max_attempts 固定打ち切りではなく予算＋安全上限制、終了時は timeout メッセージを返す
                time.sleep(10) # 起動直後の待機
                test_mode = bool(config.get("test_mode"))
                max_attempts = int(
                    config.get('rcon_ready_check_max_attempts', 12 if test_mode else 48)
                )
                interval = int(
                    config.get('rcon_ready_check_interval_seconds', 5 if test_mode else 5)
                )
                # ID:080 test_mode では同期呼び出しが長時間ブロックしないよう予算を短縮する
                start_wait_budget_seconds = int(
                    config.get('start_ready_wait_budget_seconds', 85 if test_mode else 240)
                )
                # Lambda 残り時間と整合（通知・ロック解放用に 20 秒を確保）
                remaining_fn = config.get('_context_remaining_fn')
                if callable(remaining_fn):
                    try:
                        rem_s = max(30, int(remaining_fn() // 1000) - 20)
                        if rem_s < start_wait_budget_seconds:
                            print(
                                f"ℹ️ Clamping start wait budget to Lambda remaining headroom: "
                                f"{start_wait_budget_seconds}s -> {rem_s}s"
                            )
                            start_wait_budget_seconds = rem_s
                    except Exception as e:
                        print(f"⚠️ Failed to read Lambda remaining time for start budget: {e}")
                runtime_check_timeout_seconds = int(
                    config.get('startup_storage_check_timeout_seconds', 20 if test_mode else 45)
                )
                runtime_check_timeout_seconds = max(30, runtime_check_timeout_seconds)
                start_wait_deadline = time.time() + max(30, start_wait_budget_seconds)
                
                attempt = 0
                while True:
                    attempt += 1
                    remaining_budget = start_wait_deadline - time.time()
                    if remaining_budget <= 0:
                        print("⚠️ Start wait budget exceeded before runtime path/RCON became ready.")
                        break
                    if attempt > max_attempts:
                        print(f"⚠️ Start wait max attempts exceeded ({max_attempts}).")
                        break
                    if callable(remaining_fn):
                        try:
                            if remaining_fn() < 20000:
                                print("⚠️ Lambda remaining time low; ending start wait for notify/cleanup.")
                                break
                        except Exception:
                            pass
                    # IPがまだ取れない場合があるため再取得
                    inst_refresh = ec2.describe_instances(InstanceIds=[target_instance_id])['Reservations'][0]['Instances'][0]
                    current_ip = inst_refresh.get('PublicIpAddress')
                    if current_ip:
                        if not runtime_paths_ready:
                            # 予算内でのみ SSM 確認を実施（test_mode での90秒超過を防ぐ）
                            runtime_timeout = min(
                                runtime_check_timeout_seconds,
                                max(30, int(remaining_budget) - 1)
                            )
                            storage_check_started_at = time.time()
                            ready_ok, ready_status, ready_out, ready_err = _ensure_s3files_runtime_paths(
                                ssm,
                                target_instance_id,
                                timeout_seconds=runtime_timeout
                            )
                            timing["storage_check_seconds"] += time.time() - storage_check_started_at
                            if not ready_ok:
                                print(f"⚠️ Runtime path check failed before RCON. status={ready_status} stderr={ready_err}")
                                if ready_out:
                                    print(f"⚠️ Runtime path check output: {ready_out}")
                                transient_boot_error = (
                                    ready_status == "SendCommandFailed"
                                    and (
                                        "InvalidInstanceId" in (ready_err or "")
                                        or "not in a valid state" in (ready_err or "").lower()
                                    )
                                )
                                if transient_boot_error:
                                    print("ℹ️ Runtime path check is waiting for EC2/SSM readiness; retrying.")
                                elif not runtime_check_error_logged:
                                    notify(
                                        f"⚠️ [LOG] Startup storage check failed. status={ready_status} stderr={ready_err}",
                                        mode='log'
                                    )
                                    runtime_check_error_logged = True
                                post_check_remaining = start_wait_deadline - time.time()
                                if post_check_remaining <= 0:
                                    print("⚠️ Start wait budget exceeded after runtime path check failure.")
                                    break
                                time.sleep(min(interval, max(1, int(post_check_remaining))))
                                continue
                            runtime_paths_ready = True

                        # ID:088: FACTORIO_VERSION が具体版のとき不一致なら headless を更新
                        # （stable は起動毎のダウンロードを避けるため force なしではスキップ）
                        target_ver = (config.get('factorio_version') or '').strip()
                        if target_ver and target_ver.lower() != 'stable':
                            up_timeout = min(180, max(30, int(start_wait_deadline - time.time()) - 30))
                            if up_timeout >= 60:
                                up_ok, up_status, up_out, up_err, up_skip = upgrade_factorio_headless(
                                    config,
                                    ssm,
                                    target_instance_id,
                                    run_fn=_run_ssm_shell_and_wait,
                                    timeout_seconds=up_timeout,
                                    force=False,
                                    restart=False,
                                )
                                if up_out:
                                    print(f"ℹ️ Factorio version check: {up_out}")
                                if not up_ok and not up_skip:
                                    print(f"⚠️ Factorio headless upgrade failed. status={up_status} stderr={up_err}")
                                    notify(
                                        f"⚠️ [LOG] Factorio headless upgrade failed. status={up_status} stderr={up_err}",
                                        mode='log'
                                    )
                            else:
                                print("⚠️ Skipping version check: insufficient start-wait budget.")

                        # ID:087: マウント/設定確認後にユニットを明示起動（AMI の auto-start 漏れを吸収）
                        if not service_start_attempted:
                            rem_for_svc = int(start_wait_deadline - time.time()) - 1
                            if rem_for_svc < 15:
                                print("⚠️ Skipping systemctl start: insufficient start-wait budget remaining.")
                                service_start_attempted = True
                            else:
                                service_start_attempted = True
                                svc_timeout = min(45, max(15, rem_for_svc))
                                svc_ok, svc_status, svc_out, svc_err, svc_fallback = start_factorio_service(
                                    config,
                                    ssm,
                                    target_instance_id,
                                    run_fn=_run_ssm_shell_and_wait,
                                    timeout_seconds=svc_timeout
                                )
                                if svc_fallback:
                                    notify(
                                        "⚠️ [LOG] SERVICE_UNIT_NAME is not set. "
                                        "Using fallback service chain (factorio-prod/dev/default) for start.",
                                        mode='log'
                                    )
                                if svc_out:
                                    print(f"ℹ️ Factorio service start output: {svc_out}")
                                if not svc_ok:
                                    print(
                                        f"⚠️ Factorio service start failed. status={svc_status} stderr={svc_err}"
                                    )
                                    notify(
                                        f"⚠️ [LOG] Failed to start Factorio service after storage check. "
                                        f"status={svc_status} stderr={svc_err}",
                                        mode='log'
                                    )
                                else:
                                    print("ℹ️ Factorio service start issued successfully.")

                        rcon_wait_started_at = time.time()
                        check = run_rcon_command(current_ip, config['rcon_port'], config['rcon_password'], "/version")
                        timing["rcon_ready_wait_seconds"] += time.time() - rcon_wait_started_at
                        if "Error" not in check:
                            # 3. 起動完了後、RCON経由でゲーム内パスワードを適用
                            pwd_res = run_rcon_command(current_ip, config['rcon_port'], config['rcon_password'], f"/config set password {new_pwd}")
                            
                            # パスワード設定の成否をログに記録 (失敗時のみ通知)
                            if "Error" in pwd_res or "Unknown" in pwd_res:
                                notify(f"⚠️ [LOG] Failed to apply game password via RCON: {pwd_res}", mode='log')
                            
                            elapsed = int(time.time() - start_process_time)
                            notify(f"🚀 [LOG] Factorio server is ready (Time: {elapsed}s)", mode='log')
                            _emit_start_timing("ready")
                            _set_active_instance_id(
                                target_instance_id,
                                run_mode=_get_server_run_mode(),
                                capacity_mode=_get_dynamic_capacity_mode()
                            )
                            
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
                    remaining_after_loop = start_wait_deadline - time.time()
                    if remaining_after_loop <= 0:
                        print("⚠️ Start wait budget exceeded while waiting next retry interval.")
                        break
                    time.sleep(min(interval, max(1, int(remaining_after_loop))))

                if not runtime_paths_ready:
                    _emit_start_timing("storage_check_failed")
                    factorio_state_table.delete_item(Key={'ConfigKey': 'StartStartTime'})
                    server_settings_path = _resolve_server_settings_path()
                    notify(
                        (
                            "⚠️ [LOG] Startup aborted due to storage path check failure or readiness timeout. "
                            "Please verify S3 Files mount and config path."
                        ),
                        mode='log'
                    )
                    return (
                        f"⚠️ 起動後のストレージ確認に失敗、または準備待機がタイムアウトしました。管理者にログチャットの確認と、S3 Files のマウントおよび {server_settings_path} の設定確認を依頼してください。"
                        if locale == 'ja'
                        else f"⚠️ Startup storage check failed or readiness wait timed out. Please ask an administrator to review the log chat and verify S3 Files mount plus {server_settings_path}."
                    )
                # ID:087: RCON 未達は success と見せず timeout として通知（StartStartTime もクリア）
                _emit_start_timing("rcon_ready_timeout")
                factorio_state_table.delete_item(Key={'ConfigKey': 'StartStartTime'})
                notify(
                    "⚠️ [LOG] Startup wait timed out before RCON became ready. "
                    "EC2 may be running while Factorio service is not yet responsive.",
                    mode='log'
                )
                return get_msg("start", "timeout", locale)
            return get_msg("start", "already", locale)
        finally:
            _release_start_lock(start_lock_token)

def handle_stop(event, ec2, inst, state, ip, locale):
        # TODO ID:006: SERVER_RUN_MODE=STATICはstop_instances、DYNAMICはterminate_instancesへ分岐し、終了対象InstanceIdをセッション情報から解決する
        target_instance_id = inst.get('InstanceId') or (config.get('instance_id') or '').strip().strip("'\"")
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
                target_instance_id,
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
                    f"\"$SESSION_ID\" \"$UTC_TS\" \"$JST_TS\" \"$LOG_DATE\" \"$JST_DATE\" \"{target_instance_id}\" > /tmp/session-${{SESSION_ID}}.json && "
                    "aws s3 cp /tmp/session-${SESSION_ID}.json \"${DEST}session-${SESSION_ID}.json\" >/dev/null && "
                    "rm -f /tmp/session-${SESSION_ID}.json && "
                    "if [ \"$SYNCED\" -ne 1 ]; then echo \"no factorio-*.log found\"; fi"
                )
                sync_ok, sync_status, sync_out, sync_err = _run_ssm_shell_and_wait(
                    ssm,
                    target_instance_id,
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
                target_instance_id,
                ["sudo umount /mnt/factorio-data || true"],
                timeout_seconds=20
            )
            
            # 6. EC2停止/終了
            run_mode = _get_server_run_mode()
            lifecycle_mode = _get_instance_lifecycle_mode()
            # ID:090: EventBridge 照合用に停止対象 ID を残す（terminate 後も消費可能）
            try:
                factorio_state_table.put_item(
                    Item={
                        'ConfigKey': 'StopTargetInstanceId',
                        'Value': str(target_instance_id),
                        'Timestamp': str(start_stop_time),
                    }
                )
            except Exception as e:
                print(f"⚠️ Failed to persist StopTargetInstanceId: {e}")

            if run_mode == 'DYNAMIC' and lifecycle_mode == 'EPHEMERAL':
                ec2.terminate_instances(InstanceIds=[target_instance_id])
                # ActiveInstanceId は terminated イベント側で掃除（ID 照合のため残す）
            else:
                ec2.stop_instances(InstanceIds=[target_instance_id])
            
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
    # ID:091: EC2 停止中でも実行可。セッション PW が無ければ not_set（stop で ActivePassword は消える）。
    try:
        res = factorio_state_table.get_item(Key={'ConfigKey': 'ActivePassword'})
        pwd = res.get('Item', {}).get('Value')
    except Exception as e:
        print(f"⚠️ Failed to fetch active password from DynamoDB: {e}")
        pwd = config.get("game_password")

    pwd = (pwd or "").strip("'\" ")
    if not pwd:
        return get_msg("pass", "not_set", locale)

    # 起動処理中はパスワードを出しつつ準備中を伝える
    try:
        res_start = factorio_state_table.get_item(Key={'ConfigKey': 'StartStartTime'})
        if 'Item' in res_start:
            return get_msg("pass", "starting", locale, pwd=pwd)
    except Exception:
        pass

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
    handler = ACTION_HANDLERS.get(action)
    if not handler:
        return get_msg("common", "unknown_cmd", locale)

    # ID:091: license は EC2 に依存しない
    if action == 'license':
        return handler(event, None, {}, 'stopped', None, locale)

    ec2 = get_client('ec2')
    instance_id, resolve_error = _resolve_instance_id_for_action(action, locale, ec2)
    if resolve_error:
        return resolve_error

    # instance 未解決でも offline 可コマンドは停止相当でハンドラ実行
    if not instance_id:
        return handler(event, ec2, {}, 'stopped', None, locale)

    try:
        inst = ec2.describe_instances(InstanceIds=[instance_id])['Reservations'][0]['Instances'][0]
        state = inst['State']['Name']
        ip = inst.get('PublicIpAddress')
    except Exception as e:
        print(f"⚠️ describe_instances failed for {instance_id}: {e}")
        if action in _OFFLINE_OK_ACTIONS:
            return handler(event, ec2, {}, 'stopped', None, locale)
        return get_msg("common", "server_offline", locale)

    return handler(event, ec2, inst, state, ip, locale)

def lambda_handler(event, context):
    if not config["initialized"]: init_config()

    # 診断用ログ: EventBridge からの呼び出しを含め、すべてのイベントを CloudWatch に記録
    print(f"DEBUG: Received Event: {json.dumps(event)}")

    # invocation ごとに test_mode をリセット（ステート汚染防止）
    action = event.get('action')
    test_mode = event.get('test_mode', False)
    config["test_mode"] = test_mode
    config["start_lock_denied"] = False
    # ID:087: 起動待機予算を Lambda 残り時間に合わせてクランプするために保持
    if context is not None and hasattr(context, "get_remaining_time_in_millis"):
        config["_context_remaining_fn"] = context.get_remaining_time_in_millis
    else:
        config["_context_remaining_fn"] = None

    # scripts/test_runner 用: S3 状態の参照・巻き戻し（Executor ロールで実行。Regist に S3 削除権限を広げない）
    if action in (
        'integration_query_save_state',
        'integration_restore_save_state',
        'integration_clear_start_lock',
        'integration_query_start_lock',
        'integration_detect_startup_failure',
        'integration_query_target_instance',
        'integration_upgrade_factorio',
        'integration_set_server_name',
        'integration_inspect_saves',
        'integration_fix_save_permissions',
    ):
        if not test_mode:
            return {"ok": False, "error": "test_mode is required"}
        if not factorio_state_table:
            return {"ok": False, "error": "DynamoDB not initialized"}
        if action == 'integration_query_save_state':
            return handle_integration_query_save_state(config, factorio_state_table, get_client)
        if action == 'integration_clear_start_lock':
            return _force_release_start_lock()
        if action == 'integration_query_start_lock':
            return _query_start_lock_state()
        if action == 'integration_detect_startup_failure':
            return handle_integration_detect_startup_failure(event, config, get_client)
        if action == 'integration_query_target_instance':
            return _integration_query_target_instance(event, get_client('ec2'))
        if action == 'integration_upgrade_factorio':
            return handle_integration_upgrade_factorio(
                event,
                config,
                get_client,
                factorio_state_table,
                upgrade_factorio_headless,
            )
        if action == 'integration_set_server_name':
            return handle_integration_set_server_name(
                event,
                config,
                get_client,
                factorio_state_table,
                set_factorio_server_name,
            )
        if action == 'integration_inspect_saves':
            return handle_integration_inspect_saves(
                event,
                config,
                get_client,
                factorio_state_table,
                _run_ssm_shell_and_wait,
            )
        if action == 'integration_fix_save_permissions':
            return handle_integration_fix_save_permissions(
                event,
                config,
                get_client,
                factorio_state_table,
                _run_ssm_shell_and_wait,
            )
        return handle_integration_restore_save_state(event, config, factorio_state_table, get_client)

    # EventBridge: Spot 中断警告 / Rebalance（ID:068 の第一段階）
    if event.get('source') == 'aws.ec2' and event.get('detail-type') in (
        'EC2 Spot Instance Interruption Warning',
        'EC2 Instance Rebalance Recommendation',
    ):
        return handle_managed_capacity_signal(
            event,
            config,
            factorio_state_table,
            notify,
            get_client,
            run_rcon_command
        )

    # EventBridge からの EC2 状態変更通知の処理
    if event.get('source') == 'aws.ec2' and event.get('detail-type') == 'EC2 Instance State-change Notification':
        return handle_ec2_instance_state_event(event, config, factorio_state_table, notify)

    locale = event.get('locale', 'ja')
    # 更新(PATCH)対象の判定
    notify_mode = 'patch' if action in ['status', 'pass', 'license'] else 'followup'
    
    result = execute_ec2_command(action, event)

    debug_instance_id = None
    debug_state = None
    try:
        ec2_dbg = get_client('ec2')
        dbg_instance_id = (_get_active_instance_id() or config.get('instance_id') or '').strip().strip("'\"")
        if dbg_instance_id:
            dbg_inst = ec2_dbg.describe_instances(InstanceIds=[dbg_instance_id])['Reservations'][0]['Instances'][0]
            debug_instance_id = dbg_instance_id
            debug_state = dbg_inst.get('State', {}).get('Name')
    except Exception as e:
        print(f"⚠️ test_mode debug state fetch failed: {e}")
    
    # If in test_mode, return the result directly for inspection
    if test_mode:
        resp = {"suppressed_logs": _suppressed_logs, "mode": notify_mode}
        if isinstance(result, dict): # For embeds
            resp.update({"content": None, "embeds": result.get('embeds')})
        else: # For plain text
            resp.update({"content": result, "embeds": None})
        resp.update({
            "debug_instance_id": debug_instance_id,
            "debug_state": debug_state,
            "debug_start_lock_denied": bool(config.get("start_lock_denied"))
        })
        return resp

    # Discord Interaction (Tokenが存在する) 場合のみ、応答を返す
    if event.get('token'):
        if isinstance(result, dict):
            notify(None, mode=notify_mode, event=event, embeds=result.get('embeds'))
        else:
            notify(result, mode=notify_mode, event=event)
        
    return {"status": "ok"}