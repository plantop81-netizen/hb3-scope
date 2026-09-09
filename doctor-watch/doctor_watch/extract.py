"""페이지 텍스트 → 의료진 목록 [{name, department, position, specialty}] 추출.

1순위: Claude 구조화 출력 (정확도 높음, 사이트 형식에 무관)
2순위: 규칙 기반 추출 (API 키가 없거나 호출 실패 시)
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .config import settings
from .names import clean_name, is_plausible_korean_name, name_key, norm_department

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """당신은 한국 병원 홈페이지의 '의료진 소개' 페이지 텍스트에서 의사 명단을 추출하는 도구입니다.

규칙
- 실제 진료 의사(의사, 전문의, 교수, 원장, 과장 등)만 추출합니다. 간호사, 약사, 행정직, 연구원, 물리치료사, 영양사 등은 제외합니다.
- 이름은 직함/호칭을 뺀 순수 이름만 적습니다 (예: "김철수 교수" → "김철수"). 한자·영문 병기는 제거합니다.
- department 는 페이지에 표시된 진료과(예: 소화기내과, 정형외과). 없으면 null.
- position 은 직위(교수, 임상교수, 과장, 원장, 전문의, 전임의 등). 없으면 null.
- specialty 는 세부 전문분야/진료분야 한 줄 요약. 없으면 null.
- 같은 사람이 여러 번 나오면 한 번만 적습니다.
- 페이지에 의사 명단이 전혀 없으면 빈 배열을 반환합니다.
- 추측하지 말고 텍스트에 있는 정보만 사용합니다.
"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "doctors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "department": {"type": ["string", "null"]},
                    "position": {"type": ["string", "null"]},
                    "specialty": {"type": ["string", "null"]},
                },
                "required": ["name", "department", "position", "specialty"],
                "additionalProperties": False,
            },
        },
        "is_staff_page": {"type": "boolean", "description": "이 페이지가 의료진 소개 페이지인지"},
    },
    "required": ["doctors", "is_staff_page"],
    "additionalProperties": False,
}

_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(max_retries=3)
    return _client


def _chunks(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    out, buf = [], []
    cur = 0
    for line in text.splitlines():
        if cur + len(line) + 1 > size and buf:
            out.append("\n".join(buf))
            buf, cur = [], 0
        buf.append(line)
        cur += len(line) + 1
    if buf:
        out.append("\n".join(buf))
    return out


def extract_with_llm(text: str, hospital_name: str, url: str, model: str | None = None) -> tuple[list[dict[str, Any]], bool]:
    """(doctors, is_staff_page). 실패 시 예외."""
    model = model or settings.model
    client = _get_client()
    doctors: list[dict[str, Any]] = []
    is_staff = False
    # 한 요청에 너무 긴 텍스트를 넣지 않도록 분할 (약 40k 자 ≈ 25k 토큰)
    for chunk in _chunks(text[: settings.max_chars_per_page], 40_000):
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"병원: {hospital_name}\nURL: {url}\n\n<page_text>\n{chunk}\n</page_text>",
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        )
        if not model.startswith("claude-haiku"):
            # 단순 추출 작업이므로 사고 깊이는 낮게 (Haiku 4.5 는 effort 미지원)
            kwargs["output_config"]["effort"] = "low"
        resp = client.messages.create(**kwargs)
        if resp.stop_reason == "refusal":
            raise RuntimeError("model refused")
        raw = next(b.text for b in resp.content if b.type == "text")
        data = json.loads(raw)
        is_staff = is_staff or bool(data.get("is_staff_page"))
        doctors.extend(data.get("doctors") or [])
    return normalize_doctors(doctors, url), is_staff


# ── 규칙 기반 폴백 ─────────────────────────────────────────────────────
POSITION_WORDS = r"(교수|명예교수|임상교수|조교수|부교수|진료교수|촉탁의|전문의|전임의|임상강사|원장|부원장|병원장|과장|진료과장|부장|진료부장|센터장|소장|실장|주임교수|대표원장|의무원장|진료원장)"
SEP = r"[\s|:：·,]*"
NAME_THEN_TITLE = re.compile(r"(?<![가-힣])([가-힣]{2,4})" + SEP + r"(?:\(.*?\))?" + SEP + POSITION_WORDS + r"(?![가-힣])")
TITLE_THEN_NAME = re.compile(r"(?<![가-힣])" + POSITION_WORDS + SEP + r"([가-힣]{2,4})(?![가-힣])")
NON_DOCTOR_PREFIX = {"간호", "원무", "행정", "약제", "영양", "총무", "시설", "기획", "홍보", "사무", "관리", "경영", "전산", "물리", "작업", "방사선", "임상", "병리", "재무", "인사", "보안", "고객"}
DEPT_LINE = re.compile(r"([가-힣]{0,10}(?:내과|외과|의학과|산부인과|소아청소년과|안과|피부과|비뇨의학과|비뇨기과|이비인후과|치과|한방과|신경과|병리과|핵의학과|가정의학과|정신건강의학과|재활의학과|영상의학과|응급의학과|마취통증의학과|진단검사의학과|방사선종양학과|직업환경의학과|성형외과|정형외과|신경외과|흉부외과|심장혈관흉부외과))")
STOP_NAMES = {"진료과", "의료진", "전문의", "센터", "클리닉", "병원", "소개", "안내", "예약", "홈페이지", "바로가기", "더보기", "자세히", "검색", "전체", "교수진", "진료", "협진", "외래", "입원", "선택"}


def _plausible(nm: str) -> bool:
    if nm in STOP_NAMES or nm in NON_DOCTOR_PREFIX or not is_plausible_korean_name(nm):
        return False
    if nm.endswith(("과", "실", "팀", "센터", "병원", "의원")) or DEPT_LINE.fullmatch(nm):
        return False
    return True


def extract_heuristic(text: str, url: str) -> list[dict[str, Any]]:
    doctors: dict[str, dict[str, Any]] = {}
    current_dept: str | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = DEPT_LINE.search(line)
        if m and len(line) <= 30:
            current_dept = m.group(1)
        found: list[tuple[str, str]] = []
        if "|" in line:
            # 표 행: [진료과 | 이름 | 직위 | 분야] 형태. 직위 셀의 바로 옆 셀을 이름으로 본다.
            cells = [c.strip() for c in line.split("|") if c.strip()]
            for i, c in enumerate(cells):
                if re.fullmatch(POSITION_WORDS, c):
                    for j in (i - 1, i + 1):
                        if 0 <= j < len(cells) and _plausible(cells[j]):
                            found.append((cells[j], c))
                            break
                else:
                    for nm, pos in NAME_THEN_TITLE.findall(c):
                        found.append((nm, pos))
            if not found:
                # 직위 셀이 없는 표: 진료과 셀 다음의 이름 셀
                for i, c in enumerate(cells):
                    if DEPT_LINE.fullmatch(c) and i + 1 < len(cells) and _plausible(cells[i + 1]):
                        found.append((cells[i + 1], ""))
                        current_dept = c
        else:
            for nm, pos in NAME_THEN_TITLE.findall(line):
                found.append((nm, pos))
            for pos, nm in TITLE_THEN_NAME.findall(line):
                found.append((nm, pos))
        for nm, pos in found:
            if not _plausible(nm):
                continue
            k = name_key(nm)
            if k not in doctors:
                doctors[k] = {"name": nm, "department": current_dept, "position": pos or None, "specialty": None}
    return normalize_doctors(list(doctors.values()), url)


def normalize_doctors(rows: list[dict[str, Any]], url: str) -> list[dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        nm = clean_name(str(r.get("name") or ""))
        if not nm or len(nm) > 20:
            continue
        if re.fullmatch(r"[가-힣]+", nm) and not is_plausible_korean_name(nm):
            continue
        dept = norm_department(r.get("department"))
        key = (name_key(nm), dept or "")
        if key in out:
            continue
        out[key] = {
            "name": nm,
            "name_key": name_key(nm),
            "department": dept,
            "position": (r.get("position") or None),
            "specialty": (r.get("specialty") or None),
            "source_url": url,
        }
    return list(out.values())


def extract(text: str, hospital_name: str, url: str, prefer_llm: bool = True) -> tuple[list[dict[str, Any]], str]:
    """(doctors, method). method ∈ llm | heuristic."""
    if prefer_llm and settings.llm_enabled:
        try:
            doctors, _ = extract_with_llm(text, hospital_name, url)
            return doctors, "llm"
        except Exception as e:  # noqa: BLE001
            log.warning("LLM 추출 실패(%s) → 규칙 기반으로 대체: %s", url, e)
    return extract_heuristic(text, url), "heuristic"
