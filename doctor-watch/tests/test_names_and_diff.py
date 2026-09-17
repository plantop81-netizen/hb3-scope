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


def test_diff_same_name_multiple_pages_is_one_person():
    # 대학병원: 한 교수가 진료과 페이지와 센터 페이지에 모두 실림 → 한쪽에서 빠져도 '제외' 아님
    prev = [_r("김민수", "소화기내과", "교수"), _r("김민수", "간센터", "교수")]
    cur = [_r("김민수", "소화기내과", "교수")]
    assert diff_rosters(prev, cur) == []
    # 진료과 집합이 전혀 안 겹치면 진료과 변경
    ch = diff_rosters([_r("김민수", "내과")], [_r("김민수", "소화기내과"), _r("김민수", "내시경센터")])
    assert [(c["kind"], c["prev_department"], c["department"]) for c in ch] == [("dept_changed", "내과", "소화기내과 / 내시경센터")]


def test_left_requires_page_seen_this_run(tmp_path):
    """이번 실행에서 못 읽은 페이지의 의사는 '제외' 로 세지 않는다 (pipeline 필터 규칙을 DB 로 검증)."""
    from doctor_watch import db as D
    from doctor_watch.pipeline import _ok_page_urls

    with D.session(tmp_path / "t.db") as conn:
        hid = D.upsert_hospital(conn, {"name": "A병원", "url": "http://a"})
        r1, r2 = D.start_run(conn), D.start_run(conn)
        for run, pages in ((r1, [("http://a/p1", "h1"), ("http://a/p2", "h2")]), (r2, [("http://a/p1", "h1")])):
            for url, h in pages:
                conn.execute("INSERT INTO pages (run_id, hospital_id, url, http_status, content_hash, fetched_at) VALUES (?,?,?,?,?,?)", (run, hid, url, 200, h, "t"))
            conn.execute("INSERT INTO pages (run_id, hospital_id, url, http_status, error, fetched_at) VALUES (?,?,?,?,?,?)", (run, hid, "http://a/p3", 0, "timeout", "t"))
        assert _ok_page_urls(conn, r1, hid) == {"http://a/p1", "http://a/p2"}
        assert _ok_page_urls(conn, r2, hid) == {"http://a/p1"}


def test_apply_snapshot_hysteresis(tmp_path):
    """무작위로 바뀌는 페이지에 잠깐 실린 이름은 변동이 되지 않고, 2회 연속이면 확정된다."""
    from doctor_watch import db as D
    from doctor_watch.diff import apply_snapshot

    def rows(*names, url="http://a/dept"):
        return [{"name": n, "name_key": name_key(n), "department": "내과", "position": "교수", "source_url": url} for n in names]

    with D.session(tmp_path / "t.db") as conn:
        hid = D.upsert_hospital(conn, {"name": "A병원", "url": "http://a"})
        r = [D.start_run(conn) for _ in range(5)]
        assert apply_snapshot(conn, r[0], hid, rows("김철수", "이영희"), set(), baseline=True) == []
        # 2회차: 무작위 페이지에 박잠깐 등장, 이영희 안 보임 → 아직 변동 없음
        assert apply_snapshot(conn, r[1], hid, rows("김철수") + rows("박잠깐", url="http://a/random"), {"http://a/dept", "http://a/random"}) == []
        # 3회차: 박잠깐 사라짐(잡음 폐기), 이영희 2회 연속 부재 → 제외 확정
        ch = apply_snapshot(conn, r[2], hid, rows("김철수"), {"http://a/dept"})
        assert [(c["kind"], c["name"]) for c in ch] == [("left", "이영희")]
        # 4회차: 신규 최민수 등장 → pending
        assert apply_snapshot(conn, r[3], hid, rows("김철수", "최민수"), {"http://a/dept"}) == []
        # 5회차: 최민수 2회 연속 → 합류 확정. 김철수의 페이지를 못 읽은 경우는 부재로 세지 않는다
        ch = apply_snapshot(conn, r[4], hid, rows("최민수", url="http://a/other"), {"http://a/other"})
        assert [(c["kind"], c["name"]) for c in ch] == [("joined", "최민수")]
        assert conn.execute("SELECT status, miss_count FROM doctor_state WHERE name='김철수'").fetchone()["miss_count"] == 0


def test_hospital_key_merges_org_prefixes():
    from doctor_watch.names import hospital_key

    assert hospital_key("의료법인 인당의료재단 부민병원") == "부민병원"
    assert hospital_key("부산성모병원(재단법인 천주교부산교구유지재단)") == "부산성모병원"
    assert hospital_key("인제대학교 부산백병원") == hospital_key("인제대학교부산백병원")
