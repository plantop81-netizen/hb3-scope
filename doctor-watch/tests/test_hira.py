"""심평원 API 모듈: 응답 파싱·필터 완화·CSV 병합 (실제 호출 없음, 2026-09 실응답 형태 사용)."""
import httpx

from doctor_watch import db as D
from doctor_watch import hira

PNUH = {"addr": "부산광역시 서구 구덕로 179", "clCd": "01", "clCdNm": "상급종합", "drTotCnt": 508, "sidoCd": 210000, "sidoCdNm": "부산",
        "sgguCd": 210006, "sgguCdNm": "부산서구", "telno": "240-7000", "yadmNm": "부산대학교병원", "ykiho": "JDQ4MTAx"}
YANGSAN = {"addr": "경상남도 양산시", "clCd": "01", "clCdNm": "상급종합", "drTotCnt": 470, "sidoCd": 380000, "sidoCdNm": "경남",
           "hospUrl": "http://www.pnuyh.co.kr/content/", "yadmNm": "양산부산대학교병원", "ykiho": "JDQ4MTVy"}
BSM = {"clCd": "11", "clCdNm": "종합병원", "drTotCnt": 120, "sidoCd": 210000, "sidoCdNm": "부산", "hospUrl": "www.bsm.or.kr", "yadmNm": "부산성모병원", "ykiho": "JDQ4MTBz"}
CLINIC = {"clCd": "31", "clCdNm": "의원", "drTotCnt": 1, "sidoCd": 210000, "sidoCdNm": "부산", "yadmNm": "동네의원", "ykiho": "JDQ4MTCc"}


def _payload(items, total):
    return {"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                         "body": {"items": {"item": items} if items else "", "numOfRows": 500, "pageNo": 1, "totalCount": total}}}


_RealClient = httpx.Client


def _mock(handler):
    return httpx.MockTransport(handler)


def _patch_client(monkeypatch, handler):
    monkeypatch.setattr(hira.httpx, "Client", lambda **kw: _RealClient(transport=_mock(handler)))


def test_iter_hospitals_relaxes_filters_and_filters_client_side(monkeypatch):
    calls = []

    def handler(req: httpx.Request):
        calls.append(dict(req.url.params))
        if "sidoCd" in req.url.params and "clCd" not in req.url.params:
            return httpx.Response(200, json=_payload([PNUH, BSM, CLINIC], 3))
        return httpx.Response(200, json=_payload([], 0))

    _patch_client(monkeypatch, handler)
    rows = list(hira.iter_hospitals(sido="부산", cl_codes=["상급종합", "종합병원"], service_key="k"))
    assert [r["name"] for r in rows] == ["부산대학교병원", "부산성모병원"]
    assert rows[0]["url"] is None and rows[1]["url"] == "http://www.bsm.or.kr"
    assert rows[0]["cl_name"] == "상급종합" and rows[0]["dr_tot_cnt"] == 508
    assert all("clCd" not in c for c in calls)


def test_iter_hospitals_falls_back_to_no_filter(monkeypatch):
    def handler(req: httpx.Request):
        if "sidoCd" in req.url.params:
            return httpx.Response(200, json=_payload([], 0))
        return httpx.Response(200, json=_payload([PNUH, YANGSAN], 2))

    _patch_client(monkeypatch, handler)
    rows = list(hira.iter_hospitals(sido="부산", service_key="k"))
    assert [r["name"] for r in rows] == ["부산대학교병원"]


def test_encoded_key_is_decoded():
    assert hira._service_key("abc%2Bdef%3D%3D") == "abc+def=="
    assert hira._service_key(" plainkey ") == "plainkey"


def test_csv_row_merges_with_hira_row(tmp_path):
    with D.session(tmp_path / "t.db") as conn:
        seed_id = D.upsert_hospital(conn, {"name": "부산대학교병원", "url": "https://www.pnuh.or.kr", "sido": "부산"})
        hira_id = D.upsert_hospital(conn, hira.normalize_row(PNUH))
        assert seed_id == hira_id
        row = D.get_hospital(conn, seed_id)
        assert row["url"] == "https://www.pnuh.or.kr" and row["ykiho"] == "JDQ4MTAx" and row["dr_tot_cnt"] == 508
        # 반대 순서: HIRA 먼저, CSV 나중 → URL 이 채워진다
        D.upsert_hospital(conn, hira.normalize_row(YANGSAN))
        D.upsert_hospital(conn, {"name": "양산부산대학교병원", "url": "https://www.pnuyh.or.kr", "sido": "경남"})
        assert conn.execute("SELECT COUNT(*) FROM hospitals").fetchone()[0] == 2
        assert conn.execute("SELECT url FROM hospitals WHERE name='양산부산대학교병원'").fetchone()[0] == "https://www.pnuyh.or.kr"


def test_unknown_sido_raises():
    import pytest

    with pytest.raises(hira.HiraError):
        list(hira.iter_hospitals(sido="화성", service_key="k"))


def test_csv_does_not_override_hira_classification(tmp_path):
    with D.session(tmp_path / "t.db") as conn:
        D.upsert_hospital(conn, hira.normalize_row(dict(PNUH, clCd="01", clCdNm="상급종합")))
        D.upsert_hospital(conn, {"name": "부산대학교병원", "url": "https://www.pnuh.or.kr", "sido": "부산", "cl_name": "종합병원"})
        row = conn.execute("SELECT cl_name, url FROM hospitals WHERE name='부산대학교병원'").fetchone()
        assert row["cl_name"] == "상급종합" and row["url"] == "https://www.pnuh.or.kr"
