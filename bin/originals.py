"""轉載換原文：公司規定報告只能用原始出處網址（2026-10-04）。

Yahoo、富聯網、LINE TODAY、PChome 這類轉載站，以及鏡週刊 /external/ 轉三立的稿，
都要換成原始媒體的網址：

  0. Yahoo 的先看頁面上的 provider：Yahoo股市、Yahoo奇摩新聞自己記者寫的是原文，照用；
     鏡週刊 /external/setn_<編號> 直接換成三立同編號的網址

  1. 同一輪已抓到同標題的原文 → 用原文，丟掉轉載
  2. 沒有 → 拿標題去 Bing 新聞（直接給真實網址）、再試 Google News 找原文
  3. 都找不到 → 保留轉載，但預設不勾、註明「找不到原文」，由人工補網址

標題比對要很像才算（轉載常加《生醫股》、個股：、[經濟日報] 這類前後綴，先去掉）：
別家記者寫的同一件事標題也會相近，那是另一篇報導，不能拿來頂替。
找到的頁面若署名別家（東森財經會刊「財訊快報／記者…報導」），也是轉載，繼續找下一個。
"""

import html
import re
import urllib.parse
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

from common import fetch_page, get, google_news, plain_text, pretty_source, resolve_url


def _norm(title):
    return re.sub(r"[\s　,，、。：:！!？?「」『』（）()\[\]【】\-－—…·.／/|｜*＊△▲]+", "", title)


def title_core(title):
    t = (title or "").strip()
    t = re.sub(r"^(?:《[^》]{1,10}》|[^：:\s]{1,6}[：:])", "", t)     # 《生醫股》、個股：
    t = re.sub(r"\s*[\[【(（][^\]】)）]{2,8}[\]】)）]\s*$", "", t)    # [經濟日報]
    return _norm(t)


def same_story(a, b):
    a, b = title_core(a), title_core(b)
    if len(a) < 8 or len(b) < 8:
        return a == b and bool(a)
    if a in b or b in a:
        return True
    m = SequenceMatcher(None, a, b)
    return m.quick_ratio() >= 0.9 and m.ratio() >= 0.9


class Reprints:
    def __init__(self, cfg):
        self.syn = cfg.get("syndicated_sources") or []
        self.syn_urls = cfg.get("syndicated_urls") or []
        self.names = cfg.get("media_names")

    def display(self, source, url):
        return pretty_source(source, url, self.names)

    def is_reprint(self, source, url):
        shown = f"{self.display(source, url)} {source or ''} {url}".lower()
        return (any(s.lower() in shown for s in self.syn)
                or any(p in url for p in self.syn_urls))

    def domains_of(self, provider):
        """Yahoo 標的原始媒體名（「三立新聞網 setn.com」「FTNN新聞網」）→ 網域，給 site: 搜尋用。"""
        return [d for d, name in (self.names or {}).items() if provider and name in provider]

    def credited_elsewhere(self, url):
        """內文署名「某媒體／記者」，而某媒體是 media_names 裡別的網域：這頁也是轉載。
        只認得設定裡有的媒體名，免得「中國時報／記者」被當成不是中時。"""
        m = re.search(r"([^\s　｜|/／]{2,8})／記者", plain_text(fetch_page(url), 20000))
        if not m:
            return False
        host = urllib.parse.urlsplit(url).netloc.lower()
        homes = [d.lower() for d, name in (self.names or {}).items() if name == m.group(1)]
        return bool(homes) and not any(host == d or host.endswith("." + d) for d in homes)



def bing_news(query):
    x = get("https://www.bing.com/news/search?format=rss&setlang=zh-hant&cc=TW&mkt=zh-TW"
            "&qft=interval%3d%228%22&q=" + urllib.parse.quote(query), timeout=20)
    out = []
    for it in ET.fromstring(x).iter("item"):
        link = it.findtext("link") or ""
        real = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get("url", [link])[0]
        src = next((c.text for c in it if c.tag.endswith("Source")), "") or ""
        out.append({"title": html.unescape(it.findtext("title") or "").strip(),
                    "url": real, "source": src.strip()})
    return out


def yahoo_provider(url):
    """Yahoo 頁面 JSON-LD 的 provider 名稱（FTNN新聞網、Yahoo股市…）；抓不到回空字串。"""
    page = fetch_page(url)
    m = re.search(r'"provider":\{.{0,400}?"name":"([^"]+)"', page, re.S)
    return m.group(1) if m else ""


def search_original(title, rp, urlcache, provider=""):
    """回傳 (原文網址, 媒體名, 原文標題) 或 None。provider 是 Yahoo 標的原始媒體，先在它網站裡找。"""
    try:
        sited = [r for d in rp.domains_of(provider)[:2] for r in bing_news(f"{title} site:{d}")]
        for r in sited + bing_news(title):
            if same_story(title, r["title"]) and not rp.is_reprint(r["source"], r["url"]):
                shown = rp.display(r["source"], r["url"])
                if not rp.credited_elsewhere(r["url"]):
                    return r["url"], shown, r["title"]
    except Exception:
        pass
    try:
        for r in google_news(title, 14):
            if same_story(title, r["title"]) and not rp.is_reprint(r["source"], ""):
                real = resolve_url(r["gurl"], urlcache)
                shown = rp.display(r["source"], real)
                if (real != r["gurl"] and not rp.is_reprint(r["source"], real)
                        and not rp.credited_elsewhere(real)):
                    return real, shown, r["title"]
    except Exception:
        pass
    return None


def replace_reprints(items, picked, cfg0, urlcache, cache, workers, today):
    """把各家候選（picked）裡的轉載換成原文；找不到的標 no_original。
    cache 是 .state/originals.json：轉載網址 → 找到的原文（只記找到的，沒找到下一輪再試）。
    回傳 (換成原文, 丟掉重複, 找不到) 的則數。"""
    from common import pmap, url_key
    rp = Reprints(cfg0)
    for it in items:
        it["reprint"] = rp.is_reprint(it["source"], it["url"])
    targets = {id(it): it for rows in picked.values() for it in rows if it["reprint"]}
    yahoo = [it for it in targets.values() if "yahoo.com" in it["url"]]
    for it, name in zip(yahoo, pmap(lambda it: yahoo_provider(it["url"]), yahoo, workers)):
        if name.startswith("Yahoo"):
            it.update(reprint=False, source_name=name)
            del targets[id(it)]
        else:
            it["provider"] = name
    if not targets:
        return 0, 0, 0
    pool = [it for it in items if not it["reprint"]]
    by_core = {title_core(it["title"]): it for it in pool}

    def in_pool(r):
        hit = by_core.get(title_core(r["title"]))
        return hit or next((it for it in pool if same_story(r["title"], it["title"])), None)

    found = {}
    online = []
    for k, r in targets.items():
        hit = in_pool(r)
        setn = re.search(r"mirrormedia\.mg/external/setn_(\d+)", r["url"])
        if setn and not hit:
            found[k] = {**r, "url": f"https://www.setn.com/news/{setn.group(1)}",
                        "source": "三立新聞網", "reprint": False}
        elif hit:
            found[k] = hit
        elif url_key(r["url"]) in cache:
            c = cache[url_key(r["url"])]
            found[k] = {**r, "url": c["u"], "source": c["s"], "title": c.get("t") or r["title"],
                        "reprint": False}
        else:
            online.append(k)
    for k, res in zip(online, pmap(lambda k: search_original(
            targets[k]["title"], rp, urlcache, targets[k].get("provider", "")), online, min(workers, 4))):
        if res:
            r = targets[k]
            cache[url_key(r["url"])] = {"u": res[0], "s": res[1], "t": res[2],
                                        "d": f"{today:%Y-%m-%d}"}
            found[k] = {**r, "url": res[0], "source": res[1], "title": res[2], "reprint": False}

    swapped = dupes = missing = 0
    for cid, rows in picked.items():
        out, keys = [], {url_key(it["url"]) for it in rows if not it["reprint"]}
        for it in rows:
            orig = found.get(id(it)) if it["reprint"] else None
            if it["reprint"] and orig is None:
                it["no_original"] = True
                missing += 1
            elif orig is not None:
                # 轉載帶進來的客戶、搜尋標籤要跟著原文走，關聯度判斷才會一樣
                orig["clients"] = orig["clients"] | it["clients"]
                orig["base_clients"] = orig["base_clients"] | it["base_clients"]
                for c, labels in it["book_hits"].items():
                    orig["book_hits"][c] = orig["book_hits"].get(c, set()) | labels
                if url_key(orig["url"]) in keys:
                    dupes += 1
                    continue
                keys.add(url_key(orig["url"]))
                swapped += 1
                it = {**orig, "via": rp.display(it["source"], it["url"])}
            out.append(it)
        rows[:] = out
    return swapped, dupes, missing
