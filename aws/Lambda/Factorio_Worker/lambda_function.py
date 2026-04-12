import json
import re
from datetime import datetime

# レイヤーからのインポート
from factorio_common.utils import JST, get_client, fetch_config_from_ssm, run_rcon_command, format_msg, notify_via_lambda

TEXT_RESOURCES = {
    "restore": {
        "pending_warning": {"ja": "⚠️ **注意: {time} に実行された最新のセーブはまだ S3 に反映されていない可能性があります。**\n", "en": "⚠️ **Note: Latest save at {time} may not be in S3 yet.**\n"},
        "not_found": {"ja": "📁 {date} のセーブデータが見つかりませんでした。", "en": "📁 No save found for {date}."},
        "list_header": {"ja": "📁 **{date} の履歴 {count_info}**\n", "en": "📁 **History for {date} {count_info}**\n"},
        "list_footer": {"ja": "\n\n`/restore select version_id: <ID>` で復元可能です。", "en": "\n\nRestore via `/restore select version_id: <ID>`."},
        "stop_required": {"ja": "❌ 復元前にサーバーを停止してください。", "en": "❌ Stop the server before restoring."},
        "complete": {"ja": "✅ セーブデータの復元が完了しました。\n作成日時: `{date}`\n対象バージョン: `{id}`", "en": "✅ Restore complete.\nCreated at: `{date}`\nVersion ID: `{id}`"},
        "failed": {"ja": "❌ 復元失敗: {err}", "en": "❌ Restore failed: {err}"},
        "syncing": {
            "ja": "⏳ (S3同期中...) ",
            "en": "⏳ (S3 Syncing...) "
        },
        "truncated": {"ja": "\n... (履歴が多いため、一部を省略しました)", "en": "\n... (Some items were omitted due to length limits)"}
    }
}

def get_msg(category, key, locale='ja', **kwargs):
    return format_msg(TEXT_RESOURCES, category, key, locale, **kwargs)

config = {"initialized": False}

def init_config():
    ssm_config = fetch_config_from_ssm()
    config.update(ssm_config)
    global factorio_state_table
    factorio_state_table = get_client('dynamodb', True).Table(config.get('dynamodb_table_name'))
    config["initialized"] = True

def notify(content, mode='followup', event=None, embeds=None, components=None):
    # テストモード時は Discord への通知処理をスキップ
    if config.get("test_mode"):
        print(f"DEBUG: [Test Mode] Notification suppressed: {content}")
        return
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
            v_list.append(f"- `{ts.strftime('%H:%M:%S')}` (`{sz}MB`) ID: `{v['VersionId']}`")
        
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
            if event.get('test_mode'):
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
    
    ec2 = get_client('ec2')
    inst = ec2.describe_instances(InstanceIds=[config['instance_id']])['Reservations'][0]['Instances'][0]
    if inst['State']['Name'] != 'running' and not test_mode: return
    
    # RCON結果の擬似化
    if 'rcon_res' in mock:
        res = mock['rcon_res']
    else:
        ip = inst.get('PublicIpAddress')
        res = run_rcon_command(ip, config['rcon_port'], config['rcon_password'], "/players online")
    
    if "Error" in res:
        # オフラインカウントの擬似化
        off_count = mock.get('offline_count', factorio_state_table.get_item(Key={'ConfigKey': 'OfflineCount'}).get('Item', {}).get('CountValue', 0)) + 1
        if off_count >= int(config.get('rcon_unresponsive_threshold', 2)):
            if test_mode: return {"action": "restart_triggered", "reason": "RCON unresponsive"}
            notify("⚠️ [LOG] Unresponsive. Restarting...", mode='log')
            get_client('ssm').send_command(InstanceIds=[config['instance_id']], DocumentName="AWS-RunShellScript", Parameters={'commands': ["sudo systemctl restart factorio"]})
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
                log_msg = f"🔌 [LOG] Auto-shutdown triggered. Reason: 0 players detected for {duration_mins} minutes."
                notify(log_msg, mode='log')
                
                get_client('lambda').invoke(FunctionName=config['executor_lambda_name'], InvocationType='Event', Payload=json.dumps({'action': 'stop'}))
                notify("⌛ Auto-shutdown initiated.", mode='webhook')
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
    action = event.get('action')
    test_mode = event.get('test_mode', False) # Add test_mode flag
    config["test_mode"] = test_mode # notify ラッパーで参照するために保存

    # データの安全な取得
    data_options = event.get('data', {}).get('options', [])
    sub_cmd_name = data_options[0].get('name') if data_options else None

    if action == 'auto-check':
        res = handle_auto_check(event)
        if test_mode: return res
        
    elif action == 'restore':
        result, components = handle_restore(event)
        if test_mode:
            # In test mode, return the content and components directly
            return {"content": result, "components": components, "mode": 'patch' if (sub_cmd_name == 'list') else 'followup'}
        # listコマンドの場合はメッセージを書き換え(patch)
        is_patch = (sub_cmd_name == 'list')
        notify(result, mode='patch' if is_patch else 'followup', event=event, components=components)
    return {"status": "ok"}
