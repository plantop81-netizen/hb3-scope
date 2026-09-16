from doctor_watch.diff import diff_rosters
from doctor_watch.names import clean_name, name_key, norm_department


def test_clean_name_strips_titles():
    assert clean_name("김철수 교수") == "김철수"
    assert clean_name("김 철 수") == "김철수"
    assert clean_name("이영희(李英姬) 임상교수") == "이영희"
    assert clean_name("박준호 M.D., Ph.D") == "박준호"
    assert name_key("최 수진 전문의") == "최수진"


def test_norm_department_aliases():
    assert norm_department("순환기내과") == "심장내과"
    assert norm_department("소아과") == "소아청소년과"
    assert norm_department("소화기내과 진료과") == "소화기내과"


def _r(name, dept=None, pos=None):
    return {"name": name, "name_key": name_key(name), "department": dept, "position": pos}


def test_diff_join_leave_and_changes():
    prev = [_r("김민수", "소화기내과", "교수"), _r("이영희", "소화기내과", "임상교수"), _r("박준호", "정형외과", "과장")]
    cur = [_r("김민수", "소화기내과", "교수"), _r("강다혜", "소화기내과", "전문의"), _r("박준호", "정형외과", "진료부장")]
    kinds = {(c["kind"], c["name"]) for c in diff_rosters(prev, cur)}
    assert ("left", "이영희") in kinds
    assert ("joined", "강다혜") in kinds
    assert ("position_changed", "박준호") in kinds
    assert ("left", "김민수") not in kinds


def test_diff_dept_change_is_not_leave():
    prev = [_r("김민수", "내과", "전문의")]
    cur = [_r("김민수", "소화기내과", "전문의")]
    ch = diff_rosters(prev, cur)
    assert [c["kind"] for c in ch] == ["dept_changed"]


def test_diff_same_name_two_people():
    prev = [_r("김민수", "내과"), _r("김민수", "외과")]
    cur = [_r("김민수", "내과")]
    ch = diff_rosters(prev, cur)
    assert [(c["kind"], c["department"]) for c in ch] == [("left", "외과")]
