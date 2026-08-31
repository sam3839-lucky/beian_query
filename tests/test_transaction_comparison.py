"""往期成交对比接口与纯数据组装测试。"""

from datetime import date, datetime, timedelta

import pytest

import app as app_module


class _QueryResult:
    """提供 ``fetchall`` 的最小查询结果。"""

    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _RecordingDB:
    """记录 SQL 和参数，避免单元测试连接生产 PostgreSQL。"""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _QueryResult(self.rows)


def _coverage(
    *,
    latest=date(2026, 8, 30),
    new_from=date(2022, 1, 1),
    used_from=date(2022, 1, 1),
    new_continuous_from=None,
    used_continuous_from=None,
    new_to=None,
    used_to=None,
):
    return {
        "new": {
            "available_from": new_from,
            "continuous_from": new_continuous_from or new_from,
            "available_to": new_to or latest,
        },
        "used": {
            "available_from": used_from,
            "continuous_from": used_continuous_from or used_from,
            "available_to": used_to or latest,
        },
        "common_latest": latest,
    }


def _daily_rows(day, *, new=None, used=None):
    rows = []
    if new is not None:
        rows.append(
            {"report_date": day, "property_type": "new", "deal_count": new}
        )
    if used is not None:
        rows.append(
            {"report_date": day, "property_type": "used", "deal_count": used}
        )
    return rows


def _month_group(payload, month):
    return next(item for item in payload["monthly_same_period"] if item["month"] == month)


def _month_point(payload, month, year):
    group = _month_group(payload, month)
    return next(item for item in group["rows"] if item["year"] == year)


def _year_point(payload, year):
    return next(item for item in payload["year_to_date"] if item["year"] == year)


def _continuous_point(payload, year, month):
    return next(
        item
        for item in payload["continuous_months"]
        if item["year"] == year and item["month"] == month
    )


@pytest.fixture(autouse=True)
def _clear_comparison_cache(monkeypatch):
    """端点缓存属于进程状态，测试之间必须隔离。"""
    monkeypatch.delattr(
        app_module.api_transactions_comparison, "_cache", raising=False
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2026, 8, 30), date(2026, 8, 30)),
        (datetime(2026, 8, 30, 23, 59, 58), date(2026, 8, 30)),
        ("2026-08-30", date(2026, 8, 30)),
        (None, None),
    ],
)
def test_normalize_date_accepts_only_supported_values(value, expected):
    assert app_module._normalize_date(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "2026/08/30",
        "30-08-2026",
        "2026-8-30",
        "2026-08-30T00:00:00",
        "not-a-date",
    ],
)
def test_normalize_date_rejects_ambiguous_strings(value):
    with pytest.raises(ValueError, match="report_date"):
        app_module._normalize_date(value, "report_date")


def test_comparison_period_end_clamps_leap_day_for_non_leap_years():
    assert app_module._comparison_period_end(2024, 2, 29) == date(2024, 2, 29)
    assert app_module._comparison_period_end(2023, 2, 29) == date(2023, 2, 28)
    assert app_module._comparison_period_end(2022, 4, 30) == date(2022, 4, 30)


def test_get_transaction_coverage_uses_fast_min_max_and_earlier_latest_date():
    db = _RecordingDB(
        [
            {
                "property_type": "new",
                "available_from": "2022-01-01",
                "available_to": datetime(2026, 8, 31, 18, 0),
            },
            {
                "property_type": "used",
                "available_from": date(2018, 2, 6),
                "available_to": "2026-08-30",
            },
        ]
    )

    result = app_module._get_transaction_coverage(db)

    assert result["new"]["available_from"] == date(2022, 1, 1)
    assert result["new"]["available_to"] == date(2026, 8, 31)
    assert result["used"]["available_from"] == date(2018, 2, 6)
    assert result["used"]["available_to"] == date(2026, 8, 30)
    assert result["common_latest"] == date(2026, 8, 30)
    assert len(db.calls) == 1
    sql, params = db.calls[0]
    sql_upper = sql.upper()
    assert "property_types" in sql
    assert "MIN(" in sql_upper
    assert "MAX(" in sql_upper
    assert "LAG(" not in sql_upper
    assert "LEAD(" not in sql_upper
    assert " OVER " not in sql_upper
    assert params == [1, 5999]


def test_query_transaction_comparison_uses_parameterized_date_range():
    rows = [
        {
            "report_date": date(2024, 1, 1),
            "property_type": "new",
            "deal_count": 3,
        }
    ]
    db = _RecordingDB(rows)

    result = app_module._query_transaction_comparison(
        db, years=3, common_latest=date(2026, 8, 30)
    )

    assert result == rows
    assert len(db.calls) == 1
    sql, params = db.calls[0]
    assert "property_types" in sql
    assert "%s" in sql
    serialized_params = {
        value.isoformat() if isinstance(value, (date, datetime)) else str(value)
        for value in params
    }
    assert "2024-01-01" in serialized_params
    assert "2026-08-30" in serialized_params
    assert "2024-01-01" not in sql
    assert "2026-08-30" not in sql
    assert params == [1, 5999, date(2024, 1, 1), date(2026, 8, 30)]


def test_query_transaction_comparison_skips_db_when_no_common_latest():
    db = _RecordingDB([{"unexpected": "row"}])

    assert app_module._query_transaction_comparison(db, 5, None) == []
    assert db.calls == []


def test_apply_continuous_coverage_detects_late_window_start():
    coverage = {
        "new": {
            "available_from": date(2018, 1, 1),
            "available_to": date(2026, 8, 30),
        },
        "used": {
            "available_from": date(2018, 2, 6),
            "available_to": date(2026, 8, 30),
        },
        "common_latest": date(2026, 8, 30),
    }
    rows = []
    cursor = date(2022, 1, 2)
    while cursor <= date(2026, 8, 30):
        rows += _daily_rows(cursor, new=10)
        cursor += timedelta(days=30)
    cursor = date(2022, 8, 26)
    while cursor <= date(2026, 8, 30):
        rows += _daily_rows(cursor, used=20)
        cursor += timedelta(days=30)

    result = app_module._apply_continuous_coverage(
        rows,
        coverage,
        common_latest=date(2026, 8, 30),
        years=5,
    )

    assert result["new"]["available_from"] == date(2018, 1, 1)
    assert result["new"]["continuous_from"] == date(2022, 1, 1)
    assert result["used"]["available_from"] == date(2018, 2, 6)
    assert result["used"]["continuous_from"] == date(2022, 8, 26)
    assert result["used"]["available_to"] == date(2026, 8, 30)
    assert result["common_latest"] == date(2026, 8, 30)


def test_apply_continuous_coverage_uses_date_after_last_large_gap():
    coverage = {
        "new": {
            "available_from": date(2022, 1, 1),
            "available_to": date(2026, 8, 30),
        },
        "used": {
            "available_from": date(2022, 1, 1),
            "available_to": date(2026, 8, 30),
        },
        "common_latest": date(2026, 8, 30),
    }
    rows = []
    rows += _daily_rows(date(2022, 1, 1), new=1, used=2)
    rows += _daily_rows(date(2022, 1, 15), new=3, used=4)
    rows += _daily_rows(date(2022, 3, 20), new=5, used=6)
    rows += _daily_rows(date(2026, 8, 30), new=7, used=8)

    result = app_module._apply_continuous_coverage(
        rows,
        coverage,
        common_latest=date(2026, 8, 30),
        years=5,
    )

    # 2022-03-20 后还有一个更大的测试数据间隔，因此最后一个断档后日期胜出。
    assert result["new"]["continuous_from"] == date(2026, 8, 30)
    assert result["used"]["continuous_from"] == date(2026, 8, 30)


def test_monthly_same_period_excludes_days_after_the_common_cutoff():
    rows = []
    rows += _daily_rows(date(2026, 8, 30), new=100, used=200)
    rows += _daily_rows(date(2025, 8, 30), new=90, used=180)
    rows += _daily_rows(date(2025, 8, 31), new=900, used=1800)
    rows += _daily_rows(date(2024, 8, 30), new=80, used=160)

    payload = app_module._build_transaction_comparison_payload(
        rows,
        years=3,
        coverage=_coverage(),
        common_latest=date(2026, 8, 30),
    )

    assert payload["latest_date"] == "2026-08-30"
    assert payload["data_dates"] == {
        "new": "2026-08-30",
        "used": "2026-08-30",
        "common": "2026-08-30",
    }
    assert payload["cutoff"] == {"month": 8, "day": 30}
    assert payload["years"] == [2026, 2025, 2024]
    assert payload["coverage"] == {
        "new": {
            "available_from": "2022-01-01",
            "continuous_from": "2022-01-01",
            "available_to": "2026-08-30",
        },
        "used": {
            "available_from": "2022-01-01",
            "continuous_from": "2022-01-01",
            "available_to": "2026-08-30",
        },
    }

    point = _month_point(payload, 8, 2025)
    assert point["start_date"] == "2025-08-01"
    assert point["end_date"] == "2025-08-30"
    assert point["period_status"] == "through_day"
    assert point["new"] == 90
    assert point["used"] == 180
    assert point["total"] == 270
    assert point["data_complete"] is True
    assert point["comparable"] is True
    assert point["missing_metrics"] == []


def test_used_history_before_coverage_stays_null_and_total_is_not_fabricated():
    rows = []
    rows += _daily_rows(date(2022, 1, 10), new=10)
    rows += _daily_rows(date(2022, 8, 10), new=20)
    rows += _daily_rows(date(2022, 8, 26), new=5, used=7)

    payload = app_module._build_transaction_comparison_payload(
        rows,
        years=5,
        coverage=_coverage(
            used_from=date(2018, 2, 6),
            used_continuous_from=date(2022, 8, 26),
        ),
        common_latest=date(2026, 8, 30),
    )

    assert payload["coverage"]["used"] == {
        "available_from": "2018-02-06",
        "continuous_from": "2022-08-26",
        "available_to": "2026-08-30",
    }

    january = _month_point(payload, 1, 2022)
    assert january["new"] == 10
    assert january["used"] is None
    assert january["total"] is None
    assert january["data_complete"] is False
    assert january["comparable"] is False
    assert january["missing_metrics"] == ["used"]

    august = _month_point(payload, 8, 2022)
    assert august["new"] == 25
    assert august["used"] is None
    assert august["total"] is None
    assert august["missing_metrics"] == ["used"]

    year_to_date = _year_point(payload, 2022)
    assert year_to_date["new"] == 35
    assert year_to_date["used"] is None
    assert year_to_date["total"] is None
    assert year_to_date["missing_metrics"] == ["used"]


def test_continuous_from_boundary_not_global_min_controls_completeness():
    rows = []
    rows += _daily_rows(date(2023, 1, 10), new=7, used=9)
    rows += _daily_rows(date(2024, 1, 1), new=11, used=13)

    payload = app_module._build_transaction_comparison_payload(
        rows,
        years=5,
        coverage=_coverage(
            latest=date(2026, 1, 31),
            new_from=date(2022, 1, 1),
            used_from=date(2018, 2, 6),
            used_continuous_from=date(2024, 1, 1),
        ),
        common_latest=date(2026, 1, 31),
    )

    before_boundary = _month_point(payload, 1, 2023)
    assert before_boundary["new"] == 7
    assert before_boundary["used"] is None
    assert before_boundary["total"] is None
    assert before_boundary["missing_metrics"] == ["used"]

    at_boundary = _month_point(payload, 1, 2024)
    assert at_boundary["new"] == 11
    assert at_boundary["used"] == 13
    assert at_boundary["total"] == 24
    assert at_boundary["data_complete"] is True


def test_explicit_zero_is_preserved_when_both_metrics_are_complete():
    rows = _daily_rows(date(2026, 1, 1), new=0, used=5)

    payload = app_module._build_transaction_comparison_payload(
        rows,
        years=3,
        coverage=_coverage(),
        common_latest=date(2026, 8, 30),
    )

    point = _month_point(payload, 1, 2026)
    assert point["new"] == 0
    assert point["used"] == 5
    assert point["total"] == 5
    assert point["data_complete"] is True
    assert point["missing_metrics"] == []


def test_leap_day_cutoff_maps_each_year_to_its_actual_month_end():
    rows = []
    rows += _daily_rows(date(2024, 2, 29), new=30, used=60)
    rows += _daily_rows(date(2023, 2, 28), new=20, used=40)
    rows += _daily_rows(date(2022, 2, 28), new=10, used=20)

    payload = app_module._build_transaction_comparison_payload(
        rows,
        years=3,
        coverage=_coverage(
            latest=date(2024, 2, 29),
            new_from=date(2022, 1, 1),
            used_from=date(2022, 1, 1),
        ),
        common_latest=date(2024, 2, 29),
    )

    assert payload["cutoff"] == {"month": 2, "day": 29}
    february = _month_group(payload, 2)
    assert february["period_status"] == "full_month"
    assert all(
        point["period_status"] == "full_month" for point in february["rows"]
    )
    assert _month_point(payload, 2, 2024)["end_date"] == "2024-02-29"
    assert _month_point(payload, 2, 2023)["end_date"] == "2023-02-28"
    assert _month_point(payload, 2, 2022)["end_date"] == "2022-02-28"
    assert _month_point(payload, 2, 2023)["total"] == 60


def test_payload_order_is_stable_and_continuous_gaps_are_null_not_zero():
    rows = []
    rows += _daily_rows(date(2025, 1, 5), new=10, used=20)
    rows += _daily_rows(date(2025, 3, 5), new=30, used=40)
    rows += _daily_rows(date(2026, 1, 5), new=50, used=60)
    rows += _daily_rows(date(2026, 3, 15), new=70, used=80)

    payload = app_module._build_transaction_comparison_payload(
        rows,
        years=3,
        coverage=_coverage(
            latest=date(2026, 3, 15),
            new_from=date(2024, 1, 1),
            used_from=date(2024, 1, 1),
        ),
        common_latest=date(2026, 3, 15),
    )

    assert [item["month"] for item in payload["monthly_same_period"]] == [1, 2, 3]
    assert [item["year"] for item in _month_group(payload, 1)["rows"]] == [
        2026,
        2025,
        2024,
    ]
    assert [item["year"] for item in payload["year_to_date"]] == [
        2026,
        2025,
        2024,
    ]

    continuous_keys = [
        (item["year"], item["month"]) for item in payload["continuous_months"]
    ]
    assert continuous_keys == sorted(continuous_keys)
    assert continuous_keys[0] == (2025, 1)
    assert continuous_keys[-1] == (2026, 3)

    gap = _continuous_point(payload, 2025, 2)
    assert gap["new"] is None
    assert gap["used"] is None
    assert gap["total"] is None
    assert set(gap["missing_metrics"]) == {"new", "used"}


def test_empty_coverage_returns_stable_empty_payload():
    coverage = {
        "new": {
            "available_from": date(2022, 1, 1),
            "continuous_from": date(2022, 1, 1),
            "available_to": date(2026, 8, 30),
        },
        "used": {
            "available_from": None,
            "continuous_from": None,
            "available_to": None,
        },
        "common_latest": None,
    }

    payload = app_module._build_transaction_comparison_payload(
        [], years=5, coverage=coverage, common_latest=None
    )

    assert payload["latest_date"] is None
    assert payload["data_dates"] == {
        "new": "2026-08-30",
        "used": None,
        "common": None,
    }
    assert payload["cutoff"] is None
    assert payload["years"] == []
    assert payload["coverage"]["used"] == {
        "available_from": None,
        "continuous_from": None,
        "available_to": None,
    }
    assert payload["monthly_same_period"] == []
    assert payload["year_to_date"] == []
    assert payload["continuous_months"] == []


def test_payload_preserves_explicit_null_continuous_from():
    coverage = {
        "new": {
            "available_from": date(2018, 1, 1),
            "continuous_from": None,
            "available_to": date(2026, 8, 30),
        },
        "used": {
            "available_from": date(2018, 2, 6),
            "continuous_from": None,
            "available_to": date(2026, 8, 30),
        },
        "common_latest": None,
    }

    payload = app_module._build_transaction_comparison_payload(
        [], years=5, coverage=coverage, common_latest=None
    )

    assert payload["coverage"]["new"]["available_from"] == "2018-01-01"
    assert payload["coverage"]["new"]["continuous_from"] is None
    assert payload["coverage"]["used"]["available_from"] == "2018-02-06"
    assert payload["coverage"]["used"]["continuous_from"] is None


@pytest.mark.parametrize("years", ["0", "2", "4", "6", "abc", "3.0", ""])
def test_comparison_endpoint_rejects_invalid_years_before_opening_db(
    monkeypatch, years
):
    def fail_if_called():
        raise AssertionError("非法 years 不应访问数据库")

    monkeypatch.setattr(app_module, "get_db", fail_if_called)

    response = app_module.app.test_client().get(
        "/api/transactions/comparison", query_string={"years": years}
    )

    assert response.status_code == 400
    body = response.get_json()
    assert body["error"]["code"] == "INVALID_YEARS"
    assert body["error"]["message"]


@pytest.mark.parametrize("years", [3, 5])
def test_comparison_endpoint_orchestrates_valid_requests(monkeypatch, years):
    fake_db = object()
    coverage = _coverage()
    applied_coverage = _coverage(
        used_from=date(2018, 2, 6),
        used_continuous_from=date(2022, 8, 26),
    )
    raw_rows = _daily_rows(date(2026, 8, 30), new=10, used=20)
    expected = {
        "latest_date": "2026-08-30",
        "data_dates": {
            "new": "2026-08-30",
            "used": "2026-08-30",
            "common": "2026-08-30",
        },
        "cutoff": {"month": 8, "day": 30},
        "years": list(range(2026, 2026 - years, -1)),
        "coverage": {},
        "monthly_same_period": [],
        "year_to_date": [],
        "continuous_months": [],
    }
    calls = []

    monkeypatch.setattr(app_module, "get_db", lambda: fake_db)
    monkeypatch.setattr(
        app_module,
        "_get_transaction_coverage",
        lambda db: calls.append(("coverage", db)) or coverage,
    )
    monkeypatch.setattr(
        app_module,
        "_query_transaction_comparison",
        lambda db, year_count, common_latest: calls.append(
            ("query", db, year_count, common_latest)
        )
        or raw_rows,
    )
    monkeypatch.setattr(
        app_module,
        "_apply_continuous_coverage",
        lambda rows, coverage_arg, common_latest, year_count: calls.append(
            ("apply", rows, coverage_arg, common_latest, year_count)
        )
        or applied_coverage,
    )
    monkeypatch.setattr(
        app_module,
        "_build_transaction_comparison_payload",
        lambda rows, year_count, coverage_arg, common_latest: calls.append(
            ("build", rows, year_count, coverage_arg, common_latest)
        )
        or expected,
    )

    response = app_module.app.test_client().get(
        "/api/transactions/comparison", query_string={"years": years}
    )

    assert response.status_code == 200
    assert response.get_json() == expected
    assert calls == [
        ("coverage", fake_db),
        ("query", fake_db, years, date(2026, 8, 30)),
        ("apply", raw_rows, coverage, date(2026, 8, 30), years),
        ("build", raw_rows, years, applied_coverage, date(2026, 8, 30)),
    ]


def test_comparison_endpoint_cache_key_includes_years_latest_and_min_max(
    monkeypatch,
):
    fake_db = object()
    state = {
        "latest": date(2026, 8, 30),
        "used_available_from": date(2018, 2, 6),
    }
    calls = {"coverage": 0, "query": [], "apply": [], "build": []}

    def get_coverage(db):
        assert db is fake_db
        calls["coverage"] += 1
        coverage = _coverage(
            latest=state["latest"],
            used_from=state["used_available_from"],
        )
        coverage["new"].pop("continuous_from")
        coverage["used"].pop("continuous_from")
        return coverage

    def query(db, years, common_latest):
        assert db is fake_db
        calls["query"].append((years, common_latest))
        return []

    def apply(rows, coverage, common_latest, years):
        assert rows == []
        calls["apply"].append((years, common_latest))
        result = {
            property_type: dict(coverage[property_type])
            for property_type in ("new", "used")
        }
        for property_type in ("new", "used"):
            result[property_type]["continuous_from"] = result[property_type][
                "available_from"
            ]
        result["common_latest"] = coverage["common_latest"]
        return result

    def build(rows, years, coverage, common_latest):
        assert rows == []
        assert coverage["common_latest"] == common_latest
        calls["build"].append((years, common_latest))
        return {"years": years, "latest_date": common_latest.isoformat()}

    monkeypatch.setattr(app_module, "get_db", lambda: fake_db)
    monkeypatch.setattr(app_module, "_get_transaction_coverage", get_coverage)
    monkeypatch.setattr(app_module, "_query_transaction_comparison", query)
    monkeypatch.setattr(app_module, "_apply_continuous_coverage", apply)
    monkeypatch.setattr(
        app_module, "_build_transaction_comparison_payload", build
    )

    client = app_module.app.test_client()
    assert client.get("/api/transactions/comparison?years=3").status_code == 200
    assert client.get("/api/transactions/comparison?years=3").status_code == 200
    assert client.get("/api/transactions/comparison?years=5").status_code == 200
    # 快速 MIN/MAX 签名变化，即使共同截止日不变也必须失效。
    state["used_available_from"] = date(2018, 1, 1)
    assert client.get("/api/transactions/comparison?years=5").status_code == 200
    state["latest"] = date(2026, 8, 31)
    assert client.get("/api/transactions/comparison?years=5").status_code == 200

    # coverage 必须每次刷新以构造缓存键；重查询/重组装只在键变化时发生。
    assert calls["coverage"] == 5
    assert calls["query"] == [
        (3, date(2026, 8, 30)),
        (5, date(2026, 8, 30)),
        (5, date(2026, 8, 30)),
        (5, date(2026, 8, 31)),
    ]
    assert calls["apply"] == calls["query"]
    assert calls["build"] == calls["query"]


def test_comparison_endpoint_handles_missing_common_latest_without_querying_db(
    monkeypatch,
):
    fake_db = _RecordingDB([{"unexpected": "row"}])
    coverage = {
        "new": {
            "available_from": date(2022, 1, 1),
            "continuous_from": date(2022, 1, 1),
            "available_to": date(2026, 8, 30),
        },
        "used": {
            "available_from": None,
            "continuous_from": None,
            "available_to": None,
        },
        "common_latest": None,
    }

    monkeypatch.setattr(app_module, "get_db", lambda: fake_db)
    monkeypatch.setattr(
        app_module, "_get_transaction_coverage", lambda db: coverage
    )

    response = app_module.app.test_client().get(
        "/api/transactions/comparison?years=5"
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["latest_date"] is None
    assert body["years"] == []
    assert body["monthly_same_period"] == []
    assert body["year_to_date"] == []
    assert body["continuous_months"] == []
    assert fake_db.calls == []
