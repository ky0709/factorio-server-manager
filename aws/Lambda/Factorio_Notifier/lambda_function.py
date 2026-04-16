import json
import boto3
import urllib.request
import urllib.error
import time
import os

ssm = boto3.client('ssm')
# Webhook URL のキャッシュ用
_url_cache = {}

def get_webhook_url(key='DISCORD_WEBHOOK_URL'):
    """SSM Parameter StoreからWebhook URLを取得"""
    # 環境変数からベースパスを取得。末尾のスラッシュを考慮
    base_path = os.environ.get('SSM_PARAMETER_PATH', '/factorio/')
    if not base_path.endswith('/'): base_path += '/'
    
    path = f"{base_path}{key}"

    if path in _url_cache:
        return _url_cache[path]

    try:
        # TODO ID:003: String化後の単体取得互換性を確認
        response = ssm.get_parameter(
            Name=path,
            WithDecryption=True
        )
        url = response['Parameter']['Value'].strip("'\" ")
        _url_cache[path] = url
        return url
    except Exception as e:
        print(f"❌ Error fetching SSM parameter {path}: {e}")
        return None

def post_to_discord(url, payload, method='POST'):
    """Discord APIにリクエストを送信"""
    data = json.dumps(payload).encode('utf-8')
    
    # レースコンディション対策: 404 (Unknown Webhook) の場合は最大3回リトライ
    for attempt in range(3):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header('Content-Type', 'application/json')
        req.add_header('User-Agent', 'DiscordBot (FactorioNotifier, 1.0)')

        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                print(f"Discord API Response ({response.status})")
                return True
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            # 404 Unknown Webhook (10015) の場合は、Discord側の登録待ちの可能性がある
            if e.code == 404 and "10015" in error_body and attempt < 2:
                wait_time = (attempt + 1) * 2
                print(f"⚠️ Discord reported 10015. Retrying in {wait_time}s... (Attempt {attempt + 1})")
                time.sleep(wait_time)
                continue
            
            masked_url = url.split('/')[-1][:5] + "..." if '/' in url else "unknown"
            print(f"❌ Discord API Call Failed: HTTP {e.code} at endpoint tail {masked_url} - {error_body}")
            return False
        except Exception as e:
            print(f"❌ Discord API Call Failed: {e}")
            return False
    return False

def lambda_handler(event, context):
    """
    event: {
        'mode': 'followup' | 'patch' | 'webhook' | 'log',
        'content': 'message string',
        'application_id': '...', (mode='followup' の場合必須)
        'token': '...',          (mode='followup' の場合必須)
        'flags': 64              (任意: Ephemeral等)
    }
    """
    mode = event.get('mode', 'webhook')
    content = event.get('content')
    embeds = event.get('embeds')
    components = event.get('components')
    
    payload = {}
    if content is not None: payload['content'] = content # 空文字 "" を許容して既存テキストを上書き消去可能にする
    if embeds is not None: payload['embeds'] = embeds
    if 'flags' in event: # Flags can be for both followup and patch
        payload['flags'] = event['flags']
    if components is not None: payload['components'] = components
    
    event_type = event.get('type')

    # Discord APIはcontentまたはembedsのいずれかを必須とする。
    if payload.get('content') is None and not payload.get('embeds'):
        # contentもembedsも存在しない場合はエラー
        print("❌ Error: Attempted to send an empty message payload to Discord.")
        return {"status": "error", "message": "Empty payload"}

    # レースコンディション対策: ボタン操作(Type 3)のPATCHの場合、Discord側の承認処理を待つ
    if mode == 'patch' and event_type == 3:
        time.sleep(0.5)

    # embedsが存在し、contentが指定されていない（None）場合は、contentを空文字列に設定する。
    if payload.get('embeds') and payload.get('content') is None:
        payload['content'] = ""

    if mode == 'followup':
        app_id = event.get('application_id') or os.getenv('APP_ID')
        token = event.get('token')
        if not app_id or not token:
            print("⚠️ Skipping followup: Missing application_id or token.")
            return {"status": "error", "message": "Missing credentials"}
        # Followup POST URL
        url = f"https://discord.com/api/v10/webhooks/{app_id}/{token}"
        post_to_discord(url, payload, method='POST')
        
    elif mode == 'patch':
        app_id = event.get('application_id') or os.getenv('APP_ID')
        token = event.get('token')
        if not app_id or not token:
            print("⚠️ Skipping patch: Missing application_id or token.")
            return {"status": "error", "message": "Missing credentials"}
        # 初期応答メッセージを更新する URL
        url = f"https://discord.com/api/v10/webhooks/{app_id}/{token}/messages/@original"
        post_to_discord(url, payload, method='PATCH')
        
    elif mode == 'webhook':
        url = get_webhook_url('DISCORD_WEBHOOK_URL') or os.getenv('DISCORD_WEBHOOK_URL')
        if url: post_to_discord(url, payload, method='POST') # SSMがダメなら環境変数も試す
        
    elif mode == 'log':
        urls_raw = get_webhook_url('DISCORD_LOG_WEBHOOK_URL')
        if urls_raw:
            urls = [u.strip() for u in urls_raw.split(',') if u.strip()]
            for url in urls:
                post_to_discord(url, payload, method='POST')

    return {"status": "ok"}
