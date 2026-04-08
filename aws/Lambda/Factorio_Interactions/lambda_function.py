import json
import boto3
import os
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

# 環境変数
PUBLIC_KEY = os.environ['DISCORD_PUBLIC_KEY']
CHILD_LAMBDA_NAME = 'Factorio_Executor' 

lambda_client = boto3.client('lambda')

def lambda_handler(event, context):
    # 1. 署名検証
    signature = event['headers'].get('x-signature-ed25519')
    timestamp = event['headers'].get('x-signature-timestamp')
    body = event.get('body', '')

    verify_key = VerifyKey(bytes.fromhex(PUBLIC_KEY))
    try:
        verify_key.verify(f'{timestamp}{body}'.encode(), bytes.fromhex(signature))
    except (BadSignatureError, TypeError, ValueError):
        return {'statusCode': 401, 'body': 'invalid request signature'}

    data = json.loads(body)
    
    # 2. PING応答
    if data.get('type') == 1:
        return {'statusCode': 200, 'body': json.dumps({'type': 1})}

    # 3. スラッシュコマンド（Type 2）
    if data.get('type') == 2:
        command_name = data.get('data', {}).get('name')
        
        # 子Lambdaを非同期で呼び出す（全データを引き継ぐ）
        lambda_client.invoke(
            FunctionName=CHILD_LAMBDA_NAME,
            InvocationType='Event',
            Payload=json.dumps(data)
        )
        
        # ユーザーへの初期応答
        if command_name == 'start':
            content = "🚀 Factorioサーバーを起動しています..."
        elif command_name == 'stop':
            content = "🛑 サーバーを停止しています..."
        else:
            content = "リクエストを受理しました。"

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json'
            },
            'body': json.dumps({
                'type': 4,
                'data': {
                    'content': content
                }
            })
        }

    return {'statusCode': 400}