import boto3
import json
import os
import time
import urllib.request

# 環境変数の読み込み
INSTANCE_ID = os.environ.get('INSTANCE_ID')
REGION = os.environ.get('REGION')
ec2 = boto3.client('ec2', region_name=REGION)

def lambda_handler(event, context):
    # 1. デバッグログ（親から何が届いたかCloudWatchで100%確認するため）
    print(f"Received event: {json.dumps(event)}")

    # 2. データの解析（親Lambdaが event を丸投げしている想定）
    # もし None になる場合は、親Lambdaの json.dumps(data) の中身を確認
    token = event.get('token')
    app_id = event.get('application_id')
    
    # スラッシュコマンドの名前またはアクションを取得
    command_name = event.get('action') or event.get('data', {}).get('name')

    print(f"Parsed data: command={command_name}, app_id={app_id}, has_token={'Yes' if token else 'No'}")

    # パラメータが足りない場合の早期リターン
    if not token or not app_id:
        print("Error: Missing token or application_id. Check parent Lambda's payload.")
        return {"status": "error", "reason": "missing credentials"}

    message = "リクエストを処理しました。"

    try:
        # 3. EC2の操作
        if command_name == 'start':
            print("Starting EC2...")
            ec2.start_instances(InstanceIds=[INSTANCE_ID])
            
            # IP確定待ち
            time.sleep(12)
            res = ec2.describe_instances(InstanceIds=[INSTANCE_ID])
            ip = res['Reservations'][0]['Instances'][0].get('PublicIpAddress', '取得中...')
            message = f"✅ Factorioサーバーが起動しました。\n接続先: `{ip}:34197`"
            
        elif command_name == 'stop':
            print("Stopping EC2...")
            ec2.stop_instances(InstanceIds=[INSTANCE_ID])
            message = "✅ サーバーを停止しました。"

        elif command_name == 'status':
            print("Checking EC2 status...")
            res = ec2.describe_instances(InstanceIds=[INSTANCE_ID])
            state = res['Reservations'][0]['Instances'][0]['State']['Name']
            
            # 状態に応じたメッセージのマッピング
            state_map = {
                'running': "🟢 実行中 (Running)",
                'stopped': "⚪ 停止済み (Stopped)",
                'pending': "🟡 起動準備中... (Pending)",
                'stopping': "🟡 停止処理中... (Stopping)"
            }
            status_text = state_map.get(state, state)
            message = f"現在のサーバー状態: {status_text}"

        else:
            message = f"不明なコマンドです: {command_name}"

    except Exception as e:
        print(f"EC2 Error: {e}")
        message = f"❌ AWS操作中にエラーが発生しました: {str(e)}"

    # 4. DiscordへのPATCH送信（報告）
    edit_url = f"https://discord.com/api/v10/webhooks/{app_id}/{token}/messages/@original"
    payload = json.dumps({"content": message}).encode('utf-8')
    
    req = urllib.request.Request(edit_url, data=payload, method='PATCH')
    req.add_header('Content-Type', 'application/json')
    req.add_header('User-Agent', 'DiscordBot (FactorioManager, 1.0)')

    print(f"Sending PATCH to Discord: {edit_url}")
    
    try:
        # タイムアウト10秒設定で送信
        with urllib.request.urlopen(req, timeout=10) as response:
            res_body = response.read().decode()
            print(f"Discord Response ({response.status}): {res_body}")
    except Exception as e:
        print(f"Discord Update Failed: {e}")

    return {"status": "done"}