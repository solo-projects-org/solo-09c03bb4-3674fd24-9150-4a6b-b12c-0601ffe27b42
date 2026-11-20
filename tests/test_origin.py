"""Tests for the source-country determination API."""
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

BASE_AMOUNTS = [
    {"year": 2023, "code": "A", "amount_cents": 1000},
    {"year": 2023, "code": "B", "amount_cents": 500},
    {"year": 2023, "code": "Z", "amount_cents": 0},
    {"year": 2024, "code": "A", "amount_cents": 1100},
    {"year": 2024, "code": "B", "amount_cents": 600},
    {"year": 2024, "code": "Z", "amount_cents": 0},
]
V1_ENTRIES = [{"old_code": c, "new_code": c} for c in ("A", "B", "Z")]
# v2 merges A and B into one group; Z stays alone.
V2_ENTRIES = [
    {"old_code": "A", "new_code": "A"},
    {"old_code": "B", "new_code": "A"},
    {"old_code": "B", "new_code": "B"},
    {"old_code": "Z", "new_code": "Z"},
]


def put_base(client):
    return client.put("/base", json={"amounts": BASE_AMOUNTS})


def post_mapping(client, version, entries):
    return client.post("/mappings", json={"version": version, "entries": entries})


def put_detail(client, year, code, confirmed, pending=()):
    return client.put(
        f"/details/{year}/{code}",
        json={
            "confirmed": confirmed,
            "pending": [
                {"amount_cents": amount, "candidates": list(candidates)}
                for amount, candidates in pending
            ],
        },
    )


@pytest.fixture()
def client(tmp_path):
    c = TestClient(create_app(str(tmp_path / "t.db")))
    assert put_base(c).status_code == 200
    assert post_mapping(c, 1, V1_ENTRIES).status_code == 201
    return c


def check_apportionment(apportionment, group_total):
    """Evidence apportionments must be feasible and self-consistent."""
    totals = apportionment["totals"]
    assert sum(totals.values()) == group_total
    for assignment in apportionment["assignments"]:
        assert assignment["assigned"] in assignment["candidates"]
    top = max(totals.values())
    assert apportionment["leaders"] == sorted(
        c for c, v in totals.items() if v == top
    )


# ------------------------------------------------------------- 明细写入
def test_details_require_first_mapping_version(tmp_path):
    c = TestClient(create_app(str(tmp_path / "t.db")))
    assert put_base(c).status_code == 200
    r = put_detail(c, 2023, "A", {"CN": 1000})
    assert r.status_code == 409


def test_detail_unknown_base_item_404(client):
    assert put_detail(client, 2023, "NOPE", {"CN": 1}).status_code == 404
    assert put_detail(client, 2025, "A", {"CN": 1}).status_code == 404


def test_total_mismatch_keeps_previous_detail(client):
    assert put_detail(client, 2023, "A", {"CN": 1000}).status_code == 200
    r = put_detail(client, 2023, "A", {"CN": 999})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["base_amount_cents"] == 1000
    assert detail["detail_total_cents"] == 999
    kept = client.get("/details/2023/A").json()
    assert kept["confirmed"] == {"CN": 1000}  # previous detail kept


def test_detail_total_includes_pending(client):
    r = put_detail(client, 2023, "A", {"CN": 400}, [(600, ["CN", "US"])])
    assert r.status_code == 200
    assert put_detail(client, 2023, "A", {"CN": 400}, [(599, ["CN"])]).status_code == 422


def test_replace_detail_and_normalization(client):
    assert put_detail(client, 2023, "A", {"CN": 1000}).status_code == 200
    # lowercase and duplicate candidates are normalized; amounts unchanged
    r = put_detail(client, 2023, "A", {"us": 700}, [(300, ["cn", "us", "CN"])])
    assert r.status_code == 200
    body = r.json()
    assert body["confirmed"] == {"US": 700}
    assert body["pending"] == [{"amount_cents": 300, "candidates": ["CN", "US"]}]
    assert client.get("/details/2023/A").json() == body
    listed = client.get("/details").json()["details"]
    assert [(d["year"], d["code"]) for d in listed] == [(2023, "A")]


def test_negative_amounts_rejected(client):
    r = client.put(
        "/details/2023/A",
        json={"confirmed": {"CN": -1, "US": 1001}, "pending": []},
    )
    assert r.status_code == 422
    r = client.put(
        "/details/2023/A",
        json={"confirmed": {"CN": 1000}, "pending": [{"amount_cents": 0, "candidates": []}]},
    )
    assert r.status_code == 422  # candidates must be non-empty


def test_get_detail_404(client):
    assert client.get("/details/2023/A").status_code == 404
    assert client.get("/details/2023/NOPE").status_code == 404


# ------------------------------------------------------------- 判定结果
def test_determined_all_confirmed(client):
    assert put_detail(client, 2023, "A", {"CN": 1000}).status_code == 200
    assert put_detail(client, 2024, "A", {"CN": 1100}).status_code == 200
    body = client.get("/groups/1/origin").json()
    assert body["group_id"] == 1 and body["version"] == 1
    assert body["years"] == [2023, 2024]
    by_year = {r["year"]: r for r in body["results"]}
    assert by_year[2023]["status"] == "determined"
    assert by_year[2023]["leader"] == "CN"
    assert by_year[2023]["leader_min_cents"] == 1000
    assert by_year[2023]["runner_up_max_cents"] == 0
    assert by_year[2024]["status"] == "determined"
    assert by_year[2024]["codes"] == ["A"]


def test_determined_when_pending_cannot_flip(client):
    # CN keeps at least 800, US reaches at most 200 -> CN always first
    assert put_detail(client, 2023, "A", {"CN": 800, "US": 100}, [(100, ["CN", "US"])]).status_code == 200
    assert put_detail(client, 2024, "A", {"CN": 1100}).status_code == 200
    (result,) = [r for r in client.get("/groups/1/origin").json()["results"] if r["year"] == 2023]
    assert result["status"] == "determined"
    assert result["leader"] == "CN"
    assert result["leader_min_cents"] == 800
    assert result["runner_up_max_cents"] == 200


def test_undetermined_tie_evidence(client):
    assert put_detail(client, 2023, "A", {"CN": 500, "US": 500}).status_code == 200
    assert put_detail(client, 2024, "A", {"CN": 1100}).status_code == 200
    (result,) = [r for r in client.get("/groups/1/origin").json()["results"] if r["year"] == 2023]
    assert result["status"] == "undetermined"
    evidence = result["evidence"]
    assert evidence["type"] == "tie"
    (apportionment,) = evidence["apportionments"]
    check_apportionment(apportionment, 1000)
    assert apportionment["leaders"] == ["CN", "US"]
    assert apportionment["totals"] == {"CN": 500, "US": 500}


def test_undetermined_different_leaders_evidence(client):
    # 300 pending cents can push either CN or US to the top
    assert put_detail(client, 2023, "A", {"CN": 400, "US": 300}, [(300, ["CN", "US"])]).status_code == 200
    assert put_detail(client, 2024, "A", {"CN": 1100}).status_code == 200
    (result,) = [r for r in client.get("/groups/1/origin").json()["results"] if r["year"] == 2023]
    assert result["status"] == "undetermined"
    evidence = result["evidence"]
    assert evidence["type"] == "different_leaders"
    first, second = evidence["apportionments"]
    check_apportionment(first, 1000)
    check_apportionment(second, 1000)
    assert len(first["leaders"]) == len(second["leaders"]) == 1
    assert first["leaders"] != second["leaders"]
    assert {first["leaders"][0], second["leaders"][0]} == {"CN", "US"}


def test_no_imports_when_group_total_zero(client):
    body = client.get("/groups/3/origin").json()  # group 3 = {Z}, all zeros
    assert [r["status"] for r in body["results"]] == ["no_imports", "no_imports"]
    assert [r["group_total_cents"] for r in body["results"]] == [0, 0]


def test_incomplete_lists_missing_items(client):
    assert put_detail(client, 2023, "A", {"CN": 1000}).status_code == 200
    body = client.get("/groups/1/origin").json()
    by_year = {r["year"]: r for r in body["results"]}
    assert by_year[2023]["status"] == "determined"  # 2023 detail present
    assert by_year[2024]["status"] == "incomplete"
    assert by_year[2024]["missing"] == [{"code": "A", "amount_cents": 1100}]
    # group 2 = {B}: nothing submitted at all
    body = client.get("/groups/2/origin").json()
    by_year = {r["year"]: r for r in body["results"]}
    assert by_year[2023]["missing"] == [{"code": "B", "amount_cents": 500}]
    assert by_year[2024]["missing"] == [{"code": "B", "amount_cents": 600}]


def test_zero_amount_code_needs_no_detail(tmp_path):
    c = TestClient(create_app(str(tmp_path / "t.db")))
    assert c.put(
        "/base",
        json={
            "amounts": [
                {"year": 2023, "code": "A", "amount_cents": 1000},
                {"year": 2023, "code": "Z", "amount_cents": 0},
                {"year": 2024, "code": "A", "amount_cents": 1000},
            ]
        },
    ).status_code == 200
    entries = [
        {"old_code": "A", "new_code": "A"},
        {"old_code": "Z", "new_code": "A"},  # Z merges into A's group
    ]
    assert post_mapping(c, 1, entries).status_code == 201
    (group,) = c.get("/summary").json()["groups"]
    assert group["old_codes"] == ["A", "Z"]
    assert put_detail(c, 2023, "A", {"CN": 1000}).status_code == 200
    assert put_detail(c, 2024, "A", {"CN": 1000}).status_code == 200
    body = c.get(f"/groups/{group['group_id']}/origin").json()
    # Z has no details but a zero base amount -> not missing, still determined
    assert [r["status"] for r in body["results"]] == ["determined", "determined"]


def test_group_not_found_404(client):
    r = client.get("/groups/99/origin")
    assert r.status_code == 404


# ------------------------------------------- 更正候选国家改变判定（关键场景）
def test_correcting_candidates_changes_determination(client):
    # same amounts; only the candidate list of the pending 300 cents changes
    assert put_detail(client, 2023, "A", {"CN": 400, "US": 300}, [(300, ["CN", "US"])]).status_code == 200
    assert put_detail(client, 2024, "A", {"CN": 1100}).status_code == 200

    def status_2023():
        body = client.get("/groups/1/origin").json()
        return next(r for r in body["results"] if r["year"] == 2023)

    result = status_2023()
    assert result["status"] == "undetermined"  # CN 400..700 vs US 300..600

    # correction 1: pending can only go to US -> US always first
    assert put_detail(client, 2023, "A", {"CN": 400, "US": 300}, [(300, ["US"])]).status_code == 200
    result = status_2023()
    assert result["status"] == "determined"
    assert result["leader"] == "US"
    assert result["leader_min_cents"] == 600
    assert result["runner_up_max_cents"] == 400

    # correction 2: pending can only go to CN -> CN always first
    assert put_detail(client, 2023, "A", {"CN": 400, "US": 300}, [(300, ["CN"])]).status_code == 200
    result = status_2023()
    assert result["status"] == "determined"
    assert result["leader"] == "CN"


# ------------------------------------------------------- 版本与一致性
def test_origin_follows_current_version(client):
    assert put_detail(client, 2023, "A", {"CN": 1000}).status_code == 200
    assert put_detail(client, 2024, "A", {"CN": 1100}).status_code == 200
    assert put_detail(client, 2023, "B", {"US": 500}).status_code == 200
    assert put_detail(client, 2024, "B", {"US": 600}).status_code == 200
    assert post_mapping(client, 2, V2_ENTRIES).status_code == 201

    # groups 1 and 2 are gone in v2; group 4 = {A, B}
    assert client.get("/groups/1/origin").status_code == 404
    body = client.get("/groups/4/origin").json()
    assert body["version"] == 2
    by_year = {r["year"]: r for r in body["results"]}
    assert by_year[2023]["codes"] == ["A", "B"]
    assert by_year[2023]["group_total_cents"] == 1500
    # CN 1000 vs US 500 -> determined CN for 2023
    assert by_year[2023]["status"] == "determined"
    assert by_year[2023]["leader"] == "CN"


def test_details_survive_restart(tmp_path):
    db = str(tmp_path / "t.db")
    c1 = TestClient(create_app(db))
    assert put_base(c1).status_code == 200
    assert post_mapping(c1, 1, V1_ENTRIES).status_code == 201
    assert put_detail(c1, 2023, "A", {"CN": 400, "US": 300}, [(300, ["CN", "US"])]).status_code == 200
    c1.app.state.store.close()

    c2 = TestClient(create_app(db))
    detail = c2.get("/details/2023/A").json()
    assert detail["confirmed"] == {"CN": 400, "US": 300}
    assert detail["pending"] == [{"amount_cents": 300, "candidates": ["CN", "US"]}]
    (result,) = [r for r in c2.get("/groups/1/origin").json()["results"] if r["year"] == 2023]
    assert result["status"] == "undetermined"


def test_concurrent_instances_read_while_write(tmp_path):
    """A second instance keeps correcting details and publishing mappings
    while origin queries run; every answer is one consistent committed state."""
    db = str(tmp_path / "t.db")
    writer = TestClient(create_app(db))
    reader = TestClient(create_app(db))  # another instance over the same file
    assert put_base(writer).status_code == 200
    assert post_mapping(writer, 1, V1_ENTRIES).status_code == 201
    assert put_detail(writer, 2023, "A", {"CN": 1000}).status_code == 200
    assert put_detail(writer, 2024, "A", {"CN": 1100}).status_code == 200

    stop = threading.Event()
    failures = []

    def read_loop():
        while not stop.is_set():
            r = reader.get("/groups/1/origin")
            if r.status_code == 404:  # group 1 removed once v2 lands
                continue
            if r.status_code != 200:
                failures.append(f"status {r.status_code}: {r.text}")
                continue
            body = r.json()
            if body["version"] not in (1, 2):
                failures.append(f"bad version {body['version']}")
            for result in body["results"]:
                if result["year"] != 2023:
                    continue
                # details alternate CN<->US 1000: any consistent snapshot is
                # a determined year led by one of them
                if result["status"] != "determined" or result["leader"] not in ("CN", "US"):
                    failures.append(f"torn read: {result}")

    def write_loop():
        for i in range(60):
            country = "US" if i % 2 else "CN"
            r = put_detail(writer, 2023, "A", {country: 1000})
            assert r.status_code == 200
        r = post_mapping(writer, 2, V2_ENTRIES)
        assert r.status_code == 201

    readers = [threading.Thread(target=read_loop) for _ in range(2)]
    writer_thread = threading.Thread(target=write_loop)
    for t in readers:
        t.start()
    writer_thread.start()
    writer_thread.join()
    stop.set()
    for t in readers:
        t.join()
    assert failures == []
    assert writer.get("/versions").json()["current_version"] == 2
