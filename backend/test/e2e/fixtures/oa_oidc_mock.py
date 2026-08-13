"""仅用于本地 OA S0 验收的最小身份服务。"""

import os
import secrets
import time
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from jwt.algorithms import RSAAlgorithm

ISSUER = os.environ.get("MOCK_OIDC_ISSUER", "http://localhost:9001").rstrip("/")
BROWSER_ORIGIN = os.environ.get("MOCK_OIDC_BROWSER_ORIGIN", ISSUER).rstrip("/")
CLIENT_ID = "oa-s0-local-client"
CLIENT_SECRET = "oa-s0-local-secret"
SUBJECT = "oa-s0-user"
OA_COMPANY_CODE = "TEST"
OA_TOKEN_SECRET = "oa-s0-local-token-secret-32-bytes"
OA_PARENT_ORIGIN = os.environ.get("MOCK_OA_PARENT_ORIGIN", "http://localhost:4173").rstrip("/")

app = FastAPI(title="OA S0 Mock Identity Provider")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[OA_PARENT_ORIGIN],
    allow_methods=["GET"],
    allow_headers=["*"],
)
private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
jwk.update({"kid": "oa-s0-local-key", "use": "sig", "alg": "RS256"})
authorization_codes: dict[str, dict] = {}
access_tokens: set[str] = set()
oa_tokens: set[str] = set()


@app.get("/mock-oa-token")
async def issue_mock_oa_token() -> dict[str, str]:
    """签发本地自定义 OA 双 JWT token。"""
    issued_at = int(time.time())
    claims = {"iat": issued_at, "exp": issued_at + 300, "data": {"account": SUBJECT}}
    first = jwt.encode(claims, OA_TOKEN_SECRET, algorithm="HS256")
    second = jwt.encode(claims, OA_TOKEN_SECRET, algorithm="HS256")
    token = f"{first}|{second}"
    oa_tokens.add(token)
    return {"token": token}


@app.get("/oa-api/User/GetUserInfo")
async def get_oa_userinfo(Account: str, authorization: str | None = Header(default=None)) -> dict:
    """校验完整 OA token，并返回模拟的在职用户信息。"""
    token = authorization.removeprefix("Bearer ") if authorization else ""
    if token not in oa_tokens or Account != SUBJECT:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid OA token")
    return {
        "status": 1,
        "message": "",
        "data": {
            "companyCode": OA_COMPANY_CODE,
            "account": SUBJECT,
            "fullName": "OA S0 模拟用户",
            "userStateCode": "service",
            "userJobInformationDtos": [
                {
                    "pagingSort": 1,
                    "appointmentDepartmentCode": "mock-department",
                    "appointmentDepartmentName": "模拟研发部",
                }
            ],
        },
    }


@app.get("/.well-known/openid-configuration")
async def get_discovery() -> dict:
    """返回本地模拟 Provider 的 discovery 元数据。"""
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{BROWSER_ORIGIN}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "userinfo_endpoint": f"{ISSUER}/userinfo",
        "jwks_uri": f"{ISSUER}/jwks",
        "id_token_signing_alg_values_supported": ["RS256"],
    }


@app.get("/jwks")
async def get_jwks() -> dict:
    """返回模拟 Provider 的当前签名公钥。"""
    return {"keys": [jwk]}


@app.get("/authorize")
async def authorize(
    client_id: str,
    redirect_uri: str,
    state: str,
    nonce: str,
    response_type: str = "code",
) -> RedirectResponse:
    """直接批准本地模拟用户并返回授权码。"""
    if client_id != CLIENT_ID or response_type != "code":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid authorization request")

    code = secrets.token_urlsafe(24)
    authorization_codes[code] = {
        "nonce": nonce,
        "redirect_uri": redirect_uri,
        "expires_at": time.time() + 60,
    }
    return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}")


@app.post("/token")
async def exchange_token(
    grant_type: str = Form(),
    code: str = Form(),
    redirect_uri: str = Form(),
    client_id: str = Form(),
    client_secret: str = Form(),
) -> dict:
    """一次性消费授权码并签发可验证的 id_token。"""
    request_data = authorization_codes.pop(code, None)
    if (
        grant_type != "authorization_code"
        or client_id != CLIENT_ID
        or client_secret != CLIENT_SECRET
        or not request_data
        or request_data["redirect_uri"] != redirect_uri
        or request_data["expires_at"] <= time.time()
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid grant")

    issued_at = int(time.time())
    access_token = secrets.token_urlsafe(24)
    access_tokens.add(access_token)
    id_token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": SUBJECT,
            "iat": issued_at,
            "exp": issued_at + 300,
            "nonce": request_data["nonce"],
            "preferred_username": "oa_s0_user",
            "name": "OA S0 模拟用户",
            "department": "模拟研发部",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": jwk["kid"]},
    )
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 300,
        "id_token": id_token,
    }


@app.get("/userinfo")
async def get_userinfo(authorization: str | None = Header(default=None)) -> dict:
    """返回与 id_token 主体一致的模拟用户信息。"""
    token = authorization.removeprefix("Bearer ") if authorization else ""
    if token not in access_tokens:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid access token")
    return {
        "sub": SUBJECT,
        "preferred_username": "oa_s0_user",
        "name": "OA S0 模拟用户",
        "department": "模拟研发部",
    }
