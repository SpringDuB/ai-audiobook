from test_api_read import _client


def test_root_serves_app_shell(settings):
    client = _client(settings)
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert '<meta name="color-scheme" content="dark">' in body
    assert "/static/theme.css" in body and "/static/js/app.js" in body
    assert 'id="main"' in body and 'id="masthead"' in body and 'id="rail"' in body
    assert "#/shelf" in body and "#/jobs" in body and "#/settings" in body
    assert "/static/favicon.svg" in body


def test_static_assets_are_offline_only(settings):
    client = _client(settings)
    for path in (
        "/static/theme.css",
        "/static/app.css",
        "/static/js/app.js",
        "/static/js/api.js",
        "/static/js/store.js",
        "/static/js/router.js",
        "/static/js/ui.js",
        "/static/js/format.js",
        "/static/favicon.svg",
    ):
        assert client.get(path).status_code == 200, path
    css = client.get("/static/theme.css").text + client.get("/static/app.css").text
    assert "http://" not in css and "https://" not in css      # 不依赖任何 CDN
    assert "Inter" not in css and "Roboto" not in css and "Arial" not in css
    assert "Noto Serif SC" in css and "Noto Sans SC" in css and "Cascadia Mono" in css


def test_view_modules_are_served(settings):
    client = _client(settings)
    for name in ("shelf", "workspace", "jobs", "issues", "voices", "settings"):
        assert client.get(f"/static/js/views/{name}.js").status_code == 200, name
    assert client.get("/static/js/voicepicker.js").status_code == 200


def test_unknown_static_path_is_404(settings):
    assert _client(settings).get("/static/js/nope.js").status_code == 404
