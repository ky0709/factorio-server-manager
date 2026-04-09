import boto3
from botocore.exceptions import ClientError
import json
import os
import time
import socket
import struct
import urllib.request
import re
from datetime import datetime, timedelta, timezone

# 環境変数の読み込み
INSTANCE_ID = os.environ.get('INSTANCE_ID')
REGION = os.environ.get('REGION')
RCON_PORT = int(os.environ.get('RCON_PORT', '27015'))
DYNAMODB_TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME')
SAVE_FILE_KEY = os.environ.get('SAVE_FILE_KEY', 'save.zip') # メインセーブファイルのS3キー
S3_BUCKET = os.environ.get('S3_BUCKET_NAME')

ec2 = boto3.client('ec2', region_name=REGION)
ssm = boto3.client('ssm', region_name=REGION)
dynamodb = boto3.resource('dynamodb', region_name=REGION)
factorio_state_table = dynamodb.Table(DYNAMODB_TABLE_NAME)
s3 = boto3.client('s3', region_name=REGION)

def send_webhook_message(url, content):
    """Discord Webhookにメッセージを送信する"""
    if not url:
        print("Warning: DISCORD_WEBHOOK_URL is not set.")
        return

    payload = json.dumps({"content": content}).encode('utf-8')
    req = urllib.request.Request(url, data=payload, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('User-Agent', 'DiscordBot (FactorioManager, 1.0)')

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            print(f"Webhook Response ({response.status})")
    except Exception as e:
        print(f"Webhook Send Failed: {e}")

def get_rcon_password():
    """SSM Parameter StoreからSecureStringのパスワードを取得"""
    print("Fetching RCON password from SSM...")
    response = ssm.get_parameter(
        Name='/factorio/RCON_PASSWORD',
        WithDecryption=True
    )
    return response['Parameter']['Value']

def get_webhook_url():
    """SSM Parameter StoreからWebhook URLを取得"""
    print("Fetching Webhook URL from SSM...")
    response = ssm.get_parameter(
        Name='/factorio/DISCORD_WEBHOOK_URL',
        WithDecryption=True
    )
    return response['Parameter']['Value']

def run_rcon_command(ip, port, password, command):
    """FactorioサーバーにRCONコマンドを送信する (Source RCON Protocol)"""
    try:
        with socket.create_connection((ip, port), timeout=5) as sock:
            def send_packet(pkt_id, pkt_type, body):
                # Packet format: Size(4), ID(4), Type(4), Body(str), Term(2)
                data = struct.pack('<ii', pkt_id, pkt_type) + body.encode('utf-8') + b'\x00\x00'
                sock.sendall(struct.pack('<i', len(data)) + data)

            def receive_packet():
                raw_size = sock.recv(4)
                if not raw_size: return -1, -1, ""
                size = struct.unpack('<i', raw_size)[0]
                data = sock.recv(size)
                pkt_id, pkt_type = struct.unpack('<ii', data[:8])
                return pkt_id, pkt_type, data[8:-2].decode('utf-8', errors='ignore')

            # 1. 認証 (Type 3: SERVERDATA_AUTH)
            send_packet(1, 3, password)
            pkt_id, _, _ = receive_packet()
            if pkt_id == -1: return "RCON Authentication Failed (Invalid Password)"

            # 2. コマンド実行 (Type 2: SERVERDATA_EXECCOMMAND)
            send_packet(2, 2, command)
            _, _, response = receive_packet()
            return response
    except Exception as e:
        return f"RCON Connection Error: {str(e)}"

def get_player_count(ip, port, password):
    """RCONでオンラインプレイヤー数を取得する"""
    try:
        response = run_rcon_command(ip, port, password, "/players online")
        # Expected response format: "Online players (1):" or "Online players (0)"
        match = re.search(r'Online players \((\d+)\)', response, re.IGNORECASE)
        if match:
            return int(match.group(1))
        print(f"Could not parse player count from RCON response: {response}")
        return -1 # Indicate parsing failure
    except Exception as e:
        print(f"Error getting player count via RCON: {e}")
        return -1


def execute_ec2_command(command_name, command_data=None):
    """EC2の起動・停止・状態確認のコアロジック"""
    print(f"Executing EC2 command: {command_name}")
    try:
        # 1. 最新のインスタンス状態とIPを取得
        res = ec2.describe_instances(InstanceIds=[INSTANCE_ID])
        instance = res['Reservations'][0]['Instances'][0]
        state = instance['State']['Name']
        ip = instance.get('PublicIpAddress')

        if command_name == 'start':
            if state == 'running':
                return "ALREADY_RUNNING"

            print(f"Starting EC2 (Current state: {state})...")
            ec2.start_instances(InstanceIds=[INSTANCE_ID])
            waiter = ec2.get_waiter('instance_running')
            # 起動には30秒以上かかることが多いため、MaxAttemptsを12（60秒）に拡張
            waiter.wait(InstanceIds=[INSTANCE_ID], WaiterConfig={'Delay': 5, 'MaxAttempts': 12})
            res = ec2.describe_instances(InstanceIds=[INSTANCE_ID])
            ip = res['Reservations'][0]['Instances'][0].get('PublicIpAddress', '取得中...')
            return f"✅ Factorioサーバーが起動しました。\n接続先: `{ip}:34197`"
            
        elif command_name == 'stop':
            # すでに停止している場合は特殊なステータスを返す
            if state == 'stopped':
                return "ALREADY_STOPPED"

            if state == 'running' and ip:
                password = get_rcon_password()
                print(f"Sending /server-save to {ip}:{RCON_PORT}")
                rcon_res = run_rcon_command(ip, RCON_PORT, password, "/server-save")
                print(f"RCON Response: {rcon_res}")
                time.sleep(2)

                # クリーンシャットダウン・シーケンス
                # 1. サービス停止 (ファイルハンドル解放) -> 2. sync (キャッシュ書き出し) -> 3. umount
                shutdown_commands = [
                    "sudo systemctl stop factorio",
                    "sync",
                    "sudo umount -l /mnt/factorio-saves"
                ]
                
                print(f"Executing cleanup commands via SSM on {INSTANCE_ID}...")
                try:
                    send_res = ssm.send_command(
                        InstanceIds=[INSTANCE_ID],
                        DocumentName="AWS-RunShellScript",
                        Parameters={'commands': shutdown_commands}
                    )
                    command_id = send_res['Command']['CommandId']
                    
                    # 最大30秒間、クリーンアップの完了を待機
                    for i in range(6): # 5秒 * 6回 = 30秒
                        try:
                            invocations = ssm.list_command_invocations(CommandId=command_id, InstanceId=INSTANCE_ID)['CommandInvocations']
                            if invocations:
                                status = invocations[0]['Status']
                                if status in ['Success', 'Failed', 'Cancelled', 'TimedOut']: # TimedOutも考慮
                                    print(f"Cleanup commands finished with status: {status}")
                                    break
                                else:
                                    print(f"SSM command status: {status}. Waiting...")
                            else:
                                print(f"SSM command invocation {command_id} not yet available. Waiting...")
                        except ClientError as ce:
                            print(f"Error checking SSM command status (ClientError): {ce}. Proceeding with instance stop.")
                            break # クライアントエラーが発生した場合は待機を中断
                        except Exception as e:
                            print(f"Unexpected error checking SSM command status: {e}. Proceeding with instance stop.")
                            break # 予期せぬエラーが発生した場合は待機を中断
                        time.sleep(5)
                except Exception as e:
                    print(f"Lazy unmount failed: {e}. Proceeding with instance stop.")

            ec2.stop_instances(InstanceIds=[INSTANCE_ID])
            print("Waiting for instance to enter 'stopped' state...")
            waiter = ec2.get_waiter('instance_stopped')
            waiter.wait(InstanceIds=[INSTANCE_ID], WaiterConfig={'Delay': 5, 'MaxAttempts': 24})
            return "✅ セーブ完了を確認し、サーバーを停止しました。"

        elif command_name == 'status':
            print("Checking EC2 status...")
            state_map = { # state変数はexecute_ec2_commandの冒頭で取得済み
                'running': "🟢 実行中 (Running)",
                'stopped': "⚪ 停止済み (Stopped)",
                'pending': "🟡 起動準備中... (Pending)",
                'stopping': "🟡 停止処理中... (Stopping)"
            }
            status_text = state_map.get(state, state)
            return f"現在のサーバー状態: {status_text}"
        elif command_name == 'restore':
            # サブコマンドの取得 (list/select)
            options = command_data.get('options', []) if command_data else []
            sub_cmd = options[0] if options else {}
            
            if sub_cmd.get('name') == 'list':
                # 日付オプションの取得
                sub_options = sub_cmd.get('options', [])
                date_str = next((opt['value'] for opt in sub_options if opt['name'] == 'date'), None)
                
                # JSTタイムゾーンの定義
                jst = timezone(timedelta(hours=9))
                if not date_str:
                    date_str = datetime.now(jst).strftime('%Y%m%d')
                
                print(f"Listing versions for date: {date_str}")
                
                try:
                    # 全てのバージョンを取得
                    versions = s3.list_object_versions(Bucket=S3_BUCKET, Prefix=SAVE_FILE_KEY)
                    
                    main_save_versions_for_date = [] # セーブのバージョンを格納
                    for v in versions.get('Versions', []):
                        key = v['Key']
                        # UTCからJSTへ変換
                        jst_modified = v['LastModified'].astimezone(jst)
                        if jst_modified.strftime('%Y%m%d') == date_str:
                            main_save_versions_for_date.append(v)

                    found_versions_display = []

                    # セーブの全バージョンを表示
                    main_save_versions_for_date.sort(key=lambda x: x['LastModified'], reverse=True) # 最新順にソート
                    for v in main_save_versions_for_date:
                        jst_modified = v['LastModified'].astimezone(jst)
                        is_latest_s3 = " ⭐" if v.get('IsLatest') else ""
                        time_str = jst_modified.strftime('%H:%M:%S')
                        filename = v['Key'] # S3バケットのルートに直接save.zipがあるため、キーがそのままファイル名
                        found_versions_display.append(f"[{filename}] `{v['VersionId']}` - {time_str}{is_latest_s3}")

                    if not found_versions_display:
                        return f"📅 {date_str} のセーブバックアップは見つかりませんでした。"
                    
                    res_msg = f"📅 **{date_str} のバックアップ一覧 (JST)**\n"
                    res_msg += "\n".join(found_versions_display)
                    res_msg += "\n\n復元するには `/restore select version_id:<ID>` を実行してください。\n(※セーブ直後は一覧への反映に時間がかかる場合があります)"
                    return res_msg
                    
                except Exception as e:
                    print(f"S3 List Error: {e}")
                    return f"❌ バージョン一覧の取得中にエラーが発生しました。"
            elif sub_cmd.get('name') == 'select':
                # 1. 状態確認（停止中のみ許可）
                res = ec2.describe_instances(InstanceIds=[INSTANCE_ID])
                state = res['Reservations'][0]['Instances'][0]['State']['Name']
                if state != 'stopped':
                    return "❌ 復元操作を行う前に、サーバーを停止してください。"

                # イベントから version_id を取得
                sub_options = sub_cmd.get('options', [])
                version_id = next((opt['value'] for opt in sub_options if opt['name'] == 'version_id'), None)

                if not version_id:
                    return "❌ 復元するバージョンIDが指定されていません。"

                # 2. 復元処理 (指定されたバージョンを最新としてコピー)
                try:
                    print(f"Restoring {SAVE_FILE_KEY} from VersionId: {version_id}")
                    s3.copy_object(
                        Bucket=S3_BUCKET,
                        Key=SAVE_FILE_KEY,
                        CopySource={'Bucket': S3_BUCKET, 'Key': SAVE_FILE_KEY, 'VersionId': version_id}
                    )
                    return f"✅ セーブデータの復元が完了しました。\n対象: `{SAVE_FILE_KEY}`\nVersionId: `{version_id}`"
                except ClientError as e:
                    error_code = e.response['Error']['Code']
                    print(f"S3 Copy Error: {e}")
                    return f"❌ 復元に失敗しました: {error_code}。バージョンIDが正しいか、またはS3バケットが存在するか確認してください。"

        elif command_name == 'save':
            print("Executing /save command: Saving Factorio server.")

            if state == 'running' and ip:
                password = get_rcon_password()
                print(f"Sending /server-save to {ip}:{RCON_PORT}")
                rcon_res = run_rcon_command(ip, RCON_PORT, password, "/server-save")
                print(f"RCON Response: {rcon_res}")
                if "RCON Connection Error" in rcon_res or "RCON Authentication Failed" in rcon_res:
                    return f"❌ セーブに失敗しました。RCON接続を確認してください: {rcon_res}"
                elif "Saving the map" in rcon_res:
                    # OSのバッファをS3マウントポイントへ強制書き出し
                    print(f"Triggering OS sync via SSM on {INSTANCE_ID}...")
                    ssm.send_command(
                        InstanceIds=[INSTANCE_ID],
                        DocumentName="AWS-RunShellScript",
                        Parameters={'commands': ["sync"]}
                    )
                    return "✅ セーブが完了しました。(S3同期開始)"
                else:
                    return f"⚠️ セーブコマンドは実行されましたが、予期せぬ応答です: {rcon_res}"
            elif state == 'stopped':
                return "⚪ サーバーは停止しています。セーブは実行できません。"
            else:
                return f"🟡 サーバーは現在 {state} 状態です。セーブは実行できません。"

        return f"不明なコマンドです: {command_name}"

    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'IncorrectInstanceState':
            return "現在停止処理中のため、しばらく待ってから再度お試しください。"
        print(f"EC2 ClientError: {e}")
        return f"❌ AWS操作中にエラーが発生しました: {error_code}"
    except Exception as e:
        print(f"EC2 Error: {e}")
        return f"❌ AWS操作中にエラーが発生しました: {str(e)}"


def handle_discord_command(event):
    print(f"Received event: {json.dumps(event)}")
    token = event.get('token')
    app_id = event.get('application_id')
    command_name = event.get('action') or event.get('data', {}).get('name')

    print(f"Parsed data: command={command_name}, app_id={app_id}, has_token={'Yes' if token else 'No'}")

    if not token or not app_id:
        print("Error: Missing token or application_id.")
        return {"status": "error", "reason": "missing credentials"}

    # 共通ロジックの実行
    message = execute_ec2_command(command_name, event.get('data'))

    # 特殊な戻り値をユーザー向けメッセージに変換
    if message == "ALREADY_RUNNING":
        message = "🟢 サーバーは既に起動しています。"
    elif message == "ALREADY_STOPPED":
        message = "⚪ サーバーは既に停止しています。"

    # Discordへの報告
    edit_url = f"https://discord.com/api/v10/webhooks/{app_id}/{token}/messages/@original"
    payload = json.dumps({"content": message}).encode('utf-8')
    req = urllib.request.Request(edit_url, data=payload, method='PATCH')
    req.add_header('Content-Type', 'application/json')
    req.add_header('User-Agent', 'DiscordBot (FactorioManager, 1.0)')

    print(f"Sending PATCH to Discord: {edit_url}")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            print(f"Discord Response ({response.status})")
    except Exception as e:
        print(f"Discord Update Failed: {e}")

    return {"status": "done"}


def handle_scheduled_monitor_event(event):
    print("Processing scheduled monitor event...")
    try:
        response = factorio_state_table.get_item(Key={'ConfigKey': 'ZeroPlayerCount'})
        item = response.get('Item', {'ConfigKey': 'ZeroPlayerCount', 'CountValue': 0})
        zero_player_count = item['CountValue']
        print(f"Current ZeroPlayerCount from DynamoDB: {zero_player_count}")
    except Exception as e:
        print(f"Error getting ZeroPlayerCount from DynamoDB: {e}")
        return {"status": "error", "reason": f"DynamoDB read error: {str(e)}"}

    try:
        res = ec2.describe_instances(InstanceIds=[INSTANCE_ID])
        instance_state = res['Reservations'][0]['Instances'][0]['State']['Name']
        instance_ip = res['Reservations'][0]['Instances'][0].get('PublicIpAddress')
        print(f"EC2 instance state: {instance_state}, IP: {instance_ip}")
    except Exception as e:
        print(f"Error describing EC2 instance: {e}")
        return {"status": "error", "reason": f"EC2 describe error: {str(e)}"}

    if instance_state == 'running' and instance_ip:
        rcon_password = get_rcon_password()
        online_players = get_player_count(instance_ip, RCON_PORT, rcon_password)
        print(f"Online players: {online_players}")

        if online_players == 0:
            new_zero_player_count = zero_player_count + 1
            factorio_state_table.update_item(
                Key={'ConfigKey': 'ZeroPlayerCount'},
                UpdateExpression='SET CountValue = :val',
                ExpressionAttributeValues={':val': new_zero_player_count}
            )
            print(f"Incremented ZeroPlayerCount to: {new_zero_player_count}")

            if new_zero_player_count >= 3:
                print("Zero players for 3 consecutive checks. Initiating auto-shutdown...")
                webhook_url = get_webhook_url()
                
                # 共通ロジックで停止を実行
                shutdown_message = execute_ec2_command('stop', {}) # スケジュールイベントではDiscordコンテキストがないため、S3イベント通知は発生しない
                print(shutdown_message)

                if shutdown_message not in ["ALREADY_RUNNING", "ALREADY_STOPPED"]:
                    send_webhook_message(webhook_url, f"【自動停止】{shutdown_message}")

                factorio_state_table.update_item(
                    Key={'ConfigKey': 'ZeroPlayerCount'},
                    UpdateExpression='SET CountValue = :val',
                    ExpressionAttributeValues={':val': 0}
                )
                print("ZeroPlayerCount reset to 0 after auto-shutdown.")
            else:
                print(f"Zero players detected. Count: {new_zero_player_count}. Not yet at shutdown threshold.")
        elif online_players > 0:
            if zero_player_count > 0:
                factorio_state_table.update_item(
                    Key={'ConfigKey': 'ZeroPlayerCount'},
                    UpdateExpression='SET CountValue = :val',
                    ExpressionAttributeValues={':val': 0}
                )
                print("Players detected. ZeroPlayerCount reset to 0.")
            else:
                print("Players detected. ZeroPlayerCount already 0.")
        else:
            print("Skipping count update due to RCON connection error.")

    else:
        if zero_player_count > 0:
            factorio_state_table.update_item(
                Key={'ConfigKey': 'ZeroPlayerCount'},
                UpdateExpression='SET CountValue = :val',
                ExpressionAttributeValues={':val': 0}
            )
            print("EC2 not running or IP not available. ZeroPlayerCount reset to 0.")
        else:
            print("EC2 not running or IP not available. ZeroPlayerCount already 0.")
    
    return {"status": "done", "message": "Scheduled monitor event processed."}


def lambda_handler(event, context):
    """
    メインハンドラー: Discordコマンド、自動無人監視(auto-check)、
    および定時実行(stop/start)の多重停止ロジックを制御します。
    """
    is_scheduled = event.get('source') == 'aws.events' or 'action' in event
    is_discord = event.get('token') and event.get('application_id')

    if is_scheduled and not is_discord:
        action = event.get('action', 'auto-check')
        if action == 'auto-check':
            return handle_scheduled_monitor_event(event)
        else:
            # stop や start などの個別アクションを実行
            message = execute_ec2_command(action, {}) # スケジュールイベントではコマンドデータは不要
            
            # すでに目的の状態であった場合は通知をスキップ
            if message not in ["ALREADY_RUNNING", "ALREADY_STOPPED"]:
                webhook_url = get_webhook_url()
                send_webhook_message(webhook_url, f"【定時実行】{message}")

            return {"status": "done", "message": message}

    if is_discord:
        return handle_discord_command(event)

    else:
        print(f"Unknown event type received: {json.dumps(event)}")
        return {"status": "error", "reason": "Unknown event type"}