"""이름/진료과 정규화 유틸리티."""
from __future__ import annotations

import re
import unicodedata

_HANGUL = re.compile(r"[가-힣]")
_STRIP = re.compile(r"[\s·\.\-_/()\[\]{}'\"“”‘’,]+")

# 이름 뒤에 흔히 붙는 직함/호칭. 이름 자체가 아니므로 제거한다.
TITLE_WORDS = [
    "교수", "명예교수", "석좌교수", "임상교수", "조교수", "부교수", "정교수", "겸임교수", "외래교수", "임상조교수", "임상부교수",
    "진료교수", "촉탁의", "전문의", "전공의", "레지던트", "인턴", "임상강사", "펠로우", "전임의", "원장", "부원장", "병원장",
    "의료원장", "과장", "진료과장", "부장", "진료부장", "센터장", "소장", "실장", "주임교수", "부과장", "의무원장", "진료원장",
    "대표원장", "총괄원장", "의학박사", "박사", "선생님", "선생", "의사", "님", "M.D.", "MD", "PhD", "Ph.D",
]
_TITLE_RE = re.compile("(" + "|".join(re.escape(w) for w in sorted(TITLE_WORDS, key=len, reverse=True)) + r")\s*$")


def clean_name(raw: str) -> str:
    """'김철수 교수', '김 철 수', '김철수(金哲洙)' → '김철수'."""
    s = unicodedata.normalize("NFKC", raw or "").strip()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)  # 괄호 안(한자, 영문 등) 제거
    s = re.sub(r"[A-Za-z].*$", "", s).strip() if _HANGUL.search(s) else s  # 한글 이름 뒤 영문/학위 표기 제거
    for _ in range(3):
        s2 = _TITLE_RE.sub("", s).strip()
        if s2 == s:
            break
        s = s2
    s = re.sub(r"\s+", "", s) if _HANGUL.search(s) else re.sub(r"\s+", " ", s)
    return s.strip(" ·,.-")


def name_key(raw: str) -> str:
    return _STRIP.sub("", clean_name(raw)).lower()


def is_plausible_korean_name(s: str) -> bool:
    s = clean_name(s)
    return bool(re.fullmatch(r"[가-힣]{2,4}", s))


DEPT_ALIASES = {
    "내과": "내과",
    "소화기내과": "소화기내과",
    "순환기내과": "심장내과",
    "심장내과": "심장내과",
    "심혈관내과": "심장내과",
    "호흡기내과": "호흡기내과",
    "호흡기알레르기내과": "호흡기내과",
    "신장내과": "신장내과",
    "내분비내과": "내분비내과",
    "내분비대사내과": "내분비내과",
    "혈액종양내과": "혈액종양내과",
    "종양내과": "혈액종양내과",
    "감염내과": "감염내과",
    "류마티스내과": "류마티스내과",
    "외과": "외과",
    "일반외과": "외과",
    "정형외과": "정형외과",
    "신경외과": "신경외과",
    "흉부외과": "심장혈관흉부외과",
    "심장혈관흉부외과": "심장혈관흉부외과",
    "성형외과": "성형외과",
    "산부인과": "산부인과",
    "소아청소년과": "소아청소년과",
    "소아과": "소아청소년과",
    "정신건강의학과": "정신건강의학과",
    "정신과": "정신건강의학과",
    "신경과": "신경과",
    "재활의학과": "재활의학과",
    "영상의학과": "영상의학과",
    "마취통증의학과": "마취통증의학과",
    "마취과": "마취통증의학과",
    "응급의학과": "응급의학과",
    "가정의학과": "가정의학과",
    "피부과": "피부과",
    "비뇨의학과": "비뇨의학과",
    "비뇨기과": "비뇨의학과",
    "안과": "안과",
    "이비인후과": "이비인후과",
    "진단검사의학과": "진단검사의학과",
    "병리과": "병리과",
    "핵의학과": "핵의학과",
    "방사선종양학과": "방사선종양학과",
    "직업환경의학과": "직업환경의학과",
    "치과": "치과",
    "한방내과": "한방내과",
}


def norm_department(raw: str | None) -> str | None:
    if not raw:
        return None
    s = unicodedata.normalize("NFKC", raw).strip()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"(진료과|센터|클리닉|교실)$", "", s) if len(s) > 3 else s
    return DEPT_ALIASES.get(s, s) or None
