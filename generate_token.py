"""
generate_token.py – mint the HS256 JWT that goes into Intric's "Api Key" field.

Usage:
    python3 generate_token.py            # 1-year token
    python3 generate_token.py --days 30
"""

import argparse
import datetime
import os
import sys

import jwt
from dotenv import load_dotenv

load_dotenv()

parser = argparse.ArgumentParser()
parser.add_argument("--days", type=int, default=365, help="token lifetime in days (default 365)")
parser.add_argument("--sub", default="intric", help="subject claim (default 'intric')")
args = parser.parse_args()

secret = os.getenv("MCP_SERVER_JWT_SECRET", "")
issuer = os.getenv("MCP_SERVER_JWT_ISSUER", "intric-mcp")
audience = os.getenv("MCP_SERVER_JWT_AUDIENCE", "intric-client")

if len(secret) < 32:
    print("Error: MCP_SERVER_JWT_SECRET missing or shorter than 32 chars. Copy .env.example to .env first.")
    sys.exit(1)

now = datetime.datetime.now(datetime.timezone.utc)
payload = {
    "sub": args.sub,
    "iss": issuer,
    "aud": audience,
    "iat": now,
    "exp": now + datetime.timedelta(days=args.days),
}
token = jwt.encode(payload, secret, algorithm="HS256")

print(f"\n=== JWT for Intric (valid {args.days} days, iss={issuer}, aud={audience}) ===")
print(token)
print("\nIntric → Settings → MCP servers → Add:")
print("  URL:     https://<your-public-host>/mcp")
print("  Api Key: <token above>\n")
