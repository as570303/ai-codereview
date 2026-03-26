# 预期发现：SEC-002 硬编码凭证
import boto3
import requests

# 危险：硬编码 API Key
API_KEY = "sk-prod-abcdef1234567890abcdef1234567890"
DB_PASSWORD = "SuperSecret123!"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def call_api(endpoint: str):
    headers = {"Authorization": f"Bearer {API_KEY}"}
    return requests.get(endpoint, headers=headers)


def connect_db():
    return {"host": "db.internal", "password": DB_PASSWORD}
