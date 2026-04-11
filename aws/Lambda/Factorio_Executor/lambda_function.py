import json
import re
import time
from datetime import datetime

# レイヤーからのインポート
from factorio_common.utils import JST, get_client, fetch_config_from_ssm, run_rcon_command, format_msg, notify_via_lambda

TEXT_RESOURCES = {
    "common": {
        "unknown_cmd": {"ja": "❌ 不明なコマンドです。", "en": "❌ Unknown command."},
        "server_offline": {"ja": "❌ サーバーが起動していないため、この操作は実行できません。", "en": "❌ Server is not running."},
    },
    "status": {
        "running": {
            "ja": "✅ **稼働中**\n- 接続先: `{ip}:{port}`\n- オンライン: `{players}`名 (`{names}`)\n- 最終セーブ: `{save_time}` (`{size}`MB)",
            "en": "✅ **Running**\n- Address: `{ip}:{port}`\n- Online: `{players}` (`{names}`)\n- Last Save: `{save_time}` (`{size}`MB)"
        },
        "stopped": {
            "ja": "🔴 **停止中**\n- 最終セーブ: `{save_time}` (`{size}`MB)",
            "en": "🔴 **Stopped**\n- Last Save: `{save_time}` (`{size}`MB)"
        },
        "transition": {"ja": "⏳ **状態遷移中** (`{state}`)", "en": "⏳ **Transitioning** (`{state}`)"},
        "starting": {
            "ja": "⏳ **起動処理中** (経過時間: `{elapsed}`秒)\n- 最終セーブ: `{save_time}` (`{size}`MB)",
            "en": "⏳ **Starting Process** (Elapsed: `{elapsed}`s)\n- Last Save: `{save_time}` (`{size}`MB)"
        },
        "stopping": {
            "ja": "⏳ **停止処理中** (経過時間: `{elapsed}`秒)\n- 最終セーブ: `{save_time}` (`{size}`MB)",
            "en": "⏳ **Stopping Process** (Elapsed: `{elapsed}`s)\n- Last Save: `{save_time}` (`{size}`MB)"
        },
        "syncing": {
            "ja": "⏳ (S3同期中...) ",
            "en": "⏳ (S3 Syncing...) "
        }
    },
    "start": {
        "success": {"ja": "🚀 サーバーの起動を開始しました。", "en": "🚀 Starting server..."},
        "already": {"ja": "⚠️ サーバーは既に起動しているか、準備中です。", "en": "⚠️ Server is already running or pending."},
        "completed": {
            "ja": "- 接続先: `{ip}:{port}`\n- パスワード: `{pwd}`",
            "en": "- Address: `{ip}:{port}`\n- Password: `{pwd}`"
        },
        "completed_title": {
            "ja": "✨ Factorio サーバー起動完了",
            "en": "✨ Factorio Server Ready"
        }
    },
    "stop": {
        "success": {"ja": "🔌 サーバーの停止を開始しました (セーブ・クリーンアップ実行中)。", "en": "🔌 Stopping server (Saving and cleaning up...)"},
        "already": {"ja": "⚠️ サーバーは既に停止しているか、停止処理中です。", "en": "⚠️ Server is already stopped or stopping."},
        "process_stopped": {"ja": "⏹️ Factorioプロセスを正常に終了しました。", "en": "⏹️ Factorio process stopped successfully."},
        "completed": {"ja": "✅ サーバーの全停止工程が完了しました。", "en": "✅ Server shutdown sequence completed."}
    },
    "save": {
        "success": {"ja": "💾 セーブコマンドを送信しました。S3への反映には数分かかる場合があります。", "en": "💾 Save command sent. S3 sync may take a few minutes."},
    },
    "pass": {
        "not_set": {"ja": "❌ パスワードは設定されていません。", "en": "❌ Password is not set."},
        "display": {"ja": "🔑 パスワード: `{pwd}`", "en": "🔑 Password: `{pwd}`"}
    },
    "license": {
        "content": {
            "ja": "### 📜 License Information\n本ソフトウェアは **MIT License** の下で公開されています。\n\n**■ 許諾事項**\nどなたでも無償で本ソフトウェアの使用、複写、変更、結合、掲載、頒布、サブライセンス、および販売を行うことができます。\n\n**■ 利用条件**\nすべての複製または重要な部分に、後述の著作権表示および本許諾表示を記載する必要があります。\n\n**■ 免責事項**\n本ソフトウェアは「現状のまま」提供されます。作者は、ソフトウェアの使用に起因する損害やその他の責任について一切の義務を負いません。\n\n---\n**Copyright (c) 2026 ky0709**\n**GitHub:** https://github.com/ky0709/factorio-server-manager",
            "en": "### 📜 License Information\nThis software is published under the **MIT License**.\n\n**■ Permissions**\nPermission is hereby granted, free of charge, to any person obtaining a copy of this software to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the software.\n\n**■ Conditions**\nThe above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.\n\n**■ Disclaimer**\nTHE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND. THE AUTHORS OR COPYRIGHT HOLDERS SHALL NOT BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY.\n\n---\n**Copyright (c) 2026 ky0709**\n**GitHub:** https://github.com/ky0709/factorio-server-manager"
        }
    }
}

COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_YELLOW = 0xF1C40F
COLOR_BLUE = 0x3498DB

def get_msg(category, key, locale='ja', **kwargs):
    return format_msg(TEXT_RESOURCES, category, key, locale, **kwargs)

config = {"initialized": False}

def init_config():
    ssm_config = fetch_config_from_ssm('/factorio/')
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

def execute_ec2_command(action, event):
    locale = event.get('locale', 'ja')
    ec2 = get_client('ec2')
    inst = ec2.describe_instances(InstanceIds=[config['instance_id']])['Reservations'][0]['Instances'][0]
    state = inst['State']['Name']
    ip = inst.get('PublicIpAddress')

    if action == 'status':
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
                        return get_msg("status", "starting", locale, elapsed=elapsed, save_time=save_time, size=save_size)
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
            if not db_timestamp or s3_dt.isoformat() > db_timestamp:
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

    elif action == 'start':
        if state == 'stopped':
            start_process_time = time.time()

            # 以前の停止処理マーカーが残っている可能性があるため強制削除
            factorio_state_table.delete_item(Key={'ConfigKey': 'StopStartTime'})

            # イベント検知時の経過時間算出用に開始時刻を記録
            factorio_state_table.update_item(
                Key={'ConfigKey': 'StartStartTime'},
                UpdateExpression="set #ts = :val",
                ExpressionAttributeNames={'#ts': 'Timestamp'},
                ExpressionAttributeValues={':val': str(start_process_time)}
            )

            ec2.start_instances(InstanceIds=[config['instance_id']])
            
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
                    check = run_rcon_command(current_ip, config['rcon_port'], config['rcon_password'], "/version")
                    if "Error" not in check:
                        elapsed = int(time.time() - start_process_time)
                        # ログチャットに詳細な起動時間を通知
                        notify(f"🚀 [LOG] Factorio server is ready on {current_ip}:{config.get('factorio_game_port', 34197)}. (Time taken: {elapsed}s)", mode='log')
                        
                        # 起動が確認できたので、ステータス表示用の起動マーカーを削除
                        factorio_state_table.delete_item(Key={'ConfigKey': 'StartStartTime'})

                        # パスワードとポート情報を取得して完了メッセージを生成
                        pwd = config.get("game_password", "-")
                        port = config.get('factorio_game_port', 34197)
                        return {
                            "embeds": [{
                                "title": get_msg("start", "completed_title", locale),
                                "description": get_msg("start", "completed", locale, ip=current_ip, port=port, pwd=pwd),
                                "color": COLOR_GREEN
                            }]
                        }
                time.sleep(interval)

            return get_msg("start", "success", locale)
        return get_msg("start", "already", locale)

    elif action == 'stop':
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

            # 1. セーブの実行 (RCON)
            run_rcon_command(ip, config['rcon_port'], config['rcon_password'], "/server-save")
            
            # カタログ (DynamoDB) を更新
            factorio_state_table.update_item(
                Key={'ConfigKey': 'LatestSaveInfo'},
                UpdateExpression="set #ts = :val",
                ExpressionAttributeNames={'#ts': 'Timestamp'},
                ExpressionAttributeValues={':val': datetime.now(JST).isoformat()}
            )

            # 2. Factorioサーバー停止 & アンマウント準備 (SSM)
            ssm = get_client('ssm')
            ssm.send_command(
                InstanceIds=[config['instance_id']],
                DocumentName="AWS-RunShellScript",
                Parameters={'commands': ["sudo systemctl stop factorio"]}
            )

            # 3. アンマウント実行 (SSM)
            # サービス停止後、少し待ってから実行
            time.sleep(5)
            ssm.send_command(
                InstanceIds=[config['instance_id']],
                DocumentName="AWS-RunShellScript",
                Parameters={'commands': ["sudo umount /mnt/factorio-saves || true"]}
            )
            
            # 4. EC2停止
            ec2.stop_instances(InstanceIds=[config['instance_id']])
            
            # 6. メインチャットへの完了報告 (これによって Interactor のメッセージが PATCH される)
            return get_msg("stop", "process_stopped", locale)
        return get_msg("stop", "already", locale)

    elif action == 'save':
        if state != 'running': return get_msg("common", "server_offline", locale)
        run_rcon_command(ip, config['rcon_port'], config['rcon_password'], "/server-save")
        # カタログ (DynamoDB) を更新
        factorio_state_table.update_item(
            Key={'ConfigKey': 'LatestSaveInfo'},
            UpdateExpression="set #ts = :val",
            ExpressionAttributeNames={'#ts': 'Timestamp'},
            ExpressionAttributeValues={':val': datetime.now(JST).isoformat()}
        )
        return get_msg("save", "success", locale)

    elif action == 'pass':
        pwd = config.get("game_password")
        return get_msg("pass", "display", locale, pwd=pwd) if pwd else get_msg("pass", "not_set", locale)

    elif action == 'license':
        return {
            "embeds": [{
                "title": "⚖️ MIT License",
                "description": get_msg("license", "content", locale),
                "footer": {"text": "ky0709/factorio-server-manager"},
                "color": COLOR_BLUE
            }]
        }

    return get_msg("common", "unknown_cmd", locale)

def lambda_handler(event, context):
    if not config["initialized"]: init_config()

    # EventBridge からの EC2 状態変更通知の処理
    if event.get('source') == 'aws.ec2' and event.get('detail-type') == 'EC2 Instance State-change Notification':
        detail = event.get('detail', {})
        instance_id = detail.get('instance-id', '')
        
        # state が辞書形式 {'name': 'stopped'} か、文字列 "stopped" かを判定して取得
        raw_state = detail.get('state')
        if isinstance(raw_state, dict):
            state_name = raw_state.get('name')
        else:
            state_name = raw_state

        # デバッグログ: 受信したイベントの内容を出力
        print(f"DEBUG: Received EC2 event for {instance_id} state={state_name}. Expected ID={config.get('instance_id')}")

        # インスタンスIDの比較 (常に正規化して比較)
        if instance_id and instance_id.strip("'\" ") == str(config.get("instance_id", "")).strip("'\" "):
            if state_name == 'stopped':
                # 停止開始時刻を DynamoDB から取得して経過時間を算出
                res = factorio_state_table.get_item(Key={'ConfigKey': 'StopStartTime'})
                start_time_str = res.get('Item', {}).get('Timestamp')
                
                elapsed_msg = ""
                if start_time_str:
                    elapsed = int(time.time() - float(start_time_str))
                    elapsed_msg = f" (Total sequence time: {elapsed}s)"
                
                notify(f"🔌 [LOG] EC2 Instance ({instance_id}) has stopped. Shutdown sequence completed.{elapsed_msg}", mode='log')
                factorio_state_table.delete_item(Key={'ConfigKey': 'StopStartTime'})
                return {"status": "ok"}
            elif state_name == 'running':
                # 起動開始時刻を DynamoDB から取得して経過時間を算出
                res = factorio_state_table.get_item(Key={'ConfigKey': 'StartStartTime'})
                start_time_str = res.get('Item', {}).get('Timestamp')
                
                elapsed_msg = ""
                if start_time_str:
                    elapsed = int(time.time() - float(start_time_str))
                    elapsed_msg = f" (EC2 boot time: {elapsed}s)"
                
                notify(f"🚀 [LOG] EC2 Instance ({instance_id}) is now running.{elapsed_msg}", mode='log')
                factorio_state_table.delete_item(Key={'ConfigKey': 'StartStartTime'})
                return {"status": "ok"}
        
        # イベント対象外であっても、EventBridgeイベントであるならここで終了させる
        return {"status": "event_handled_or_ignored"}

    action = event.get('action')
    locale = event.get('locale', 'ja')
    test_mode = event.get('test_mode', False)
    config["test_mode"] = test_mode # notify ラッパーで参照するために保存

    # 更新(PATCH)対象の判定
    notify_mode = 'patch' if action in ['status', 'pass', 'license'] else 'followup'
    
    result = execute_ec2_command(action, event)
    
    # If in test_mode, return the result directly for inspection
    if test_mode:
        if isinstance(result, dict): # For embeds
            return {"content": None, "embeds": result.get('embeds'), "mode": notify_mode}
        else: # For plain text
            return {"content": result, "embeds": None, "mode": notify_mode}

    # Discord Interaction (Tokenが存在する) 場合のみ、応答を返す
    if event.get('token'):
        if isinstance(result, dict):
            notify(None, mode=notify_mode, event=event, embeds=result.get('embeds'))
        else:
            notify(result, mode=notify_mode, event=event)
        
    return {"status": "ok"}