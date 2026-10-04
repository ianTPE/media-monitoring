# 共用函式：讀設定、抓 Google News、還原真實網址、抓記者名
import json, os, re, ssl, time, html as htmllib, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
TPE = ZoneInfo("Asia/Taipei")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# 不是記者名的字樣（多半是媒體自己掛名）
SECTION_TAIL = re.compile(
    r"\s*[-–—]\s*(產業|日報|新聞|要聞|財經|證券|生技|頭條|焦點|即時|科技|國際|"
    r"政治|生活|健康|房產|理財|影音|專題|社會|兩岸|評論)\s*$")

NOT_A_PERSON = ("報", "網", "社", "編輯", "新聞", "中心", "綜合", "記者", "電子",
                "台", "媒體", "雜誌", "時報", "日報", "頻道", "財經", "產經",
                "生技", "月刊", "周刊", "週刊", "通訊", "整理", "提供", "編譯")


def _merge(base, extra):
    """defaults.yaml 打底，客戶檔的 list 用疊加、其餘直接覆寫。"""
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = out[k] + [x for x in v if x not in out[k]]
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


def load_clients(only=None):
    base = {}
    dp = ROOT / "defaults.yaml"
    if dp.exists():
        base = yaml.safe_load(dp.read_text(encoding="utf-8")) or {}
    lp = ROOT / "defaults.local.yaml"      # 只留本機的設定（Excel 路徑、客戶名稱對照）
    if lp.exists():
        base = _merge(base, yaml.safe_load(lp.read_text(encoding="utf-8")) or {})
    out = []
    for p in sorted((ROOT / "clients").glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        cfg = _merge(base, yaml.safe_load(p.read_text(encoding="utf-8")) or {})
        cfg.setdefault("id", p.stem)
        if only and only not in (cfg["id"], cfg.get("name")):
            continue
        out.append(cfg)
    if only and not out:
        raise SystemExit(f"找不到客戶設定：{only}（請看 clients/ 目錄）")
    return out


# 有些政府／媒體網站憑證少了 Subject Key Identifier，Python 3.13 嚴格模式會擋；
# 放寬這一項但仍完整驗證憑證鏈（curl 的行為）。
_LOOSE = ssl.create_default_context()
_LOOSE.verify_flags &= ~ssl.VERIFY_X509_STRICT


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "zh-TW,zh;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode(r.headers.get_content_charset() or "utf-8", "ignore")
    except urllib.error.URLError as e:
        if not isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            raise
        with urllib.request.urlopen(req, timeout=timeout, context=_LOOSE) as r:
            return r.read().decode(r.headers.get_content_charset() or "utf-8", "ignore")


def clean_title(title, source=""):
    """去掉 Google News 標題尾端的媒體名與網站分類（「… | 產業熱點 | 產業」）。"""
    title = htmllib.unescape(title).strip()
    if source and title.endswith(f" - {source}"):
        title = title[: -len(source) - 3].strip()
    for _ in range(3):
        new = re.sub(r"\s*[|｜]\s*[^|｜]{1,14}\s*$", "", title)
        if new == title or not new:
            break
        title = new
    return re.sub(SECTION_TAIL, "", title).strip()


def google_news(query, days):
    """回傳 Google News 搜尋結果（標題／媒體／時間／轉址連結）。"""
    q = urllib.parse.quote(f"{query} when:{max(days, 1) + 1}d")
    url = (f"https://news.google.com/rss/search?q={q}"
           "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    items = []
    for it in ET.fromstring(get(url)).findall("./channel/item"):
        title = (it.findtext("title") or "").strip()
        source = (it.findtext("source") or "").strip()
        try:
            when = parsedate_to_datetime(it.findtext("pubDate")).astimezone(TPE)
        except Exception:
            when = datetime.now(TPE)
        items.append({"title": clean_title(title, source), "source": source,
                      "when": when, "gurl": (it.findtext("link") or "").strip()})
    return items


# ---- Google News 轉址還原（需要文章頁上的 id / ts / 簽章再打一次 API）----
_BATCH = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


def resolve_url(gurl, cache):
    if cache.get(gurl, gurl) != gurl:   # 舊版會把失敗也寫進快取，這種當作沒快取
        return cache[gurl]
    real = gurl
    try:
        page = get(gurl)
        aid = re.search(r'data-n-a-id="([^"]+)"', page).group(1)
        sig = re.search(r'data-n-a-sg="([^"]+)"', page).group(1)
        ts = int(re.search(r'data-n-a-ts="(\d+)"', page).group(1))
        inner = json.dumps(["garturlreq",
                            [["X", "X", ["X", "X"], None, None, 1, 1, "US:en",
                              None, 1, None, None, None, None, None, 0, 1],
                             "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                            aid, ts, sig])
        body = urllib.parse.urlencode(
            {"f.req": json.dumps([[["Fbv4je", inner, None, "generic"]]])}).encode()
        req = urllib.request.Request(_BATCH, data=body, headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read().decode("utf-8", "ignore")
        m = re.search(r'\[\\"garturlres\\",\\"(.+?)\\"', raw)
        if m:
            real = m.group(1).encode().decode("unicode_escape")
            real = real.replace("\\u003d", "=").replace("\\u0026", "&")
    except Exception:
        pass
    if real != gurl:      # 失敗（多半是被 Google 暫時擋）不寫快取，下一輪再試
        cache[gurl] = real
    return real


def clean_url(url):
    """拿掉追蹤參數，讓連結乾淨一點。"""
    try:
        u = urllib.parse.urlsplit(url)
        # 鉅亨網列印版 /news/print/<id>?embed=1 → 一般新聞頁 /news/id/<id>
        m = re.fullmatch(r"/news/print/(\d+)", u.path)
        if u.netloc.endswith("cnyes.com") and m:
            return f"https://news.cnyes.com/news/id/{m.group(1)}"
        keep = [(k, v) for k, v in urllib.parse.parse_qsl(u.query)
                if not k.lower().startswith(("utm_", "fbclid", "gclid"))
                and k.lower() not in ("from", "dark_mode", "ref")]
        return urllib.parse.urlunsplit(
            (u.scheme, u.netloc, u.path, urllib.parse.urlencode(keep), ""))
    except Exception:
        return url


_AMP_PATH = re.compile(r"/amp(?=/|$)|\.amp(?=$|\.html?$)", re.I)


def url_key(url):
    """同一篇報導的比對鍵：忽略 http/https、www/m/amp 子網域、AMP 版路徑，
    TradingView 各語系子網域，以及台視 ?i= 與路徑兩種寫法。只用來去重，不當連結。"""
    try:
        u = urllib.parse.urlsplit(url)
    except Exception:
        return url
    host = u.netloc.lower()
    host = re.sub(r"^(?:www|m|amp|mobile)\.", "", host)
    if host.endswith(".tradingview.com"):
        host = "tradingview.com"
    path = _AMP_PATH.sub("", u.path).rstrip("/")
    if host.endswith("cnyes.com"):
        path = re.sub(r"^/news/print/(\d+)$", r"/news/id/\1", path)
        host = "news.cnyes.com" if path.startswith("/news/id/") else host
    query = [(k, v) for k, v in urllib.parse.parse_qsl(u.query)
             if k.lower() not in ("amp", "outputtype", "embed")]
    if host == "ttv.com.tw":
        ids = [v for k, v in query if k == "i"]
        if ids:
            path, query = f"{path}/{ids[0]}", []
        path = re.sub(r"^(/\w+/view/[0-9A-F]+)/\d+$", r"\1", path, flags=re.I)
    return f"{host}{path}?{urllib.parse.urlencode(sorted(query))}".rstrip("?")


def url_rank(url):
    """同一篇有多個網址時挑哪個當連結：越小越好。避開 AMP／手機版、非中文語系、?i= 寫法。"""
    u = urllib.parse.urlsplit(url)
    host = u.netloc.lower()
    return (bool(_AMP_PATH.search(u.path) or re.match(r"^(?:amp|m|mobile)\.", host)),
            host.endswith(".tradingview.com") and not host.startswith("tw."),
            bool(u.query), u.scheme != "https", len(url))


def pretty_source(source, url, media_names):
    """Google News 常給網域或英文名，換成報告要用的媒體名。"""
    try:
        host = urllib.parse.urlsplit(url).netloc.lower()
    except Exception:
        host = ""
    host = host[4:] if host.startswith("www.") else host
    for key in sorted(media_names or {}, key=len, reverse=True):
        k = key.lower()
        if host == k or host.endswith("." + k):
            return media_names[key]
    for key in sorted(media_names or {}, key=len, reverse=True):
        if key.lower() in (source or "").lower():
            return media_names[key]
    return source


def looks_like_person(name):
    name = (name or "").strip()
    if not (2 <= len(name) <= 5):
        return False
    return not any(bad in name for bad in NOT_A_PERSON)


def _clean_name(name, source=""):
    name = re.sub(r"[\s　]+", "", htmllib.unescape(name or ""))
    name = re.sub(r"(攝影?|文|報導|整理|編譯|即時)$", "", name)
    name = re.sub(r"[的與和及暨、擔]$", "", name)
    if "的" in name:
        return ""
    if name[:1] in ("會", "們", "群", "站"):
        return ""
    if not looks_like_person(name):
        return ""
    if source and (name in source or source in name):
        return ""
    return name


def fetch_page(url):
    try:
        return get(url, timeout=20)
    except Exception:
        return ""


def plain_text(page, limit=40000):
    """把網頁轉成純文字，用來判斷內文有沒有提到某家公司。"""
    page = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    text = htmllib.unescape(re.sub(r"(?s)<[^>]+>", " ", page))
    return re.sub(r"\s+", " ", text)[:limit]


ARTICLE_EXTRACT_VER = "p4"   # 抓正文的方式改版時要跟著改，內文比對快取才會失效


def _drop_card_links(html):
    """移除「整張卡片是一個連結」的推薦區塊（<a> 裡包著 img/div/p），
    但保留正文裡的行內連結。"""
    out, pos = [], 0
    for m in re.finditer(r"(?is)<a\b[^>]*>(.*?)</a>", html):
        if re.search(r"(?i)<(img|div|p|h[1-6])\b", m.group(1)):
            out.append(html[pos:m.start()])
            pos = m.end()
    out.append(html[pos:])
    return "".join(out)


def article_text(page, limit=40000):
    """只取文章正文，避免比對到側欄的「相關新聞」而誤判。

    優先用 JSON-LD 的 articleBody，沒有就取所有 <p> 段落；段落太少（可能不是
    文章頁或用了別的標記）才退回整頁純文字。
    """
    m = re.search(r'"articleBody"\s*:\s*("(?:[^"\\]|\\.)*")', page, re.S)
    if m:
        try:
            return json.loads(m.group(1))[:limit]
        except Exception:
            pass
    page_nb = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", page)
    page_nb = _drop_card_links(page_nb)
    strip = lambda x: re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", x))).strip()
    paras = []
    for chunk in re.findall(r"(?is)<p[^>]*>(.*?)</p>", page_nb):
        text = strip(chunk)
        if not text:
            continue
        # 「推薦新聞」「延伸閱讀」那種整串都是連結的段落不算正文
        linked = sum(len(strip(a)) for a in re.findall(r"(?is)<a[^>]*>(.*?)</a>", chunk))
        if linked / len(text) > 0.6:
            continue
        paras.append(text)
    text = " ".join(paras)
    for marker in ("延伸閱讀", "推薦閱讀", "更多新聞", "相關新聞", "熱門新聞", "看更多"):
        i = text.find(marker)
        if i > 200:
            text = text[:i]
    return text[:limit] if len(text) >= 200 else plain_text(page, limit)


def reporter_of(url, source=""):
    """best effort 抓記者名；抓不到回空字串，報告裡再人工補。"""
    return reporter_from_page(fetch_page(url), source)


def reporter_from_page(page, source=""):
    """以內文署名（記者XXX／…報導）為主：部分媒體的 JSON-LD 作者欄位有缺字，
    例如 udn 會寫成「謝柏」，內文才是正確的「謝柏宏」。"""
    if not page:
        return ""

    byline = ""
    for pat in (r"[〔（(]\s*記者([\u4e00-\u9fff]{2,4})\s*[／/]",
                r"記者([\u4e00-\u9fff]{2,4})\s*[／/]",
                r"[〔（(]\s*記者([\u4e00-\u9fff]{2,4})",
                r"記者([\u4e00-\u9fff]{2,4})\s*[\u4e00-\u9fff]{0,4}報導",
                r"(?:撰文|採訪撰文|圖文)\s*[／/｜|:：]\s*([\u4e00-\u9fff]{2,4})"):
        m = re.search(pat, page)
        if m and (name := _clean_name(m.group(1), source)):
            byline = name
            break

    tagged = ""
    for blob in re.findall(r'"author"\s*:\s*[\[{](.{0,400}?)[}\]]', page, re.S):
        m = re.search(r'"name"\s*:\s*"([^"]{1,20})"', blob)
        if m and (name := _clean_name(m.group(1), source)):
            tagged = name
            break

    if byline and tagged and tagged != byline and tagged not in byline:
        return byline if len(byline) >= len(tagged) else tagged
    if byline or tagged:
        return byline or tagged

    m = re.search(r'<meta[^>]+name="author"[^>]+content="([^"]{1,20})"', page)
    return _clean_name(m.group(1), source) if m else ""


def pmap(fn, items, workers=6):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


def state_path(name):
    d = ROOT / ".state"
    d.mkdir(exist_ok=True)
    return d / name


def load_state(name, default):
    p = state_path(name)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_state(name, data):
    state_path(name).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                               encoding="utf-8")


# ---- 台灣行事曆：用人事行政總處「政府行政機關辦公日曆表」判斷上班日 ----
CAL_API = "https://data.gov.tw/api/v2/rest/dataset/14718"


def _download_calendar(year):
    """回傳 {'2026-09-21': False(上班) / True(放假)}；抓不到回 None。"""
    roc = year - 1911
    try:
        meta = json.loads(get(CAL_API, timeout=25))
        url = None
        for r in meta["result"].get("distribution", []):
            desc = r.get("resourceDescription", "")
            if desc.startswith(f"{roc}年") and "Google" not in desc:
                url = r.get("resourceDownloadUrl")      # 後面的是更新版，取最後一個
        if not url:
            return None
        table = {}
        for line in get(url, timeout=30).lstrip("﻿").splitlines()[1:]:
            c = [x.strip().strip('"') for x in line.split(",")]
            if len(c) < 3 or len(c[0]) != 8 or not c[0].isdigit():
                continue
            table[f"{c[0][:4]}-{c[0][4:6]}-{c[0][6:]}"] = c[2] != "0"
        return table or None
    except Exception:
        return None


def calendar_for(year):
    name = f"calendar-{year}.json"
    table = load_state(name, None)
    if table is None:
        table = _download_calendar(year)
        if table:
            save_state(name, table)
    return table or {}


def is_holiday(d, cfg=None):
    """d 是 date。順序：客戶設定 > 官方日曆 > 週末規則。"""
    cfg = cfg or {}
    key = d.isoformat()
    if key in (cfg.get("holidays") or []):
        return True
    if key in (cfg.get("extra_workdays") or []):
        return False
    table = calendar_for(d.year)
    if key in table:
        return table[key]
    return d.weekday() >= 5


def previous_workday(d, cfg=None):
    """d 之前最近的一個上班日（跳過週末與國定假日）。"""
    for _ in range(30):
        d -= timedelta(days=1)
        if not is_holiday(d, cfg):
            return d
    return d


WEEK_ZH = "一二三四五六日"


def zh_day(d):
    return f"{d:%m/%d}（{WEEK_ZH[d.weekday()]}）"


# ---- 檔案路徑 ----
DEFAULT_PATHS = {"candidates": "{client}/{ym}/{date}-{client}.md",
                 "reports": "{client}/{ym}/{date}-{client}.md"}


def out_path(kind, cfg, day, make_dir=False):
    """kind 是 'candidates' 或 'reports'。"""
    tpl = ((cfg.get("paths") or {}).get(kind)) or DEFAULT_PATHS[kind]
    rel = tpl.format(client=cfg["id"], name=cfg.get("name", cfg["id"]),
                     date=f"{day:%Y-%m-%d}", ym=f"{day:%Y-%m}",
                     yyyy=f"{day:%Y}", mm=f"{day:%m}", dd=f"{day:%d}")
    path = ROOT / kind / rel
    if make_dir:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def find_existing(kind, cfg, day):
    """先找新版路徑，找不到再找舊的扁平檔名，舊檔才不會突然讀不到。"""
    path = out_path(kind, cfg, day)
    if path.exists():
        return path
    legacy = ROOT / kind / f"{day:%Y-%m-%d}-{cfg['id']}.md"
    return legacy if legacy.exists() else path
