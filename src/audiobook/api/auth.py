"""界面入口密码（AB_UI_PASSWORD）的闸门。

密码留空（默认）= 直通，和以前一样谁打开页面谁进；
密码非空 = 进编辑台先输密码：没会话的页面请求跳 /login，/api/* 回 401。

会话不落库：cookie 里存的是密码派生的 HMAC。改密码等于让所有已登录设备
立刻下线（旧 cookie 对不上新 token），服务端也不用存 session 表。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from http.cookies import SimpleCookie

COOKIE_NAME = "aiab_auth"
# 手机"添加到主屏幕"后经常几周不重输密码，给足 30 天；密码一改就全部失效
COOKIE_MAX_AGE = 60 * 60 * 24 * 30
_TOKEN_SALT = b"aiab-ui-session-v1"

# 不登录也能拿的：登录页、静态资源、登录/登出/会话状态、PWA 的两个根文件
PUBLIC_PATHS = frozenset(
    (
        "/login",
        "/api/login",
        "/api/logout",
        "/api/session",
        "/sw.js",
        "/manifest.webmanifest",
        "/favicon.ico",
    )
)
PUBLIC_PREFIXES = ("/static/",)


def session_token(password: str) -> str:
    """密码 → 会话 cookie 值（HMAC，单向；cookie 泄了也反推不出密码）。"""
    return hmac.new(str(password).encode("utf-8"), _TOKEN_SALT, hashlib.sha256).hexdigest()


def password_matches(given: str, expected: str) -> bool:
    """恒定时间比较；按 UTF-8 编码后比，中文密码也能用。"""
    return hmac.compare_digest(str(given).encode("utf-8"), str(expected).encode("utf-8"))


def token_ok(token: str, password: str) -> bool:
    return hmac.compare_digest(str(token).encode("utf-8"), session_token(password).encode("utf-8"))


def token_from_cookie_header(cookie_header: str) -> str:
    """Cookie 头 → 会话 token（没有 / 坏格式都当没登录）。"""
    if not cookie_header:
        return ""
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
    except Exception:  # noqa: BLE001 - 坏 cookie 不该 500
        return ""
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel is not None else ""


def authenticated(password: str, cookie_header: str) -> bool:
    """password 为空 = 不需要登录，恒真。"""
    if not password:
        return True
    return token_ok(token_from_cookie_header(cookie_header), password)


def _asgi_cookie(scope) -> str:
    for key, value in scope.get("headers") or ():
        if key == b"cookie":
            return value.decode("latin-1", "replace")
    return ""


class PasswordGate:
    """纯 ASGI 中间件：包住整个 app，不碰流式响应（SSE / 音频照旧）。"""

    def __init__(self, app, password: str) -> None:
        self.app = app
        self.password = str(password or "")

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self.password:
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or "/"
        if self._is_public(path) or authenticated(self.password, _asgi_cookie(scope)):
            await self.app(scope, receive, send)
            return
        if path == "/api" or path.startswith("/api/"):
            await self._deny_api(send)
            return
        await self._to_login(send)

    @staticmethod
    def _is_public(path: str) -> bool:
        return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)

    @staticmethod
    async def _deny_api(send) -> None:
        body = json.dumps({"detail": "需要访问密码：先打开页面输入密码再重试"}, ensure_ascii=False).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _to_login(send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 303,
                "headers": [(b"location", b"/login"), (b"cache-control", b"no-store")],
            }
        )
        await send({"type": "http.response.body", "body": b""})
