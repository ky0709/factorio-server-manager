import json
import boto3
import socket
import struct
from datetime import timezone, timedelta

JST = timezone(timedelta(hours=9))

# クライアントのキャッシュ用
_clients = {}

def get_client(service, is_resource=False):
    """必要になるまで初期化を遅らせる (Lazy Initialization)"""
    key = f"{service}_res" if is_resource else service
    if key not in _clients:
        if is_resource:
            _clients[key] = boto3.resource(service)
        else:
            _clients[key] = boto3.client(service)
    return _clients[key]

def fetch_config_from_ssm(path='/factorio/'):
    """SSMから設定を一括取得して辞書で返す"""
    ssm = get_client('ssm')
    config = {}
    try:
        # パス配下のパラメータを取得
        paginator = ssm.get_paginator('get_parameters_by_path')
        for page in paginator.paginate(Path=path, WithDecryption=True):
            for p in page['Parameters']:
                key = p['Name'].split('/')[-1].lower()
                # 値の前後にある空白や引用符を削除して格納
                config[key] = p['Value'].strip("'\" ")
        return config
    except Exception as e:
        print(f"Error fetching SSM parameters: {e}")
        return {}

def run_rcon_command(ip, port, password, command):
    """FactorioサーバーにRCONコマンドを送信する"""
    try:
        with socket.create_connection((ip, int(port)), timeout=5) as sock:
            def send_packet(p_type, p_body):
                p_id = 0x1234
                packet = struct.pack('<ii', p_id, p_type) + p_body.encode('utf-8') + b'\x00\x00'
                sock.sendall(struct.pack('<i', len(packet)) + packet)
                
                header = sock.recv(4)
                if not header: return None
                p_len = struct.unpack('<i', header)[0]
                p_data = sock.recv(p_len)
                return p_data[8:-2].decode('utf-8')

            # ログイン認証 (Type 3)
            send_packet(3, password)
            # コマンド実行 (Type 2)
            return send_packet(2, command)
    except Exception as e:
        return f"Error: RCON Connection Failed ({str(e)})"

def format_msg(resources, category, key, locale='ja', **kwargs):
    """多言語リソースからメッセージを取得してフォーマットする"""
    lang = 'ja' if locale == 'ja' else 'en'
    try:
        text = resources[category][key][lang]
        return text.format(**kwargs) if kwargs else text
    except (KeyError, TypeError):
        return f"MISSING_TEXT: {category}.{key}"

def notify_via_lambda(notifier_name, content, mode='followup', event=None, embeds=None, components=None):
    """Notifier Lambdaを呼び出して通知を委譲"""
    payload = {'mode': mode, 'content': content}
    if embeds: payload['embeds'] = embeds
    if components: payload['components'] = components
    if event:
        payload.update({
            'application_id': event.get('application_id'), 
            'token': event.get('token'),
            'type': event.get('type') # インタラクションタイプを通知
        })
    get_client('lambda').invoke(FunctionName=notifier_name, InvocationType='Event', Payload=json.dumps(payload))