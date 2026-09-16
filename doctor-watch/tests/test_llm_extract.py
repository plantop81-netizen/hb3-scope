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
