import os
from dotenv import load_dotenv

def setup_configs():
    # スクリプトの場所を基準にプロジェクトルートを取得
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # .env ファイルをルートから読み込む
    load_dotenv(os.path.join(BASE_DIR, ".env"))
    
    # 置換対象のリスト
    replacements = {
        "<REGION>": os.getenv('AWS_REGION'),
        "<ACCOUNT_ID>": os.getenv('AWS_ACCOUNT_ID'),
        "<INSTANCE_ID>": os.getenv('INSTANCE_ID'),
        "<S3_BUCKET_NAME>": os.getenv('S3_BUCKET_NAME'),
        "<SAVE_FILE_KEY>": os.getenv('SAVE_FILE_KEY'),
    }

    # テンプレートファイルと出力先の対応
    targets = [
        os.path.join(BASE_DIR, "aws/IAM/FactorioControlPolicy/policy.json"),
        os.path.join(BASE_DIR, "aws/IAM/EC2-Factorio-Server-RolePolicy/policy.json"),
        os.path.join(BASE_DIR, "aws/IAM/FactorioRegistPolicy/policy.json")
    ]

    print("--- Generating IAM Policies from Templates ---")

    for target in targets:
        template_path = target + ".example"
        
        if not os.path.exists(template_path):
            print(f"⚠️  Template not found: {template_path}")
            continue

        with open(template_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # プレースホルダーを実際の値で置換
        for placeholder, value in replacements.items():
            if value:
                content = content.replace(placeholder, value)
        
        # 置換後の内容を policy.json として出力
        with open(target, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"✅ Generated: {target}")

if __name__ == "__main__":
    setup_configs()