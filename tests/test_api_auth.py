"""入口密码（AB_UI_PASSWORD）：留空 = 谁都能直接进；填上 = 先输密码，接口没会话回 401。"""

from test_api_read import _client

from audiobook.api.auth import COOKIE_NAME, session_token

PASSWORD = "开门暗号"


def _locked(settings):
    """同一份 data_dir，只是打开入口密码。"""
    return settings.model_copy(update={"ui_password": PASSWORD})


def test_without_password_everyone_gets_in(settings):
    client = _client(settings)
    assert client.get("/").status_code == 200
    assert 'id="logout"' in client.get("/").text  # 按钮在，但默认 hidden，由前端按会话放出来
    assert client.get("/api/books").status_code == 200
    assert client.get("/api/session").json() == {"auth_required": False, "authenticated": True}
    assert client.get("/login").status_code == 200  # 密码没开登录页也能打开（脚本会送人回首页）


def test_with_password_page_and_api_are_gated(settings):
    client = _client(_locked(settings))
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert client.get("/api/books").status_code == 401
    assert client.get("/api/session").json() == {"auth_required": True, "authenticated": False}
    # 登录页与静态资源不能被挡：不然登录框自己都打不开
    login = client.get("/login")
    assert login.status_code == 200
    assert 'id="gate-password"' in login.text and "/static/js/gate.js" in login.text
    assert client.get("/static/js/gate.js").status_code == 200
    assert client.get("/static/theme.css").status_code == 200
    # 非 /api 的路径（含 OpenAPI 文档）也是先跳登录页，不直接报 401
    assert client.get("/docs", follow_redirects=False).status_code == 303


def test_login_unlocks_page_and_api(settings):
    client = _client(_locked(settings))
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/api/books").status_code == 200
    assert client.get("/api/session").json() == {"auth_required": True, "authenticated": True}


def test_wrong_password_is_rejected(settings):
    client = _client(_locked(settings))
    assert client.post("/api/login", json={"password": "猜的"}).status_code == 401
    assert client.post("/api/login", json={}).status_code == 401
    assert client.get("/api/books").status_code == 401  # 猜错之后还是没有会话


def test_cookie_from_another_password_is_invalid(settings):
    """会话 = 密码派生的 HMAC：换个密码，旧 cookie 立刻作废。"""
    client = _client(_locked(settings))
    stale = {"cookie": f"{COOKIE_NAME}={session_token('上一版密码')}"}
    assert client.get("/api/books", headers=stale).status_code == 401
    # 对得上的 token 则直接放行（会话不落库，cookie 本身就是凭证）
    fresh = {"cookie": f"{COOKIE_NAME}={session_token(PASSWORD)}"}
    assert client.get("/api/books", headers=fresh).status_code == 200


def test_logout_drops_the_session(settings):
    client = _client(_locked(settings))
    client.post("/api/login", json={"password": PASSWORD})
    assert client.get("/api/books").status_code == 200
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/books").status_code == 401


def test_login_is_rate_limited_by_sleep(settings, monkeypatch):
    """猜错要慢半拍（穷举不划算），但只慢在失败上。"""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("audiobook.api.app.asyncio.sleep", fake_sleep)
    client = _client(_locked(settings))
    client.post("/api/login", json={"password": "猜的"})
    assert slept == [0.5]
    slept.clear()
    client.post("/api/login", json={"password": PASSWORD})
    assert slept == []
