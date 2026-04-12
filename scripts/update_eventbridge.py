import os
import boto3
import sys
import json
from dotenv import load_dotenv
from botocore.exceptions import ClientError

def update_eventbridge_resources():
    # プロジェクトルートの取得
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 引数から環境を選択 (デフォルト: prod)
    env_arg = sys.argv[1] if len(sys.argv) > 1 else "prod"
    env_file = ".env" if env_arg == "prod" else f".env.{env_arg}"
    env_path = os.path.join(BASE_DIR, env_file)

    if not os.path.exists(env_path):
        print(f"❌ Environment file {env_file} not found.")
        sys.exit(1)

    load_dotenv(env_path)
    
    # 1. 実行確認 (AUTO_CONFIRM が設定されていない場合のみ)
    if os.getenv('AUTO_CONFIRM') != '1':
        if env_arg == 'prod':
            print(f"🚨 ATTENTION: You are about to update PRODUCTION EventBridge resources.")
            confirm = input(f"Proceed with update for 'prod'? (y/N): ")
            if confirm.lower() != 'y':
                print("🛑 Operation cancelled.")
                sys.exit(1)
            prod_confirm = input("⚠️  FINAL CONFIRMATION: To proceed, please type 'UPDATE-PROD': ")
            if prod_confirm != 'UPDATE-PROD':
                print("🛑 Update aborted.")
                sys.exit(1)
        else:
            confirm = input(f"Proceed with EventBridge update for '{env_arg}'? (y/N): ")
            if confirm.lower() != 'y':
                print("🛑 Operation cancelled.")
                sys.exit(1)

    region = os.getenv('AWS_REGION', 'ap-northeast-1').strip().strip("'\"")
    account_id = os.getenv('AWS_ACCOUNT_ID', '').strip().strip("'\"")
    instance_id = os.getenv('INSTANCE_ID', '').strip().strip("'\"")
    executor_lambda_name = os.getenv('EXECUTOR_LAMBDA_NAME', '').strip().strip("'\"")
    worker_lambda_name = os.getenv('WORKER_LAMBDA_NAME', '').strip().strip("'\"")
    
    # リソース名の決定 (prod の場合はサフィックスなし)
    suffix = f"-{env_arg}" if env_arg == "dev" else ""
    auto_check_name = os.getenv('AUTO_CHECK_SCHEDULE_NAME', f"Factorio-AutoCheck{suffix}")
    daily_stop_name = os.getenv('DAILY_STOP_SCHEDULE_NAME', f"Factorio-DailyStop{suffix}")
    rule_name = os.getenv('EC2_STATE_RULE_NAME', f"Factorio-EC2StateChange{suffix}")
    
    # スケジューラ用IAMロール名 (環境変数から取得)
    base_eb_role = os.getenv('EVENTBRIDGE_ROLE_NAME', 'FactorioEventBridgeRole')
    eb_role_name = f"{base_eb_role}{suffix}"
    
    # スケジュール式の取得
    auto_check_expr = os.getenv('AUTO_CHECK_SCHEDULE', 'rate(5 minutes)')
    daily_stop_expr = os.getenv('DAILY_STOP_CRON', 'cron(0 15 * * ? *)')

    scheduler = boto3.client('scheduler', region_name=region)
    events = boto3.client('events', region_name=region)
    awslambda = boto3.client('lambda', region_name=region)
    iam = boto3.client('iam')
    role_arn = f"arn:aws:iam::{account_id}:role/{eb_role_name}"
    policy_name = os.getenv('EVENTBRIDGE_POLICY_NAME', 'FactorioEventBridgePolicy')
    policy_arn = f"arn:aws:iam::{account_id}:policy/{policy_name}"

    print(f"\n--- Updating EventBridge Resources for [{env_arg.upper()}] ---")

    # 0. EventBridge 用 IAM ロールの権限を確認 (管理ポリシーのアタッチ)
    try:
        # 以前のインラインポリシーが残っている場合はクリーンアップ (移行措置)
        try:
            iam.delete_role_policy(RoleName=eb_role_name, PolicyName="FactorioSchedulerPolicy")
            print(f"🗑️  Removed legacy inline policy from {eb_role_name}")
        except iam.exceptions.NoSuchEntityException: pass

        print(f"⚖️  Ensuring Managed Policy is attached to {eb_role_name}...")
        iam.attach_role_policy(RoleName=eb_role_name, PolicyArn=policy_arn)
        print("✅ EventBridge Managed Policy attachment ensured.")
    except Exception as e:
        print(f"⚠️  Could not ensure policy attachment: {e}")

    # 1. EventBridge Scheduler (Schedules) の更新
    schedules = [
        {
            "Name": auto_check_name,
            "Expression": auto_check_expr,
            "TargetArn": f"arn:aws:lambda:{region}:{account_id}:function:{worker_lambda_name}",
            "Input": '{"action": "auto-check"}'
        },
        {
            "Name": daily_stop_name,
            "Expression": daily_stop_expr,
            "TargetArn": f"arn:aws:lambda:{region}:{account_id}:function:{executor_lambda_name}",
            "Input": '{"action": "stop"}'
        }
    ]

    for sch in schedules:
        try:
            # 現在の設定を取得して比較
            current = scheduler.get_schedule(Name=sch['Name'])
            current_state = current.get('State', 'ENABLED')
            is_same = (
                current.get('ScheduleExpression') == sch['Expression'] and
                current.get('Target', {}).get('Arn') == sch['TargetArn'] and
                current.get('Target', {}).get('Input') == sch['Input'] and
                current.get('Target', {}).get('RoleArn') == role_arn
            )

            if is_same:
                print(f"✨ Skipping Schedule: {sch['Name']} (No changes detected)")
                continue

            print(f"🔄 Updating Schedule: {sch['Name']} to {sch['Expression']}...")
            scheduler.update_schedule(
                Name=sch['Name'],
                ScheduleExpression=sch['Expression'],
                State=current_state,
                Target={
                    'Arn': sch['TargetArn'],
                    'RoleArn': role_arn,
                    'Input': sch['Input']
                },
                FlexibleTimeWindow={'Mode': 'OFF'}
            )
            print(f"✅ Successfully updated {sch['Name']}")
        except scheduler.exceptions.ResourceNotFoundException:
            print(f"⚠️  Schedule {sch['Name']} not found. Please run init_aws_resources.py first.")
        except Exception as e:
            print(f"❌ Failed to update {sch['Name']}: {e}")

    # 2. EventBridge Rule (EC2 State Change) の更新 (INSTANCE_ID 変更の反映)
    try:
        # 現在のルールとターゲットの状態を取得
        try:
            current_rule = events.describe_rule(Name=rule_name)
            current_pattern_dict = json.loads(current_rule.get('EventPattern', '{}'))
        except events.exceptions.ResourceNotFoundException:
            current_pattern_dict = {}

        current_targets = []
        try:
            current_targets = events.list_targets_by_rule(Rule=rule_name).get('Targets', [])
        except Exception as e:
            print(f"⚠️  Could not list targets for {rule_name}: {e}")
            current_targets = []

        new_pattern = {
            "source": ["aws.ec2"],
            "detail-type": ["EC2 Instance State-change Notification"],
            "detail": {
                "instance-id": [instance_id],
                "state": sorted(["running", "stopped"])
            }
        }
        expected_target_arn = f"arn:aws:lambda:{region}:{account_id}:function:{executor_lambda_name}"
        target_id = '1'
        
        # パターンの正規化関数 (すべてのリストをソート)
        def canonicalize(obj):
            if isinstance(obj, dict):
                return {k: canonicalize(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return sorted([canonicalize(i) for i in obj], key=lambda x: json.dumps(x, sort_keys=True))
            return obj

        canonical_current = canonicalize(current_pattern_dict)
        canonical_new = canonicalize(new_pattern)
        pattern_match = canonical_current == canonical_new
        
        # ターゲットID '1' が期待通りのARNを指しているか確認
        target_match = any(t.get('Id') == target_id and t.get('Arn') == expected_target_arn and t.get('RoleArn') == role_arn for t in current_targets)

        current_state = current_rule.get('State', 'ENABLED')

        if pattern_match and target_match:
            print(f"✨ Skipping Event Rule: {rule_name} (No changes detected)")
        else:
            if not pattern_match:
                print(f"🔍 DEBUG: Pattern mismatch detected.")
                # print(f"  Current: {json.dumps(canonical_current)}")
                # print(f"  New:     {json.dumps(canonical_new)}")
            if not target_match:
                print(f"🔍 DEBUG: Target mismatch detected.")
                print(f"  Expected: {expected_target_arn} (ID: {target_id})")
                print(f"  Found:    {[t.get('Arn') for t in current_targets]}")

            print(f"🔄 Updating Event Rule: {rule_name} for instance {instance_id}...")
            events.put_rule(Name=rule_name, EventPattern=json.dumps(new_pattern), State=current_state)
            events.put_targets(Rule=rule_name, Targets=[{'Id': target_id, 'Arn': expected_target_arn, 'RoleArn': role_arn}])
            print(f"✅ Successfully updated {rule_name}")

        # 3. Lambda の呼び出し権限 (Permission) の確認と追加
        try:
            statement_id = f"AllowEventBridgeNotify-{env_arg}"
            rule_arn = f"arn:aws:events:{region}:{account_id}:rule/{rule_name}"
            
            # 既存のポリシーを取得して Sid と SourceArn の整合性を確認
            add_needed = True
            try:
                policy_resp = awslambda.get_policy(FunctionName=executor_lambda_name)
                policy_dict = json.loads(policy_resp['Policy'])
                
                for statement in policy_dict.get('Statement', []):
                    sid = statement.get('Sid')
                    # 過去のタイポ (Alllow) も含めて既存の権限をチェック
                    if sid in [statement_id, f"AlllowEventBridgeNotify-{env_arg}"]:
                        # 同一IDが存在する場合、SourceArn が現在のルールと一致するか確認
                        current_source_arn = statement.get('Condition', {}).get('ArnLike', {}).get('AWS:SourceArn')
                        if current_source_arn == rule_arn:
                            add_needed = False
                        else:
                            # ARN が古い場合は一度削除して再作成
                            print(f"🔄 SourceArn mismatch or legacy ID found for {sid}. Re-creating permission...")
                            awslambda.remove_permission(FunctionName=executor_lambda_name, StatementId=sid)
                        break
            except awslambda.exceptions.ResourceNotFoundException:
                pass # ポリシーがない場合は新規作成
            except ClientError as e:
                if e.response['Error']['Code'] == 'AccessDeniedException':
                    print(f"⚠️  AccessDenied: Cannot check existing Lambda policy. Please ensure 'lambda:GetPolicy' is allowed and deployed.")
                else:
                    print(f"⚠️  Warning while checking policy: {e}")
            except Exception as e:
                print(f"⚠️  Warning while checking policy: {e}")

            if add_needed:
                try:
                    print(f"🔐 Adding EventBridge invocation permission to {executor_lambda_name}...")
                    awslambda.add_permission(
                        FunctionName=executor_lambda_name,
                        StatementId=statement_id,
                        Action="lambda:InvokeFunction",
                        Principal="events.amazonaws.com",
                        SourceArn=rule_arn
                    )
                    print("✅ Permission added.")
                except ClientError as e:
                    if e.response['Error']['Code'] == 'ResourceConflictException':
                        print(f"ℹ️  Permission statement already exists (Sid: {statement_id}).")
                    else:
                        raise
        except Exception as perm_err:
            print(f"⚠️  Could not update Lambda permission: {perm_err}")
            
    except events.exceptions.ResourceNotFoundException:
        print(f"⚠️  Event Rule {rule_name} not found. Please run init_aws_resources.py first.")
    except Exception as e:
        print(f"❌ Failed to update {rule_name}: {e}")

if __name__ == "__main__":
    # セッション確認
    try:
        if not boto3.Session().get_credentials() and not os.getenv('AWS_PROFILE'):
            BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env_arg = sys.argv[1] if len(sys.argv) > 1 else "prod"
            load_dotenv(os.path.join(BASE_DIR, ".env" if env_arg == "prod" else f".env.{env_arg}"))
        
        update_eventbridge_resources()
    except Exception as e:
        print(f"❌ Auth Error: {e}")
        sys.exit(1)