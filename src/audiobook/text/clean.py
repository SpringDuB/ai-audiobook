"""导入清洗：把网页来源书稿里的广告、导航、HTML 残渣先剔掉。

txt 书稿大多是从网页上抓的，正文里混着站点水印、章节导航、"求收藏/求月票"、
HTML 标签与实体。清洗只做"明显不是正文"的删除，凡是拿不准的一律保留：
误删正文比漏删广告严重得多。清洗发生在分章之前，落盘统计写进 chapters.json
（clean_stats / dropped_lines），出了问题能对账。
"""

import html
import re
from dataclasses import dataclass, field

from .split_chapters import match_chapter_title

ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u2060\u00ad"), None)

# HTML 残渣：抓取站经常把 <p>、<br/>、&nbsp; 直接留在 txt 里
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
HTML_BREAK = re.compile(r"<br\s*/?>", re.I)
HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9]{0,15}(?:\s[^<>]{0,200})?/?>")
HTML_ENTITY_ONLY = re.compile(r"^(?:&nbsp;|&emsp;|&ensp;|&#160;|&#12288;|\s)+$", re.I)
WHITESPACE_RUN = re.compile(r"[ \t\xa0\u3000]+")

# 网址/域名水印：出现在句子中间也删掉（只删网址本身，正文一律保留）
DOMAIN = re.compile(
    r"(?:https?://)?(?:[a-z0-9-]+\.)+(?:com|net|org|cc|cn|info|top|xyz|me|tv|la|io|vip|club|site|online|app)"
    r"(?:/[A-Za-z0-9/._~%-]*)?",
    re.I,
)

# 行级噪声：整行本来就不是正文的（命中才删，且要求行足够短）
NOISE_PATTERNS = [
    re.compile(r"https?://\S+", re.I),
    re.compile(r"panlay", re.I),
    re.compile(r"浏览器访问"),
    re.compile(r"(更多|免费).{0,8}(网盘|资源|小说|电子书)"),
    re.compile(r"以下.{0,6}(小说网|读书网|看书)"),
    re.compile(r"^(笔趣阁|啃书小说网|顶点小说|无弹窗).*$"),
    re.compile(r"^[\s\-—_=*·.]{4,}$"),
    re.compile(r"^[\W_]{1,4}$"),
]

# 短行里的"网页垃圾"关键词：广告、导航、站点提示、作者求票。
# 分强弱两档：强关键词基本只出现在垃圾行里；弱关键词（月票/打赏/订阅）
# 只有在和"求/请/关注"这类动作词同时出现时才判垃圾，避免误删剧情行。
JUNK_HINTS = [
    re.compile(r"请记住本站|记住本站|收藏本站|本站域名|本书网址|永久域名|域名变更|防走丢|防止走丢|备份网址"),
    re.compile(r"最新章节|无弹窗|全文免费|免费阅读|手机阅读|手机用户请|手机版|电脑版|阅读模式|自动订阅|加入书架"),
    re.compile(r"扫码关注|关注公众号|微信公众号|加入书签|投推荐票"),
    re.compile(r"上一章|下一章|返回目录|章节目录|回到顶部|天才一秒记住|一秒记住|本章未完|未完待续|分节阅读"),
    re.compile(r"书友群|QQ群|qq群|读者群|交流群|进群|群号"),
    re.compile(r"txt下载|全集下载|电子书下载|小说下载|打包下载|网盘"),
]
WEAK_JUNK_HINTS = [re.compile(r"月票|订阅|收藏|打赏|推荐票|福利|活动")]
ACTION_HINTS = re.compile(r"求|请|欢迎|关注|加入|点击|投票|订阅|收藏|点赞|转发|支持|下章|更新")
JUNK_LINE_MAX_CHARS = 60

TITLE_SUFFIX = re.compile(
    r"[（(](求收藏|求推荐|求订阅|新书|加更|第[一二三四五六七八九十\d]+更|月票)[^）)]*[）)]\s*$"
)


@dataclass
class CleanResult:
    text: str
    dropped: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=lambda: {"dropped": 0, "kept": 0})


def clean_title(title: str) -> str:
    return TITLE_SUFFIX.sub("", title).strip()


def _is_noise(line: str) -> bool:
    if any(p.search(line) for p in NOISE_PATTERNS):
        return True
    if len(line) > JUNK_LINE_MAX_CHARS:
        return False
    if any(p.search(line) for p in JUNK_HINTS):
        return True
    # 弱关键词：只有像"求月票 / 订阅一下"这种动作句才判垃圾
    return bool(ACTION_HINTS.search(line) and any(p.search(line) for p in WEAK_JUNK_HINTS))


def _strip_markup(line: str) -> str:
    """去掉 HTML 标签/实体与域名水印；只处理"有嫌疑"的行，正文原样返回。"""
    if "<" in line or "&" in line:
        line = HTML_COMMENT.sub("", line)
        line = HTML_TAG.sub("", line)
        if "&" in line:
            line = html.unescape(line)
    if "." in line:
        line = DOMAIN.sub(" ", line)
    return WHITESPACE_RUN.sub(" ", line)


def clean_text(text: str) -> CleanResult:
    text = text.translate(ZERO_WIDTH)
    # 抓取站常把段落粘在一行里用 <br> 分隔：先还原成换行再逐行清洗
    text = HTML_COMMENT.sub("", text)
    text = HTML_BREAK.sub("\n", text)
    kept: list[str] = []
    dropped: list[str] = []
    last: str | None = None
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _strip_markup(raw).strip()
        if not line:
            continue
        if HTML_ENTITY_ONLY.match(line):
            continue
        is_title = match_chapter_title(line)
        if is_title:
            line = clean_title(line)   # "第三章 洗髓伐脉（新书求收藏）" → 去掉求票后缀，标题必须留
            if not line:
                continue
        if last is not None and line == last and len(line) >= 12:
            # 抓取站整段重复（同一段连着出现两次）不算正文；短句重复（"哈哈！"）保留
            dropped.append(raw.strip())
            continue
        if not is_title and _is_noise(line):
            dropped.append(raw.strip())
            continue
        kept.append(line)
        last = line
    return CleanResult(
        text="\n\n".join(kept),
        dropped=dropped,
        stats={"dropped": len(dropped), "kept": len(kept)},
    )
