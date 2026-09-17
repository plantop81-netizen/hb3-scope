"""건강보험심사평가원 공공데이터 API 연동.

- 병원정보서비스 getHospBasisList : 전국 요양기관 기본정보 (홈페이지 URL 포함)
- 의료기관별상세정보서비스 getSpcSbjtSdrInfo2.7 : 전문과목별 전문의 수

공공데이터포털(data.go.kr)에서 무료 활용신청 후 발급되는 서비스키가 필요하다.
필드명은 2024~2026 사양 기준이며 포털 명세가 바뀌면 FIELD_MAP 만 수정하면 된다.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import httpx

from .config import settings

log = logging.getLogger(__name__)

BASIS_URL = "https://apis.data.go.kr/B551182/hospInfoServicev2/getHospBasisList"
SPECIALIST_URL = "https://apis.data.go.kr/B551182/MadmDtlInfoService2.7/getSpcSbjtSdrInfo2.7"

# 시도코드 (심평원 병원정보서비스 실응답 기준, 2026-09 확인: 250000=대전, 210000=부산, 370000=경북, 380000=경남)
SIDO_CODES = {
    "서울": "110000", "부산": "210000", "인천": "220000", "대구": "230000", "광주": "240000", "대전": "250000",
    "울산": "260000", "경기": "310000", "강원": "320000", "충북": "330000", "충남": "340000", "전북": "350000",
    "전남": "360000", "경북": "370000", "경남": "380000", "제주": "390000", "세종": "410000",
}

# 종별코드 (실응답 기준: 01 상급종합, 11 종합병원, 21 병원, 28 요양병원, 29 정신병원, 31 의원 ...)
CL_CODES = {
    "상급종합": "01", "종합병원": "11", "병원": "21", "요양병원": "28", "정신병원": "29", "의원": "31",
    "치과병원": "41", "치과의원": "51", "조산원": "61", "보건기관": "71", "약국": "81", "한방병원": "91", "한의원": "92",
}
CL_NAMES = {v: k for k, v in CL_CODES.items()}

FIELD_MAP = {
    "ykiho": "ykiho",
    "name": "yadmNm",
    "cl_cd": "clCd",
    "cl_name": "clCdNm",
    "sido": "sidoCdNm",
    "sggu": "sgguCdNm",
    "addr": "addr",
    "tel": "telno",
    "url": "hospUrl",
    "dr_tot_cnt": "drTotCnt",
}


class HiraError(RuntimeError):
    pass


def _service_key(key: str) -> str:
    """포털의 'Encoding' 키(%2B, %3D 포함)를 붙여넣어도 동작하도록 디코딩된 형태로 통일한다.
    (httpx 가 params 를 다시 URL 인코딩하므로 Decoding 키를 넘겨야 한다.)"""
    key = key.strip()
    if "%" in key:
        from urllib.parse import unquote

        key = unquote(key)
    return key


def _items(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    resp = payload.get("response", {})
    header = resp.get("header", {})
    code = str(header.get("resultCode", ""))
    if code not in ("00", "0", ""):
        raise HiraError(f"HIRA API error {code}: {header.get('resultMsg')}")
    body = resp.get("body", {}) or {}
    items = body.get("items") or {}
    rows = items.get("item") if isinstance(items, dict) else items
    if rows is None:
        rows = []
    if isinstance(rows, dict):
        rows = [rows]
    return rows, int(body.get("totalCount") or 0)


# 공공데이터포털은 해외 IP(예: GitHub Actions 미국 러너) 접속을 막는 경우가 있어 연결 시간을 짧게 잡고 몇 번만 재시도한다.
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
RETRIES = 3


def _fetch_page(client: httpx.Client, params: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = client.get(BASIS_URL, params=params)
            r.raise_for_status()
            try:
                return _items(r.json())
            except ValueError as e:  # XML 오류응답 등
                raise HiraError(f"JSON 파싱 실패: {r.text[:300]}") from e
        except httpx.TransportError as e:  # 연결/읽기 시간초과, 프록시 거부 등
            last = e
            log.warning("HIRA 접속 실패 (%d/%d): %s", attempt + 1, RETRIES, e)
            time.sleep(2 * (attempt + 1))
    raise HiraError(
        "apis.data.go.kr 에 접속할 수 없습니다. 공공데이터포털은 해외 IP 를 차단하는 경우가 있으니 "
        "국내 PC 에서 `init-hospitals` 를 실행해 목록을 만들거나 CSV 로 등록하세요."
    ) from last


def iter_hospitals(
    sido: str | None = None,
    cl_codes: list[str] | None = None,
    page_size: int = 500,
    service_key: str | None = None,
    sleep: float = 0.2,
) -> Iterator[dict[str, Any]]:
    """지정 조건의 요양기관을 페이지 순회하며 내부 스키마 dict 로 변환해 yield.

    포털 API 가 지역+종별 조건을 함께 주면 0건을 돌려주는 경우가 있어(2026-09 확인),
    서버 필터는 지역까지만 걸고 종별은 응답을 받아 코드에서 거른다. 지역 필터마저 0건이면
    조건 없이 전체를 받아 코드에서 거른다.
    """
    key = _service_key(service_key or settings.hira_service_key)
    if not key:
        raise HiraError("HIRA_SERVICE_KEY 가 설정되지 않았습니다 (.env 참고)")
    want_cl = {CL_CODES.get(c, c) for c in cl_codes} if cl_codes else None
    if sido and sido not in SIDO_CODES and not str(sido).isdigit():
        raise HiraError(f"알 수 없는 시도명: {sido} (가능: {', '.join(SIDO_CODES)})")
    want_sido = SIDO_CODES.get(sido, sido) if sido else None
    base: dict[str, Any] = {"serviceKey": key, "numOfRows": page_size, "_type": "json"}

    with httpx.Client(timeout=TIMEOUT) as client:
        # 1차: 지역 필터. 2차: 조건 없음.
        attempts: list[dict[str, Any]] = []
        if want_sido:
            attempts.append({"sidoCd": want_sido})
        attempts.append({})
        for extra in attempts:
            rows, total = _fetch_page(client, {**base, **extra, "pageNo": 1})
            if total == 0 and extra:
                log.warning("HIRA: 조건 %s 로 0건 → 조건 완화", extra)
                continue
            log.info("HIRA: 조건 %s 총 %d건", extra or "없음", total)
            page = 1
            while True:
                if page > 1:
                    time.sleep(sleep)
                    rows, _ = _fetch_page(client, {**base, **extra, "pageNo": page})
                for row in rows:
                    h = normalize_row(row)
                    if want_sido and str(row.get("sidoCd") or "") != str(want_sido):
                        continue
                    if want_cl and str(h.get("cl_cd") or "") not in want_cl:
                        continue
                    yield h
                log.info("HIRA basis page %s (%s/%s)", page, min(page * page_size, total), total)
                if page * page_size >= total or not rows:
                    break
                page += 1
            return


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    h = {k: row.get(v) for k, v in FIELD_MAP.items()}
    h["url"] = normalize_url(h.get("url"))
    if h.get("cl_cd") and not h.get("cl_name"):
        h["cl_name"] = CL_NAMES.get(str(h["cl_cd"]))
    try:
        h["dr_tot_cnt"] = int(h["dr_tot_cnt"]) if h.get("dr_tot_cnt") not in (None, "") else None
    except (TypeError, ValueError):
        h["dr_tot_cnt"] = None
    return h


def normalize_url(u: str | None) -> str | None:
    if not u:
        return None
    u = str(u).strip()
    if not u or u in ("-", "없음", "http://", "https://"):
        return None
    if not u.lower().startswith(("http://", "https://")):
        u = "http://" + u
    return u


def specialist_counts(ykiho: str, service_key: str | None = None) -> dict[str, int]:
    """전문과목별 전문의 수 {과목명: 인원}."""
    key = _service_key(service_key or settings.hira_service_key)
    if not key:
        raise HiraError("HIRA_SERVICE_KEY 가 설정되지 않았습니다")
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.get(SPECIALIST_URL, params={"serviceKey": key, "ykiho": ykiho, "numOfRows": 100, "_type": "json"})
        r.raise_for_status()
        rows, _ = _items(r.json())
    out: dict[str, int] = {}
    for row in rows:
        nm = row.get("dgsbjtCdNm") or row.get("dgsbjtCd")
        try:
            out[str(nm)] = int(row.get("dgsbjtPrSdrCnt") or 0)
        except (TypeError, ValueError):
            continue
    return out
