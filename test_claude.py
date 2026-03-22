import json, sys
import boto3
from botocore.exceptions import BotoCoreError, ClientError

print("== test_claude.py starting ==")

# Use the default AWS CLI profile/region from aws configure
session = boto3.Session()
region = session.region_name or "us-east-1"  # fallback if region not set
print("Resolved region:", region)

# Verify credentials by calling STS
try:
    sts = session.client("sts", region_name=region)
    ident = sts.get_caller_identity()
    print("STS Identity OK:", ident["Account"], ident["Arn"])
except Exception as e:
    print("STS failed (credentials/region).", e)
    sys.exit(1)

rt = session.client("bedrock-runtime", region_name=region)

body = {
    "anthropic_version": "bedrock-2023-05-31",
    "max_tokens": 50,
    "messages": [
        {"role": "user",
         "content": [{"type": "text", "text": "Hello Claude, say hi to our hackathon team!"}]}
    ],
}

try:
    resp = rt.invoke_model(
        modelId="anthropic.claude-3-5-sonnet-20240620-v1:0",
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
except (BotoCoreError, ClientError) as e:
    print("invoke_model failed:", e)
    sys.exit(1)

raw = resp["body"].read()
print("RAW (first 300 bytes):", raw[:300])
try:
    data = json.loads(raw)
    print("CLAUDE SAYS:", data["content"][0]["text"])
except Exception as e:
    print("Could not parse model response:", e, "\nFull body:", raw)

print("== done ==")
