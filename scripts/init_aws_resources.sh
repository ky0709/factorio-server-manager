#!/bin/bash
set -e # エラーが発生した時点でスクリプトを終了

# =================================================================
# Factorio Server Manager - AWS Environment Initializer
# =================================================================
# このスクリプトは、指定された環境（dev/prod）のAWSリソースの「器」を作成します。
# 実行前に aws configure で適切な権限が設定されていることを確認してください。

# 環境の選択
ENV_ARG=$1
if [ "$ENV_ARG" == "dev" ]; then
    ENV_FILE="../.env.dev"
    ROLE_NAME="FactorioLambdaRole-dev"
    SCHEDULER_ROLE_NAME="FactorioSchedulerRole-dev"
    ENV_DISPLAY="DEVELOPMENT"
else
    ENV_FILE="../.env"
    ROLE_NAME="FactorioLambdaRole"
    SCHEDULER_ROLE_NAME="FactorioSchedulerRole"
    ENV_DISPLAY="PRODUCTION"
    ENV_ARG="prod"
fi

# 設定値の読み込み (現在のディレクトリが scripts であることを想定)
if [ -f "$ENV_FILE" ]; then
    echo "📖 Loading $ENV_FILE for $ENV_DISPLAY environment..."
    # 環境変数のロード (スペースを含む値を許容)
    set -a; source "$ENV_FILE"; set +a
else
    echo "❌ $ENV_FILE が見つかりません。プロジェクトルートで作成してください。"
    exit 1
fi

REGION=${AWS_REGION:-"ap-northeast-1"}

echo "🚀 Starting resource creation for [$ENV_DISPLAY]"
if [ -n "$AWS_PROFILE" ]; then
    echo "👤 Using AWS Profile: $AWS_PROFILE"
else
    echo "⚠️  AWS_PROFILE is not set. Using default credentials."
fi
echo "📍 Region: $REGION"

# 本番環境の場合のみ最終確認
if [ "$ENV_ARG" == "prod" ]; then
    echo "🚨 WARNING: You are about to create PRODUCTION resources."
    read -p "Are you sure you want to proceed? (y/N): " confirm
    [[ $confirm == [yY] ]] || exit 1
fi

# 1. S3 バケットの作成
if aws s3api head-bucket --bucket "$S3_BUCKET_NAME" 2>/dev/null; then
    echo "    (S3 Bucket $S3_BUCKET_NAME already exists)"
else
    echo "📦 Creating S3 Bucket: $S3_BUCKET_NAME..."
    if [[ "$REGION" == "us-east-1" ]]; then
        aws s3api create-bucket --bucket "$S3_BUCKET_NAME" --region "$REGION" || { echo "❌ Failed to create S3 bucket"; exit 1; }
    else
        aws s3api create-bucket --bucket "$S3_BUCKET_NAME" --region "$REGION" --create-bucket-configuration LocationConstraint="$REGION" || { echo "❌ Failed to create S3 bucket"; exit 1; }
    fi
    aws s3api put-bucket-versioning --bucket "$S3_BUCKET_NAME" --versioning-configuration Status=Enabled || { echo "❌ Failed to enable versioning"; exit 1; }
    echo "✅ S3 Bucket created and Versioning enabled."
fi

# 2. DynamoDB テーブルの作成
if aws dynamodb describe-table --table-name "$DYNAMODB_TABLE_NAME" --region "$REGION" >/dev/null 2>&1; then
    echo "    (DynamoDB Table $DYNAMODB_TABLE_NAME already exists)"
else
    echo "📊 Creating DynamoDB Table: $DYNAMODB_TABLE_NAME..."
    aws dynamodb create-table \
        --table-name "$DYNAMODB_TABLE_NAME" \
        --attribute-definitions AttributeName=ConfigKey,AttributeType=S \
        --key-schema AttributeName=ConfigKey,KeyType=HASH \
        --provisioned-throughput ReadCapacityUnits=1,WriteCapacityUnits=1 \
        --region "$REGION" > /dev/null || { echo "❌ Failed to create DynamoDB table"; exit 1; }

    # 初期データの投入 (ZeroPlayerCount)
    echo "📝 Initializing DynamoDB data..."
    aws dynamodb put-item \
        --table-name "$DYNAMODB_TABLE_NAME" \
        --item '{"ConfigKey": {"S": "ZeroPlayerCount"}, "CountValue": {"N": "0"}}' \
        --region "$REGION" || { echo "❌ Failed to initialize DynamoDB data"; exit 1; }
    echo "✅ DynamoDB Table created and initialized."
fi

# 3. IAM ロールの作成 (Lambda用)
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
    echo "    (IAM Role $ROLE_NAME already exists)"
else
    echo "👤 Creating IAM Role for Lambda: $ROLE_NAME..."
    cat <<EOF > lambda-trust-policy.json
    {
      "Version": "2012-10-17",
      "Statement": [
        {
          "Effect": "Allow",
          "Principal": { "Service": "lambda.amazonaws.com" },
          "Action": "sts:AssumeRole"
        }
      ]
    }
EOF
    aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document file://lambda-trust-policy.json > /dev/null || { echo "❌ Failed to create IAM Role"; rm lambda-trust-policy.json; exit 1; }
    aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole || { echo "❌ Failed to attach basic execution policy"; exit 1; }
    rm lambda-trust-policy.json
    echo "✅ IAM Role for Lambda created."
fi

# 4. IAM ポリシーの初回作成
# scripts/deploy_policies.py が「ポリシーが存在しないとエラー」になる仕様のため、空のポリシーを作成
echo "⚖️  Creating initial IAM Policies..."
POLICIES=(
    "$INTERACT_POLICY_NAME" "$EXECUTE_POLICY_NAME" "$NOTIFY_POLICY_NAME" 
    "$SERVER_POLICY_NAME" "$REGIST_POLICY_NAME" "$WORK_POLICY_NAME"
)

EMPTY_POLICY='{"Version":"2012-10-17","Statement":[{"Effect":"Deny","Action":"*","Resource":"*"}]}'

for p_name in "${POLICIES[@]}"; do
    if aws iam get-policy --policy-arn "arn:aws:iam::$AWS_ACCOUNT_ID:policy/$p_name" >/dev/null 2>&1; then
        echo "    (Already exists: $p_name)"
    else
        echo "  - Creating $p_name..."
        aws iam create-policy --policy-name "$p_name" --policy-document "$EMPTY_POLICY" > /dev/null || { echo "❌ Failed to create IAM policy: $p_name"; exit 1; }
    fi
done
echo "✅ Initial IAM Policies created (Now you can run scripts/deploy_policies.py $ENV_ARG)."

# 5. Lambda 関数の器作成 (ダミーコードで初回作成)
echo "λ Creating Lambda functions placeholders..."
echo "def lambda_handler(event, context): return {'status': 'initializing'}" > index.py
zip -q dummy.zip index.py

FUNCTIONS=(
    "$INTERACTOR_LAMBDA_NAME" "$EXECUTOR_LAMBDA_NAME" 
    "$WORKER_LAMBDA_NAME" "$NOTIFIER_LAMBDA_NAME"
)

for f_name in "${FUNCTIONS[@]}"; do
    if aws lambda get-function --function-name "$f_name" --region "$REGION" >/dev/null 2>&1; then
        echo "    (Already exists: $f_name)"
    else
        echo "  - Creating $f_name..."
        aws lambda create-function \
            --function-name "$f_name" \
            --runtime python3.12 \
            --role arn:aws:iam::$AWS_ACCOUNT_ID:role/"$ROLE_NAME" \
            --handler lambda_function.lambda_handler \
            --zip-file fileb://dummy.zip \
            --region "$REGION" > /dev/null || { echo "❌ Failed to create Lambda function: $f_name"; exit 1; }
    fi
done

# 6. スケジューラ用 IAM ロールの作成
if aws iam get-role --role-name "$SCHEDULER_ROLE_NAME" >/dev/null 2>&1; then
    echo "    (IAM Role $SCHEDULER_ROLE_NAME already exists)"
else
    echo "👤 Creating IAM Role for Scheduler: $SCHEDULER_ROLE_NAME..."
    cat <<EOF > scheduler-trust-policy.json
    {
      "Version": "2012-10-17",
      "Statement": [
        {
          "Effect": "Allow",
          "Principal": { "Service": "scheduler.amazonaws.com" },
          "Action": "sts:AssumeRole"
        }
      ]
    }
EOF
    aws iam create-role --role-name "$SCHEDULER_ROLE_NAME" --assume-role-policy-document file://scheduler-trust-policy.json > /dev/null || { echo "❌ Failed to create Scheduler Role"; rm scheduler-trust-policy.json; exit 1; }
    
    # Lambda 呼び出し権限の付与
    cat <<EOF > scheduler-policy.json
    {
      "Version": "2012-10-17",
      "Statement": [
        {
          "Effect": "Allow",
          "Action": "lambda:InvokeFunction",
          "Resource": "arn:aws:lambda:$REGION:$AWS_ACCOUNT_ID:function:Factorio_*"
        }
      ]
    }
EOF
    aws iam put-role-policy --role-name "$SCHEDULER_ROLE_NAME" --policy-name "FactorioSchedulerPolicy" --policy-document file://scheduler-policy.json
    rm scheduler-trust-policy.json scheduler-policy.json
    echo "✅ IAM Role for Scheduler created."
fi

# 7. EventBridge Scheduler の作成 (無人監視 & 定時停止)
echo "⏰ Setting up EventBridge Schedules..."
SCHEDULER_ROLE_ARN="arn:aws:iam::$AWS_ACCOUNT_ID:role/$SCHEDULER_ROLE_NAME"
DAILY_STOP_EXPRESSION=${DAILY_STOP_CRON:-"cron(0 15 * * ? *)"}
AUTO_CHECK_EXPRESSION=${AUTO_CHECK_SCHEDULE:-"rate(5 minutes)"}

# 無人監視
aws scheduler create-schedule \
    --name "Factorio-AutoCheck-$ENV_ARG" \
    --schedule-expression "$AUTO_CHECK_EXPRESSION" \
    --target "{\"Arn\":\"arn:aws:lambda:$REGION:$AWS_ACCOUNT_ID:function:$WORKER_LAMBDA_NAME\",\"RoleArn\":\"$SCHEDULER_ROLE_ARN\",\"Input\":\"{\\\"action\\\": \\\"auto-check\\\"}\"}" \
    --flexible-time-window "{\"Mode\":\"OFF\"}" \
    --region "$REGION" >/dev/null 2>&1 || echo "    (AutoCheck schedule already exists)"

# 定時停止
aws scheduler create-schedule \
    --name "Factorio-DailyStop-$ENV_ARG" \
    --schedule-expression "$DAILY_STOP_EXPRESSION" \
    --target "{\"Arn\":\"arn:aws:lambda:$REGION:$AWS_ACCOUNT_ID:function:$EXECUTOR_LAMBDA_NAME\",\"RoleArn\":\"$SCHEDULER_ROLE_ARN\",\"Input\":\"{\\\"action\\\": \\\"stop\\\"}\"}" \
    --flexible-time-window "{\"Mode\":\"OFF\"}" \
    --region "$REGION" >/dev/null 2>&1 || echo "    (DailyStop schedule already exists)"

# 8. EventBridge Rule の作成 (EC2 状態変更通知)
echo "🔔 Setting up EC2 State-change Notification Rule..."
RULE_NAME="Factorio-EC2StateChange-$ENV_ARG"
aws events put-rule \
    --name "$RULE_NAME" \
    --event-pattern "{\"source\":[\"aws.ec2\"],\"detail-type\":[\"EC2 Instance State-change Notification\"],\"detail\":{\"instance-id\":[\"$INSTANCE_ID\"],\"state\":[\"running\",\"stopped\"]}}" \
    --region "$REGION" >/dev/null

aws events put-targets \
    --rule "$RULE_NAME" \
    --targets "[{\"Id\":\"1\",\"Arn\":\"arn:aws:lambda:$REGION:$AWS_ACCOUNT_ID:function:$EXECUTOR_LAMBDA_NAME\"}]" \
    --region "$REGION" >/dev/null

# Lambda への権限追加 (EventBridge からの呼び出し許可)
aws lambda add-permission \
    --function-name "$EXECUTOR_LAMBDA_NAME" \
    --statement-id "AllowEventBridgeNotify-$ENV_ARG" \
    --action "lambda:InvokeFunction" \
    --principal "events.amazonaws.com" \
    --source-arn "arn:aws:events:$REGION:$AWS_ACCOUNT_ID:rule/$RULE_NAME" \
    --region "$REGION" >/dev/null 2>&1 || true

rm index.py dummy.zip
echo "✅ EventBridge resources and permissions setup complete."

echo ""
echo "================================================================="
echo "🎉 Infrastructure setup complete for [$ENV_DISPLAY]!"
echo "次のステップ:"
echo "1. python scripts/setup_config.py $ENV_ARG  # ポリシーJSONの生成"
echo "2. python scripts/deploy_policies.py $ENV_ARG # 本物の権限を適用"
echo "3. python scripts/deploy_lambda.py $ENV_ARG   # 本物のコードをデプロイ"
echo "4. python scripts/register.py $ENV_ARG        # Discordコマンド & SSM同期"
echo "================================================================="
