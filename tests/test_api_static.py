import re

from test_api_read import _client


def test_root_serves_app_shell(settings):
    client = _client(settings)
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert '<meta name="color-scheme" content="light dark">' in body
    assert "/static/theme.css" in body and "/static/js/app.js" in body
    assert 'id="main"' in body and 'id="masthead"' in body and 'id="rail"' in body
    assert "#/shelf" in body and "#/jobs" in body and "#/settings" in body
    assert "/static/logo-mark.png" in body
    # 双主题：防闪烁脚本要在样式表之前落主题，顶栏要有切换按钮
    assert 'id="theme-toggle"' in body
    assert 'localStorage.getItem("aiab-theme")' in body
    assert body.index("aiab-theme") < body.index("/static/theme.css")


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
        "/static/js/icons.js",
        "/static/js/format.js",
        "/static/logo-mark.png",
        "/static/favicon.png",
    ):
        assert client.get(path).status_code == 200, path
    css = client.get("/static/theme.css").text + client.get("/static/app.css").text
    assert "http://" not in css and "https://" not in css      # 不依赖任何 CDN
    assert "Inter" not in css and "Roboto" not in css and "Arial" not in css
    assert "Noto Serif SC" in css and "Noto Sans SC" in css and "Cascadia Mono" in css

    theme = client.get("/static/theme.css").text
    assert ":root[data-theme=\"dark\"]" in theme           # 纸 / 墨两套 token 都在
    assert "--accent:" in theme and "--danger:" in theme
    # 颜色只在 theme.css 的 token 层出现，页面样式里不许写裸色值
    assert re.search(r"#[0-9a-fA-F]{3,8}\b", client.get("/static/app.css").text) is None

    # 图标是内联的 SVG，不走 CDN / 雪碧图
    icons = client.get("/static/js/icons.js").text
    assert "fetch(" not in icons and "url(" not in icons and "<img" not in icons
    assert "createElementNS" in icons and "currentColor" in icons and "viewBox" in icons
    assert "Lucide" in icons                     # 保留上游版权声明
    assert "hourglass" in icons                  # 逐句合成中的沙漏

    app_css = client.get("/static/app.css").text
    assert "aiab-spin" in app_css                # 沙漏转圈动画
    assert "prefers-reduced-motion" in app_css    # 动效可关


def test_view_modules_are_served(settings):
    client = _client(settings)
    for name in ("shelf", "workspace", "jobs", "issues", "voices", "settings"):
        assert client.get(f"/static/js/views/{name}.js").status_code == 200, name
    assert client.get("/static/js/voicepicker.js").status_code == 200


def test_unknown_static_path_is_404(settings):
    assert _client(settings).get("/static/js/nope.js").status_code == 404
