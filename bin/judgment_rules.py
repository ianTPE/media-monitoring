"""AI 判讀後保留客戶公司新聞的共同規則。"""

from urllib.parse import unquote, urlsplit

from fetch import core_of, mentions


def is_search_page(url):
    """站內搜尋結果頁即使標題含公司名，也不是客戶新聞。"""
    path = unquote(urlsplit(url).path).lower()
    return any(part in {"search", "搜尋"} for part in path.split("/"))


def keep_company_title(cfg, title, url):
    """沿用 fetch 的公司名比對，並遵守客戶的前綴設定。"""
    return (not is_search_page(url)
            and mentions(title, core_of(cfg),
                         prefix=cfg.get("allow_core_prefix", True)))
