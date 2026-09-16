"""병원 홈페이지에서 '의료진 소개' 페이지들을 자동으로 찾아낸다.

전략
1. 홈페이지의 모든 링크를 모아 텍스트/URL 키워드 점수로 후보를 고른다.
2. 후보 페이지 안에서 진료과별 의료진 하위 페이지(페이지네이션 포함)를 한 단계 더 따라간다.
3. 병원당 최대 페이지 수(설정)를 넘지 않는다.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

from bs4 import BeautifulSoup

from .config import settings

# (정규식, 점수)
TEXT_KEYWORDS: list[tuple[str, int]] = [
    (r"의료진\s*(소개|안내|검색|찾기|보기)", 10),
    (r"^의료진$", 10),
    (r"진료\s*의료진", 9),
    (r"의료진", 8),
    (r"의사\s*(소개|안내|찾기)", 8),
    (r"교수진|교수\s*소개", 7),
    (r"진료진|진료팀|의료팀", 7),
    (r"전문의\s*(소개|안내)", 7),
    (r"진료과\s*/?\s*의료진", 8),
    (r"(find|search)\s*(a\s*)?doctor", 7),
    (r"^doctors?$|medical\s*staff|our\s*doctors|physicians", 6),
    (r"진료과\s*(안내|소개)", 3),
    (r"원장\s*(소개|인사말)", 2),
]
URL_KEYWORDS: list[tuple[str, int]] = [
    (r"doctor", 6),
    (r"medical[_-]?staff|staff", 5),
    (r"physician|professor|faculty", 5),
    (r"uiryojin|euiryojin|dr[_/]|/dr\b|drlist|dr_list|doctorlist|docList|drSearch", 6),
    (r"(dept|department|clinic).*(doctor|staff|dr)", 6),
    (r"member|team", 2),
]
NEGATIVE = re.compile(r"(login|logout|join|signup|sitemap|recruit|채용|구인|모집|board|notice|공지|news|뉴스|event|이벤트|download|pdf$|jpg$|png$|zip$|mailto:|tel:|javascript:)", re.I)
DEPT_HINT = re.compile(r"(내과|외과|소아|산부인과|정형|신경|안과|피부|이비인후|비뇨|재활|영상|마취|응급|가정의학|정신|치과|한방|센터|클리닉|과$)")
PAGINATION = re.compile(r"(page|pageNo|pageIndex|pg|p)=\d+", re.I)


def _norm(url: str) -> str:
    parts = urlsplit(url)
    parts = parts._replace(fragment="")
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith(("utm_", "jsessionid"))]
    return urlunsplit(parts._replace(query=urlencode(q)))


def same_site(a: str, b: str) -> bool:
    ha, hb = urlsplit(a).netloc.lower(), urlsplit(b).netloc.lower()
    strip = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
    ha, hb = strip(ha), strip(hb)
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def score_link(text: str, href: str) -> int:
    t = re.sub(r"\s+", " ", text or "").strip()
    score = 0
    for pat, s in TEXT_KEYWORDS:
        if re.search(pat, t, re.I):
            score = max(score, s)
    for pat, s in URL_KEYWORDS:
        if re.search(pat, href, re.I):
            score = max(score, score + s if score else s)
    if NEGATIVE.search(href) or NEGATIVE.search(t):
        score -= 8
    return score


def candidate_links(base_url: str, html: str, min_score: int = 5) -> list[tuple[int, str, str]]:
    """(점수, url, 링크텍스트) 정렬 리스트."""
    soup = BeautifulSoup(html, "lxml")
    seen: dict[str, tuple[int, str]] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript", "mailto", "tel")):
            continue
        text = a.get_text(" ", strip=True) or a.get("title", "") or (a.img.get("alt", "") if a.img else "")
        url = _norm(urljoin(base_url, href))
        if not same_site(base_url, url):
            continue
        s = score_link(text, url)
        if s < min_score:
            continue
        if url not in seen or seen[url][0] < s:
            seen[url] = (s, text)
    out = [(s, u, t) for u, (s, t) in seen.items()]
    out.sort(key=lambda x: (-x[0], len(x[1])))
    return out


def department_links(page_url: str, html: str) -> list[str]:
    """의료진 목록 페이지 안의 진료과별/페이지네이션 하위 링크."""
    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []
    seen: set[str] = set()
    page_path = urlsplit(page_url).path
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript", "mailto", "tel")):
            continue
        url = _norm(urljoin(page_url, href))
        if not same_site(page_url, url) or url in seen:
            continue
        text = a.get_text(" ", strip=True)
        path = urlsplit(url).path
        is_dept = bool(DEPT_HINT.search(text)) and len(text) <= 20
        is_page = bool(PAGINATION.search(url)) and path == page_path
        same_family = path == page_path or path.startswith(page_path.rsplit("/", 1)[0] + "/")
        if (is_dept or is_page) and same_family and not NEGATIVE.search(url):
            seen.add(url)
            out.append(url)
    return out


def select_option_links(page_url: str, html: str) -> list[str]:
    """<select> 로 진료과를 고르는 사이트: option value 가 URL/코드일 때 후보 생성."""
    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []
    for sel in soup.find_all("select"):
        name = sel.get("name") or sel.get("id") or ""
        if not re.search(r"dept|dep|part|subject|과", name, re.I):
            continue
        for opt in sel.find_all("option"):
            val = (opt.get("value") or "").strip()
            if not val or val in ("0", "all", "전체", ""):
                continue
            if val.startswith(("http", "/")):
                out.append(_norm(urljoin(page_url, val)))
            else:
                parts = urlsplit(page_url)
                q = dict(parse_qsl(parts.query))
                q[name] = val
                out.append(urlunsplit(parts._replace(query=urlencode(q))))
    return out[: settings.max_pages_per_hospital]
