from datetime import date, timedelta

import pytest

import app as app_module


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Db:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _Result(self.rows)


def _row(district_id, name, months, *, deals=100, inventory=1000, age=0, filled=False):
    return {
        "district_id": district_id,
        "district_name": name,
        "stat_date": date(2026, 9, 20),
        "inventory_count": inventory,
        "deal_90d": deals,
        "daily_avg": deals / 90,
        "months": months,
        "conclusion": None,
        "inventory_as_of": date(2026, 9, 20) - timedelta(days=age),
        "inventory_filled": filled,
        "inventory_age_days": age,
        "expected_districts": 3,
    }


@pytest.fixture(autouse=True)
def clear_cache():
    app_module.api_new_house_destocking._cache = None


def test_payload_sorts_nulls_last_and_preserves_quality_states():
    rows = [
        _row(5999, "全市", 12.6),
        _row(2, "龙岗", 18.4, age=40),
        _row(3, "南山", None, deals=0),
        _row(1, "福田", 7.2),
    ]

    payload = app_module._destocking_payload(rows, loaded_at="fixed")

    assert payload["window_days"] == 90
    assert payload["data_as_of"] == "2026-09-20"
    assert [item["district_name"] for item in payload["districts"]] == ["福田", "龙岗", "南山"]
    assert payload["districts"][1]["status"] == "stale"
    assert payload["districts"][2]["status"] == "no_deal"


@pytest.mark.parametrize(
    ("months", "expected"),
    [
        (None, "暂无数据"),
        (5.9, "供不应求"),
        (6, "供需平衡"),
        (12, "供需平衡"),
        (12.1, "去化承压"),
        (18, "去化承压"),
        (18.1, "严重滞销"),
    ],
)
def test_conclusion_boundaries(months, expected):
    assert app_module._destocking_conclusion(months) == expected


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"inventory_count": None}, "no_inventory"),
        ({"inventory_count": -1}, "unavailable"),
        ({"inventory_count": "1000"}, "unavailable"),
        ({"deal_90d": None}, "unavailable"),
        ({"deal_90d": -1}, "unavailable"),
        ({"inventory_count": 0}, "no_inventory"),
        ({"deal_90d": 0}, "no_deal"),
        ({"inventory_filled": True}, "forward_filled"),
    ],
)
def test_status_maps_missing_zero_invalid_and_filled_values(overrides, expected):
    row = _row(1, "福田", 9.0)
    row.update(overrides)
    assert app_module._destocking_status(row, "2026-09-20") == expected


def test_payload_returns_none_for_empty_rows_and_marks_partial_coverage():
    assert app_module._destocking_payload([]) is None

    rows = [_row(5999, "全市", 12.6), _row(1, "福田", 7.2)]
    payload = app_module._destocking_payload(rows, loaded_at="fixed")

    assert payload["data_status"] == "partial"
    assert payload["coverage"] == {"available": 1, "expected": 3}
    assert payload["citywide"]["status"] == "partial"


def test_endpoint_uses_one_batch_query(monkeypatch):
    db = _Db([_row(5999, "全市", 12.6), _row(1, "福田", 7.2)])
    monkeypatch.setattr(app_module, "get_db", lambda: db)

    response = app_module.app.test_client().get("/api/new-house-destocking")

    assert response.status_code == 200
    assert response.get_json()["citywide"]["months"] == 12.6
    assert len(db.calls) == 1
    assert "ROW_NUMBER" in db.calls[0][0]
    assert "monthly_metrics" in db.calls[0][0]


def test_endpoint_returns_structured_503_without_database_details(monkeypatch):
    monkeypatch.setattr(app_module, "get_db", lambda: (_ for _ in ()).throw(RuntimeError("secret DSN")))

    response = app_module.app.test_client().get("/api/new-house-destocking")

    assert response.status_code == 503
    body = response.get_json()
    assert body["error"]["code"] == "DESTOCKING_UNAVAILABLE"
    assert "secret DSN" not in response.get_data(as_text=True)
