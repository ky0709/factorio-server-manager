import os
import boto3
import sys
from dotenv import load_dotenv

def update_schedules():
    # プロジェクトルートの取得
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 引数から環境を選択
    env_arg = sys.argv[1] if len(sys.argv) > 1 else "dev"
    env_file = f".env.{env_arg}" if env_arg != "prod" else ".env"
    env_path = os.path.join(BASE_DIR, env_file)

    if not os.path.exists(env_path):
        print(f"❌ Environment file {env_file} not found.")
        sys.exit(1)

    load_dotenv(env_path)
    
    region = os.getenv('AWS_REGION', 'ap-northeast-1')
    account_id = os.getenv('AWS_ACCOUNT_ID')
    
    # スケジュール名の決定
    suffix = env_arg
    auto_check_name = f"Factorio-AutoCheck-{suffix}"
    daily_stop_name = f"Factorio-DailyStop-{suffix}"
    role_name = f"FactorioSchedulerRole-{suffix}" if suffix == "dev" else "FactorioSchedulerRole"
    
    # 式の取得
    auto_check_expr = os.getenv('AUTO_CHECK_SCHEDULE', 'rate(5 minutes)')
    daily_stop_expr = os.getenv('DAILY_STOP_CRON', 'cron(0 15 * * ? *)')

    scheduler = boto3.client('scheduler', region_name=region)
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

    print(f"\n--- Updating EventBridge Schedules for [{env_arg}] ---")

    schedules = [
        {
            "Name": auto_check_name,
            "Expression": auto_check_expr,
            "TargetArn": f"arn:aws:lambda:{region}:{account_id}:function:{os.getenv('WORKER_LAMBDA_NAME')}",
            "Input": '{"action": "auto-check"}'
        },
        {
            "Name": daily_stop_name,
            "Expression": daily_stop_expr,
            "TargetArn": f"arn:aws:lambda:{region}:{account_id}:function:{os.getenv('EXECUTOR_LAMBDA_NAME')}",
            "Input": '{"action": "stop"}'
        }
    ]

    for sch in schedules:
        try:
            print(f"🔄 Updating {sch['Name']} to {sch['Expression']}...")
            scheduler.update_schedule(
                Name=sch['Name'],
                ScheduleExpression=sch['Expression'],
                Target={
                    'Arn': sch['TargetArn'],
                    'RoleArn': role_arn,
                    'Input': sch['Input']
                },
                FlexibleTimeWindow={'Mode': 'OFF'}
            )
            print(f"✅ Successfully updated {sch['Name']}")
        except scheduler.exceptions.ResourceNotFoundException:
            print(f"⚠️  Schedule {sch['Name']} not found. Please run init_aws_resources.sh first.")
        except Exception as e:
            print(f"❌ Failed to update {sch['Name']}: {e}")

if __name__ == "__main__":
    # セッション確認
    try:
        if not boto3.Session().get_credentials() and not os.getenv('AWS_PROFILE'):
            # .env の AWS_PROFILE を読み込むための再ロード
            BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env_arg = sys.argv[1] if len(sys.argv) > 1 else "dev"
            load_dotenv(os.path.join(BASE_DIR, f".env.{env_arg}" if env_arg != "prod" else ".env"))
        
        update_schedules()
    except Exception as e:
        print(f"❌ Auth Error: {e}")
        sys.exit(1)