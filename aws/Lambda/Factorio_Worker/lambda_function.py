import json
import re
import time
from datetime import datetime

# レイヤーからのインポート
from factorio_common.utils import JST, get_client, fetch_config_from_ssm, run_rcon_command, format_msg, notify_via_lambda, GLOBAL_TEXT_RESOURCES

def get_msg(category, key, locale='ja', **kwargs):
    return format_msg(GLOBAL_TEXT_RESOURCES, category, key, locale, **kwargs)

config = {"initialized": False}
_suppressed_logs = []

def init_config():
    global _suppressed_logs
    _suppressed_logs = []
    ssm_config = fetch_config_from_ssm()
    config.update(ssm_config)
    global factorio_state_table
    factorio_state_table = get_client('dynamodb', True).Table(config.get('dynamodb_table_name'))
    config["initialized"] = True

def notify(content, mode='followup', event=None, embeds=None, components=None):
    # テストモード時は Discord への通知処理をスキップ
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

    notify_via_lambda(
        config['notifier_lambda_name'],
        content,
        mode=mode,
        event=event,
        embeds=embeds,
        components=components
    )

def handle_restore(event):
    locale = event.get('locale', 'ja')
    data = event.get('data', {})
    data_options = data.get('options', [])
    
    # スラッシュコマンドから検索条件を取得
    date_query = datetime.now(JST).strftime('%Y%m%d')
    
    if data_options:
        # スラッシュコマンド時
        sub_cmd = data_options[0].get('name')
        sub_options = data_options[0].get('options', [])
        # dateオプションがあれば上書き、なければデフォルト（今日）を使用
        date_query = next((str(o['value']) for o in sub_options if o['name'] == 'date'), date_query)
    else:
        return get_msg("restore", "not_found", locale, date="Unknown"), None

    s3 = get_client('s3')
    bucket = config['s3_bucket_name']
    key = config['save_file_key']

    if sub_cmd == 'list':
        versions = s3.list_object_versions(Bucket=bucket, Prefix=key).get('Versions', [])

        # 同期状態の判定ロジック (Executorのstatusと同様)
        sync_prefix = ""
        pending_msg = ""
        try:
            res_cat = factorio_state_table.get_item(Key={'ConfigKey': 'LatestSaveInfo'})
            db_timestamp = res_cat.get('Item', {}).get('Timestamp')
            if db_timestamp and versions:
                # 秒単位で比較するために正規化
                db_dt_norm = datetime.fromisoformat(db_timestamp).replace(microsecond=0)
                s3_dt_norm = versions[0]['LastModified'].astimezone(JST).replace(microsecond=0)
                if db_dt_norm > s3_dt_norm:
                    sync_prefix = get_msg("restore", "syncing", locale)
                    # カタログ上の時刻を取得して注意喚起メッセージを生成
                    cat_time_str = db_dt_norm.strftime('%Y/%m/%d %H:%M:%S')
                    pending_msg = get_msg("restore", "pending_warning", locale, time=cat_time_str)
        except Exception as e:
            print(f"Sync check error in worker: {e}")

        count_query = next((o['value'] for o in sub_options if o['name'] == 'count'), None)
        v_list = []

        if count_query:
            # count 指定時はページングなしで制限のみ適用
            requested_count = min(max(int(count_query), 1), 20)
            versions_to_show = versions[:requested_count]
            # 表示対象として抽出された実際の件数
            total_found = len(versions_to_show)
            display_date = f"最新{total_found}件" if locale == 'ja' else f"Latest {total_found}" 
            count_info = "" # count引数時はタイトルに件数を含むため空にする
        else:
            # 日付指定時
            display_date = date_query
            filtered_versions = [v for v in versions if v['LastModified'].astimezone(JST).strftime('%Y%m%d') == date_query]
            total_found = len(filtered_versions) # その検索条件に合致する全件数
            
            # 最大15件に制限
            versions_to_show = filtered_versions[:15]
            shown_count = len(versions_to_show)

            # 件数表示の組み立て
            if total_found > shown_count:
                count_info = f"({shown_count}/{total_found}件)" if locale == 'ja' else f"({shown_count}/{total_found} items)"
            else:
                count_info = f"({total_found}件)" if locale == 'ja' else f"({total_found} items)"
            
        for v in versions_to_show:
            ts = v['LastModified'].astimezone(JST)
            sz = round(v['Size'] / (1024 * 1024), 1)
            v_list.append(f"### {ts.strftime('%H:%M:%S')} ({sz}MB) 🆔 `{v['VersionId']}`")
        
        if not v_list: return get_msg("restore", "not_found", locale, date=display_date), None # No items found

        # --- Discordの文字数制限(2000文字)を考慮したメッセージ構築 ---
        header = sync_prefix + pending_msg + get_msg("restore", "list_header", locale, date=display_date, count_info=count_info)
        footer = get_msg("restore", "list_footer", locale)
        
        content_body = ""
        is_truncated = False
        for line in v_list:
            # 2000文字制限に対して余裕(150文字)を持ってチェック
            if len(header) + len(content_body) + len(line) + len(footer) + 150 > 2000:
                is_truncated = True
                break
            content_body += "\n" + line

        # 15件制限にかかった場合もフラグを立てる
        if len(v_list) < total_found:
            is_truncated = True

        if is_truncated:
            content_body += get_msg("restore", "truncated", locale)
            
        return (header + content_body + footer), None

    elif sub_cmd == 'select':
        ec2 = get_client('ec2')
        state = ec2.describe_instances(InstanceIds=[config['instance_id']])['Reservations'][0]['Instances'][0]['State']['Name']
        if state == 'running': return get_msg("restore", "stop_required", locale), None
        
        vid = next((o['value'] for o in sub_options if o['name'] == 'version_id'), None)
        if not vid:
             return get_msg("restore", "failed", locale, err="Version ID is required."), None

        try:
            if config.get('test_mode'):
                print(f"DEBUG: [Test Mode] Skipping actual S3 copy for version {vid}")
                return get_msg("restore", "complete", locale, date="TEST_DATE", id=vid), None

            s3.copy_object(Bucket=bucket, CopySource={'Bucket': bucket, 'Key': key, 'VersionId': vid}, Key=key)

            # 復元成功後、カタログ (DynamoDB) の情報を復元したバージョンの日時に更新
            v_date = vid  # フォールバック用
            try:
                version_meta = s3.head_object(Bucket=bucket, Key=key, VersionId=vid)
                v_timestamp = version_meta['LastModified'].astimezone(JST).isoformat()
                v_date = version_meta['LastModified'].astimezone(JST).strftime('%Y/%m/%d %H:%M:%S')
                v_size = round(version_meta.get('ContentLength', 0) / (1024 * 1024), 1)
                factorio_state_table.update_item(
                    Key={'ConfigKey': 'LatestSaveInfo'},
                    UpdateExpression="set #ts = :val, #sz = :sz",
                    ExpressionAttributeNames={'#ts': 'Timestamp', '#sz': 'FileSize'},
                    ExpressionAttributeValues={':val': v_timestamp, ':sz': str(v_size)}
                )
            except Exception as db_err:
                print(f"Failed to update catalog after restore: {db_err}")

            return get_msg("restore", "complete", locale, date=v_date, id=vid), None
        except Exception as e: return get_msg("restore", "failed", locale, err=str(e)), None

def handle_auto_check(event):
    test_mode = event.get('test_mode', False)
    mock = event.get('mock_data', {}) # テスト用の擬似データ
    locale = event.get('locale', 'ja')
    
    ec2 = get_client('ec2')
    inst = ec2.describe_instances(InstanceIds=[config['instance_id']])['Reservations'][0]['Instances'][0]
    state_name = inst['State']['Name']
    if state_name != 'running' and not test_mode: return

    # --- 0. 停止処理中の監視 ---
    # 手動停止(/stop)が進行中の場合、タイムアウトをチェックする
    res_stop = factorio_state_table.get_item(Key={'ConfigKey': 'StopStartTime'})
    if 'Item' in res_stop:
        stop_ts = float(res_stop['Item'].get('Timestamp', 0))
        elapsed_from_stop = time.time() - stop_ts
        # 停止タイムアウト閾値 (例: 10分)
        shutdown_timeout = int(config.get('shutdown_timeout_seconds', 600))

        if elapsed_from_stop > shutdown_timeout:
            msg = get_msg("worker_specific", "shutdown_failed", locale)
            if test_mode: return {"action": "shutdown_timeout_detected", "suppressed_msg": msg}
            # ログチャットに警告を送信し、停止命令を再試行する
            notify(msg, mode='log')
            get_client('lambda').invoke(FunctionName=config['executor_lambda_name'], InvocationType='Event', Payload=json.dumps({'action': 'stop', 'locale': locale}))
            return {"action": "retry_stop_triggered", "reason": "Shutdown timeout", "suppressed_msg": msg}
        return {"action": "none", "reason": "Stop sequence in progress"}

    # --- 1. 起動失敗(ハングアップ)の検知 ---
    # 起動マーカー(StartStartTime)が存在する場合、まだ初期起動プロセス中であることを意味する
    mock_start = mock.get('mock_start_time')
    if mock_start:
        # テスト用に過去の開始時刻を擬似的に生成
        res_start = {'Item': {'Timestamp': str(time.time() - mock_start)}}
    else:
        res_start = factorio_state_table.get_item(Key={'ConfigKey': 'StartStartTime'})

    if 'Item' in res_start:
        start_ts = float(res_start['Item'].get('Timestamp', 0))
        elapsed_from_start = time.time() - start_ts
        # タイムアウト閾値: RCONポーリング最大時間 + 余裕(5分)
        # デフォルト設定(12回*10秒)では短すぎるため、一律15分(900秒)をデッドラインとする
        startup_timeout = int(config.get('startup_timeout_seconds', 900))
        
        if elapsed_from_start > startup_timeout:
            msg = get_msg("worker_specific", "startup_failed", locale)
            if test_mode: return {"action": "startup_timeout_detected", "suppressed_msg": msg}
            notify(msg, mode='log')
            # プロセスが立ち上がらないままEC2だけ動いている状態なので強制停止
            get_client('lambda').invoke(FunctionName=config['executor_lambda_name'], InvocationType='Event', Payload=json.dumps({'action': 'stop', 'locale': locale}))
            return {"action": "stop_triggered", "reason": "Startup timeout", "suppressed_msg": msg}
        return {"action": "none", "reason": "Server is still booting"}
    
    # RCON結果の擬似化
    if 'rcon_res' in mock:
        res = mock['rcon_res']
    else:
        ip = inst.get('PublicIpAddress')
        res = run_rcon_command(ip, config['rcon_port'], config['rcon_password'], "/players online")
    
    if "Error" in res:
        # --- 2. 実行中クラッシュの検知 ---
        # オフラインカウントの擬似化
        off_count = mock.get('offline_count', factorio_state_table.get_item(Key={'ConfigKey': 'OfflineCount'}).get('Item', {}).get('CountValue', 0)) + 1
        if off_count >= int(config.get('rcon_unresponsive_threshold', 2)):
            # --- 3. 再起動ループの防止 ---
            restart_count = mock.get('auto_restart_count', factorio_state_table.get_item(Key={'ConfigKey': 'AutoRestartCount'}).get('Item', {}).get('CountValue', 0)) + 1
            max_restarts = int(config.get('max_auto_restart_limit', 3))

            if restart_count > max_restarts:
                if test_mode: return {"action": "restart_loop_limit_reached"}
                notify(get_msg("worker_specific", "restart_limit_reached", locale), mode='log')
                # 無限ループ防止のためサーバーを停止
                get_client('lambda').invoke(FunctionName=config['executor_lambda_name'], InvocationType='Event', Payload=json.dumps({'action': 'stop', 'locale': locale}))
                return {"action": "stop_triggered", "reason": "Restart loop detected"}

            if test_mode: return {"action": "restart_triggered", "reason": "RCON unresponsive"}
            notify(get_msg("worker_specific", "rcon_unresponsive", locale), mode='log') # ログチャットにアラート送信
            get_client('ssm').send_command(InstanceIds=[config['instance_id']], DocumentName="AWS-RunShellScript", Parameters={'commands': ["sudo systemctl restart factorio"]})
            factorio_state_table.update_item(Key={'ConfigKey': 'AutoRestartCount'}, UpdateExpression="set CountValue = :v", ExpressionAttributeValues={':v': restart_count})
            off_count = 0
        if test_mode: return {"action": "count_offline", "current": off_count}
        factorio_state_table.update_item(Key={'ConfigKey': 'OfflineCount'}, UpdateExpression="set CountValue = :v", ExpressionAttributeValues={':v': off_count})
    else:
        match = re.search(r"\((\d+)\)", res)
        p_count = int(match.group(1)) if match else 1
        if p_count == 0:
            # 無人カウントの擬似化
            z_count = mock.get('zero_player_count', factorio_state_table.get_item(Key={'ConfigKey': 'ZeroPlayerCount'}).get('Item', {}).get('CountValue', 0)) + 1
            threshold = int(config.get('zero_player_threshold', 3))
            if z_count >= threshold:
                if test_mode: return {"action": "invoke_executor", "target_action": "stop", "reason": "Zero players threshold reached"}
                
                # 停止理由の詳細をログチャットに通知
                duration_mins = threshold * 5 # 5分間隔のチェックを想定
                log_msg = "🔌 [LOG] Auto-shutdown triggered. Reason: The server has been unattended for a certain period of time."
                notify(log_msg, mode='log')
                
                get_client('lambda').invoke(FunctionName=config['executor_lambda_name'], InvocationType='Event', Payload=json.dumps({'action': 'stop', 'locale': locale})) # localeを渡す
                notify(get_msg("worker_specific", "auto_shutdown", locale, duration=duration_mins), mode='webhook')
                z_count = 0
            if test_mode: return {"action": "count_zero_players", "current": z_count}
            factorio_state_table.update_item(Key={'ConfigKey': 'ZeroPlayerCount'}, UpdateExpression="set CountValue = :v", ExpressionAttributeValues={':v': z_count})
        else:
            if test_mode: return {"action": "reset_counts", "reason": "Players online"}
            factorio_state_table.update_item(Key={'ConfigKey': 'ZeroPlayerCount'}, UpdateExpression="set CountValue = :v", ExpressionAttributeValues={':v': 0})
            factorio_state_table.update_item(Key={'ConfigKey': 'OfflineCount'}, UpdateExpression="set CountValue = :v", ExpressionAttributeValues={':v': 0})
    return {"action": "none"}

def lambda_handler(event, context):
    if not config["initialized"]: init_config()

    # invocation ごとに test_mode をリセット（ステート汚染防止）
    action = event.get('action')
    test_mode = event.get('test_mode', False)
    config["test_mode"] = test_mode

    # データの安全な取得
    data_options = event.get('data', {}).get('options', [])
    sub_cmd_name = data_options[0].get('name') if data_options else None

    if action == 'auto-check':
        res = handle_auto_check(event)
        if test_mode:
            if isinstance(res, dict):
                res["suppressed_logs"] = _suppressed_logs
            return res
        
    elif action == 'restore':
        result, components = handle_restore(event)
        if test_mode:
            # In test mode, return the content, components and suppressed logs directly
            return {"content": result, "components": components, "suppressed_logs": _suppressed_logs, "mode": 'patch' if (sub_cmd_name == 'list') else 'followup'}
        # listコマンドの場合はメッセージを書き換え(patch)
        is_patch = (sub_cmd_name == 'list')
        notify(result, mode='patch' if is_patch else 'followup', event=event, components=components)
    return {"status": "ok"}
