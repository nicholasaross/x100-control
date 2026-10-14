#!/usr/bin/env bash
# Removes everything deploy.sh created in AWS. Local certs/ and bridge.env are left alone.
#   alexa/infra/teardown.sh            (same AWS_REGION / STACK_NAME as you deployed with)
set -euo pipefail
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-west-1}}"
STACK="${STACK_NAME:-x100-alexa}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"   # the project folder (certs/)
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${ARTIFACT_BUCKET:-x100-alexa-$ACCOUNT-$REGION}"

echo "==> Deleting stack $STACK"
aws cloudformation delete-stack --stack-name "$STACK"
aws cloudformation wait stack-delete-complete --stack-name "$STACK"

if [[ -s "$ROOT/certs/certificate-arn" ]]; then
  CERT_ID="$(cut -d/ -f2 < "$ROOT/certs/certificate-arn")"
  echo "==> Revoking and deleting bridge certificate $CERT_ID"
  aws iot update-certificate --certificate-id "$CERT_ID" --new-status INACTIVE || true
  aws iot delete-certificate --certificate-id "$CERT_ID" || true
  rm -f "$ROOT/certs/certificate-arn"
fi

if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  echo "==> Removing artifact bucket $BUCKET"
  aws s3 rb "s3://$BUCKET" --force >/dev/null
fi
echo "Done. Also delete the skill in the Alexa developer console and the Login with Amazon security profile."
