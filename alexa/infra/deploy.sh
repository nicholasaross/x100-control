#!/usr/bin/env bash
# Deploys the optional Alexa (AWS) half of x100-control and writes the bridge's settings.
#
#   alexa/infra/deploy.sh amzn1.ask.skill.xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
#
# Creates (in eu-west-1 by default, which is where English (UK) Alexa add-ons run):
#   * an RSA key + CSR for the bridge -- the private key never leaves this machine
#   * an IoT certificate issued from that CSR, the IoT thing and a narrow policy
#   * the Alexa Smart Home Lambda, its role and the Alexa-only invoke permission
#   * certs/ (key, cert, Amazon root CA) and bridge.env for the bridge
#
# Safe to re-run: it reuses the existing key/certificate and updates the stack.
# Env overrides: AWS_PROFILE, AWS_REGION, STACK_NAME, THING_NAME, FRIENDLY_NAME,
#                ALLOWED_USER_ID, ARTIFACT_BUCKET, X100_HOST
set -euo pipefail

SKILL_ID="${1:-${SKILL_ID:-}}"
if [[ ! "$SKILL_ID" =~ ^amzn1\.ask\.skill\. ]]; then
  echo "usage: $0 <alexa-skill-id>   (Alexa developer console > your skill > Smart Home > Your Skill ID)" >&2
  exit 2
fi

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-west-1}}"
STACK="${STACK_NAME:-x100-alexa}"
THING="${THING_NAME:-x100-projector}"
FRIENDLY="${FRIENDLY_NAME:-Projector}"
ALEXA_DIR="$(cd "$(dirname "$0")/.." && pwd)"   # alexa/
ROOT="$(cd "$ALEXA_DIR/.." && pwd)"              # the project folder: certs/ and bridge.env go here
CERTS="$ROOT/certs"
ENV_FILE="$ROOT/bridge.env"
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"

for tool in aws openssl curl; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing required tool: $tool" >&2; exit 1; }
done

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
echo "==> Account $ACCOUNT, region $REGION, stack $STACK, thing $THING"

# ---------------------------------------------------------------- certificate
mkdir -p "$CERTS"
chmod 700 "$CERTS"
if [[ ! -f "$CERTS/bridge.key" ]]; then
  echo "==> Generating the bridge key and CSR"
  openssl req -new -newkey rsa:2048 -nodes -keyout "$CERTS/bridge.key" -out "$CERTS/bridge.csr" \
    -subj "/CN=$THING" 2>/dev/null
  chmod 600 "$CERTS/bridge.key"
fi
if [[ ! -s "$CERTS/certificate-arn" ]]; then
  echo "==> Asking AWS IoT to issue a certificate for the CSR"
  aws iot create-certificate-from-csr --set-as-active \
    --certificate-signing-request "file://$CERTS/bridge.csr" \
    --query certificateArn --output text > "$CERTS/certificate-arn"
fi
CERT_ARN="$(cat "$CERTS/certificate-arn")"
CERT_ID="${CERT_ARN##*/}"
aws iot describe-certificate --certificate-id "$CERT_ID" \
  --query certificateDescription.certificatePem --output text > "$CERTS/bridge.crt"
curl -fsSL https://www.amazontrust.com/repository/AmazonRootCA1.pem -o "$CERTS/AmazonRootCA1.pem"

IOT_ENDPOINT="$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --query endpointAddress --output text)"
echo "==> IoT endpoint $IOT_ENDPOINT"

# ---------------------------------------------------------------- lambda package bucket
BUCKET="${ARTIFACT_BUCKET:-x100-alexa-$ACCOUNT-$REGION}"
if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  echo "==> Creating artifact bucket $BUCKET"
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$BUCKET" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  fi
  aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi

# ---------------------------------------------------------------- stack
find "$ALEXA_DIR/lambda" -name __pycache__ -prune -exec rm -rf {} +
echo "==> Packaging and deploying CloudFormation stack"
aws cloudformation package --template-file "$ALEXA_DIR/infra/template.yaml" \
  --s3-bucket "$BUCKET" --s3-prefix "$STACK" --output-template-file "$CERTS/packaged.yaml" >/dev/null
aws cloudformation deploy --template-file "$CERTS/packaged.yaml" --stack-name "$STACK" \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
  --parameter-overrides \
    SkillId="$SKILL_ID" ThingName="$THING" BridgeCertificateArn="$CERT_ARN" \
    IotDataEndpoint="$IOT_ENDPOINT" FriendlyName="$FRIENDLY" AllowedUserId="${ALLOWED_USER_ID:-}"
LAMBDA_ARN="$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='LambdaArn'].OutputValue" --output text)"

# ---------------------------------------------------------------- bridge.env
[[ -f "$ENV_FILE" ]] || cp "$ROOT/deploy/bridge.env.example" "$ENV_FILE"
set_env() {  # set_env KEY VALUE -- replace or append, portable across GNU/BSD tools
  local key="$1" value="$2"
  if grep -q "^$key=" "$ENV_FILE"; then
    awk -v k="$key" -v v="$value" 'index($0, k"=") == 1 { print k "=" v; next } { print }' \
      "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
}
set_env THING_NAME "$THING"
set_env AWS_IOT_ENDPOINT "$IOT_ENDPOINT"
set_env CERT_FILE "certs/bridge.crt"
set_env KEY_FILE "certs/bridge.key"
set_env CA_FILE "certs/AmazonRootCA1.pem"
if [[ -n "${X100_HOST:-}" ]]; then set_env X100_HOST "$X100_HOST"; fi
chmod 600 "$ENV_FILE"

cat <<EOF

Done.

  Lambda ARN (Alexa console > Smart Home > Default endpoint, and tick "Europe, India"):
    $LAMBDA_ARN

  Bridge settings: $ENV_FILE
  Bridge certificate and key: $CERTS/

Next: move certs/ and the AWS lines of this bridge.env to the bridge. On the Pi,
add the lines to its own bridge.env and run \`sudo deploy/install.sh --alexa\`
(alexa/README.md, step 4). Check with \`x100-bridge --check\`, then link the
skill in the Alexa app.
EOF
