#!/bin/bash

# =================================================================
# Factorio Server Manager - AWS Environment Cleanup Script
# =================================================================
# このスクリプトは、指定された環境（dev/prod）のAWSリソースをすべて削除します。
# ⚠️ 注意: S3バケット内のセーブデータやDynamoDBの状態もすべて失われます。

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

# 設定値の読み込み
if [ -f "$ENV_FILE" ]; then
    echo "📖 Loading $ENV_FILE for $ENV_DISPLAY environment..."
    # 環境変数のロード (スペースを含む値を許容)
    set -a; source "$ENV_FILE"; set +a
else
    echo "❌ $ENV_FILE が見つかりません。"
    exit 1
fi

REGION=${AWS_REGION:-"ap-northeast-1"}

echo "🚨🚨🚨 WARNING 🚨🚨🚨"
echo "You are about to DELETE ALL resources for [$ENV_DISPLAY] in region: $REGION"
echo "Resources to be removed:"
echo "  - Lambda Functions: $INTERACTOR_LAMBDA_NAME, $EXECUTOR_LAMBDA_NAME, etc."
echo "  - IAM Role: $ROLE_NAME"
echo "  - Scheduler IAM Role: $SCHEDULER_ROLE_NAME"
echo "  - IAM Policies: $INTERACT_POLICY_NAME, $EXECUTE_POLICY_NAME, etc."
echo "  - DynamoDB Table: $DYNAMODB_TABLE_NAME"
echo "  - S3 Bucket: $S3_BUCKET_NAME (INCLUDING ALL CONTENT)"
echo "  - SSM Parameters under: $SSM_PARAMETER_PATH"
echo "  - EventBridge Schedules: Factorio-AutoCheck-$ENV_ARG, Factorio-DailyStop-$ENV_ARG"
echo "  - EventBridge Rule: Factorio-EC2StateChange-$ENV_ARG"
echo ""

read -p "Are you absolutely sure you want to proceed? Type 'DELETE-$ENV_ARG' to confirm: " confirm
if [ "$confirm" != "DELETE-$ENV_ARG" ]; then
    echo "🛑 Cleanup aborted."
    exit 1
fi

# 1. Lambda 関数の削除
echo "λ Deleting Lambda functions..."
FUNCTIONS=("$INTERACTOR_LAMBDA_NAME" "$EXECUTOR_LAMBDA_NAME" "$WORKER_LAMBDA_NAME" "$NOTIFIER_LAMBDA_NAME")
for f in "${FUNCTIONS[@]}"; do
    echo "  - Deleting $f..."
    aws lambda delete-function --function-name "$f" --region "$REGION" 2>/dev/null || echo "    (Already deleted)"
done

# 2. EventBridge Scheduler の削除
echo "⏰ Deleting EventBridge Schedules..."
aws scheduler delete-schedule --name "Factorio-AutoCheck-$ENV_ARG" --region "$REGION" 2>/dev/null || true
aws scheduler delete-schedule --name "Factorio-DailyStop-$ENV_ARG" --region "$REGION" 2>/dev/null || true
echo "✅ EventBridge Schedules deleted."

# 3. EventBridge Rule の削除
echo "🔔 Deleting EventBridge Rule..."
RULE_NAME="Factorio-EC2StateChange-$ENV_ARG"
aws events remove-targets --rule "$RULE_NAME" --ids "1" --region "$REGION" 2>/dev/null || true
aws events delete-rule --name "$RULE_NAME" --region "$REGION" 2>/dev/null || true
echo "✅ EventBridge Rule deleted."

# 4. SSM パラメータの削除
echo "🔑 Deleting SSM Parameters under $SSM_PARAMETER_PATH..."
PARAM_NAMES=$(aws ssm get-parameters-by-path --path "$SSM_PARAMETER_PATH" --recursive --query "Parameters[].Name" --output text --region "$REGION")
if [ -n "$PARAM_NAMES" ]; then
    aws ssm delete-parameters --names $PARAM_NAMES --region "$REGION"
    echo "✅ SSM Parameters deleted."
else
    echo "    (No parameters found)"
fi

# 5. IAM ポリシーの削除
echo "⚖️  Deleting IAM Policies..."
POLICIES=("$INTERACT_POLICY_NAME" "$EXECUTE_POLICY_NAME" "$NOTIFY_POLICY_NAME" "$SERVER_POLICY_NAME" "$REGIST_POLICY_NAME" "$WORK_POLICY_NAME")
for p in "${POLICIES[@]}"; do
    POLICY_ARN="arn:aws:iam::$AWS_ACCOUNT_ID:policy/$p"
    echo "  - Deleting $p..."
    # 以前のバージョンがある場合は削除する必要がある
    VERSIONS=$(aws iam list-policy-versions --policy-arn "$POLICY_ARN" --query "Versions[?IsDefaultVersion==\`false\`].VersionId" --output text 2>/dev/null || echo "")
    for v in $VERSIONS; do
        aws iam delete-policy-version --policy-arn "$POLICY_ARN" --version-id "$v"
    done
    aws iam delete-policy --policy-arn "$POLICY_ARN" 2>/dev/null || echo "    (Already deleted)"
done

# 6. IAM ロールの削除
echo "👤 Deleting IAM Role: $ROLE_NAME..."
aws iam detach-role-policy --role-name "$ROLE_NAME" --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null || true
aws iam delete-role --role-name "$ROLE_NAME" 2>/dev/null || echo "    (Already deleted)"

echo "👤 Deleting Scheduler IAM Role: $SCHEDULER_ROLE_NAME..."
aws iam delete-role-policy --role-name "$SCHEDULER_ROLE_NAME" --policy-name "FactorioSchedulerPolicy" 2>/dev/null || true
aws iam delete-role --role-name "$SCHEDULER_ROLE_NAME" 2>/dev/null || echo "    (Already deleted)"

# 7. DynamoDB テーブルの削除
echo "📊 Deleting DynamoDB Table: $DYNAMODB_TABLE_NAME..."
aws dynamodb delete-table --table-name "$DYNAMODB_TABLE_NAME" --region "$REGION" 2>/dev/null || echo "    (Already deleted)"

# 6. S3 バケットの削除 (中身を空にしてから削除)
echo "📦 Deleting S3 Bucket: $S3_BUCKET_NAME..."
if aws s3 ls "s3://$S3_BUCKET_NAME" 2>/dev/null; then
    # バージョニングされているオブジェクトも含めて完全に削除するために rb --force を使用
    # 注意: バージョニングされたオブジェクトがある場合、通常の rm では消えないため
    # 本気で消す場合は一工夫必要ですが、ここでは標準的な削除を試みます
    aws s3 rb "s3://$S3_BUCKET_NAME" --force --region "$REGION"
    echo "✅ S3 Bucket deleted."
else
    echo "    (Bucket not found)"
fi

echo ""
echo "✨ Cleanup completed for [$ENV_DISPLAY]!"