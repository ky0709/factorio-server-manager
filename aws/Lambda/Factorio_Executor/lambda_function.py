import boto3
import json
import os
import time
import socket
import struct
import urllib.request

# 環境変数の読み込み
INSTANCE_ID = os.environ.get('INSTANCE_ID')
REGION = os.environ.get('REGION')
RCON_PORT = int(os.environ.get('RCON_PORT', '27015'))

ec2 = boto3.client('ec2', region_name=REGION)
ssm = boto3.client('ssm', region_name=REGION)

def get_rcon_password():
    """SSM Parameter StoreからSecureStringのパスワードを取得"""
    print("Fetching RCON password from SSM...")
    response = ssm.get_parameter(
        Name='/factorio/RCON_PASSWORD',
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
            print("Preparing to stop EC2. Sending save command first...")
            
            # 現在の状態を確認し、起動中であればセーブを試行
            res = ec2.describe_instances(InstanceIds=[INSTANCE_ID])
            instance = res['Reservations'][0]['Instances'][0]
            state = instance['State']['Name']
            ip = instance.get('PublicIpAddress')

            if state == 'running' and ip:
                password = get_rcon_password()
                print(f"Sending /server-save to {ip}:{RCON_PORT}")
                rcon_res = run_rcon_command(ip, RCON_PORT, password, "/server-save")
                print(f"RCON Response: {rcon_res}")
                time.sleep(2) # セーブ完了のための短い待機

            ec2.stop_instances(InstanceIds=[INSTANCE_ID])

            # 停止完了を待機
            print("Waiting for instance to enter 'stopped' state...")
            waiter = ec2.get_waiter('instance_stopped')
            try:
                # 5秒おきに最大24回（計2分間）チェック
                waiter.wait(
                    InstanceIds=[INSTANCE_ID],
                    WaiterConfig={'Delay': 5, 'MaxAttempts': 24}
                )
                message = "✅ サーバーの停止が完了しました。"
                if state == 'running': message = "💾 セーブ完了を確認し、サーバーを正常に停止しました。"
            except Exception as e:
                print(f"Waiter error or timeout: {e}")
                message = "🛑 停止処理を開始しましたが、完了確認がタイムアウトしました。/status コマンドで後ほど確認してください。"

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