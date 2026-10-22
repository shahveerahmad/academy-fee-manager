import os
import secrets

import bcrypt
from dotenv import load_dotenv
from fastapi import Header, HTTPException, Request

load_dotenv()

OWNER_PASSWORD_HASH = os.environ["OWNER_PASSWORD_HASH"].encode()
SESSION_SECRET = os.environ["SESSION_SECRET"]
API_KEY = os.environ["N8N_API_KEY"]


def check_password(password: str) -> bool:
    return bcrypt.checkpw(password.encode(), OWNER_PASSWORD_HASH)


def require_access(request: Request, x_api_key: str | None = Header(default=None)):
    # Door 1: the owner is logged in (session cookie)
    if request.session.get("owner"):
        return
    # Door 2: n8n sends the secret key
    if x_api_key and secrets.compare_digest(x_api_key, API_KEY):
        return
    raise HTTPException(status_code=401, detail="Not authorized")