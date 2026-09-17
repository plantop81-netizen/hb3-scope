"""Claude 호출부를 모의 클라이언트로 검증 (실제 API 호출 없음)."""
import json
from types import SimpleNamespace

from doctor_watch import extract


class _FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        assert kw["model"]
        assert kw["output_config"]["format"]["type"] == "json_schema"
        payload = {"is_staff_page": True, "doctors": [
            {"name": "김민수 교수", "department": "소화기내과", "position": "교수", "specialty": "내시경"},
            {"name": "김민수", "department": "소화기내과 ", "position": "교수", "specialty": None},  # 중복
            {"name": "한지민", "department": None, "position": "간호부장", "specialty": None},
        ]}
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))])


def test_extract_with_llm_parses_structured_output(monkeypatch):
    fake = SimpleNamespace(messages=_FakeMessages())
    monkeypatch.setattr(extract, "_get_client", lambda: fake)
    doctors, is_staff = extract.extract_with_llm("소화기내과 김민수 교수", "테스트병원", "http://x/dr", model="claude-opus-5")
    assert is_staff
    names = [(d["name"], d["department"]) for d in doctors]
    assert ("김민수", "소화기내과") in names
    assert len([n for n, _ in names if n == "김민수"]) == 1
    assert fake.messages.calls[0]["output_config"]["effort"] == "low"


def test_haiku_has_no_effort(monkeypatch):
    fake = SimpleNamespace(messages=_FakeMessages())
    monkeypatch.setattr(extract, "_get_client", lambda: fake)
    extract.extract_with_llm("텍스트", "병원", "http://x", model="claude-haiku-4-5")
    assert "effort" not in fake.messages.calls[0]["output_config"]


def test_extract_falls_back_to_heuristic_when_llm_fails(monkeypatch):
    monkeypatch.setattr(extract.settings, "anthropic_api_key", "dummy")

    def boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(extract, "extract_with_llm", boom)
    doctors, method = extract.extract("정형외과\n박준호 과장", "병원", "http://x")
    assert method == "heuristic" and doctors[0]["name"] == "박준호"


def test_llm_pick_staff_links(monkeypatch):
    from doctor_watch import discover

    html = """<html><body>
      <a href="/notice">공지사항</a><a href="/about/greeting">인사말</a>
      <a href="/m/team.do">진료팀</a><a href="/recruit">채용</a></body></html>"""

    class _M:
        def create(self, **kw):
            listing = kw["messages"][0]["content"]
            idx = [int(l.split("\t")[0]) for l in listing.splitlines() if "\t" in l and "team.do" in l]
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps({"indices": idx}))])

    monkeypatch.setattr(extract, "_get_client", lambda: SimpleNamespace(messages=_M()))
    urls = discover.llm_pick_staff_links("테스트병원", "http://h.test/", html)
    assert urls == ["http://h.test/m/team.do"]


def test_credit_exhaustion_disables_llm_for_run(monkeypatch):
    import anthropic
    import httpx as _hx

    monkeypatch.setattr(extract, "LLM_DISABLED_REASON", None)
    monkeypatch.setattr(extract.settings, "anthropic_api_key", "dummy")
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        req = _hx.Request("POST", "https://api.anthropic.com/v1/messages")
        resp = _hx.Response(400, request=req, json={"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low"}})
        raise anthropic.BadRequestError("Your credit balance is too low", response=resp, body=None)

    monkeypatch.setattr(extract, "extract_with_llm", boom)
    d1, m1 = extract.extract("정형외과\n박준호 과장", "병원", "http://x")
    d2, m2 = extract.extract("정형외과\n박준호 과장", "병원", "http://y")
    assert m1 == m2 == "heuristic" and calls["n"] == 1  # 두 번째부터는 호출 자체를 안 함
    assert "크레딧" in extract.LLM_DISABLED_REASON
    monkeypatch.setattr(extract, "LLM_DISABLED_REASON", None)
