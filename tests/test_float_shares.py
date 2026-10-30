from backend.app.providers import float_shares


def test_parse_tencent_fields():
    fields = ["0"] * 74
    fields[2], fields[3], fields[44], fields[72] = "600000", "10.0", "3337.24", "33305838300"
    line = 'v_sh600000="' + "~".join(fields) + '";'
    assert float_shares.parse(line) == {"600000": 33305838300.0}
    fields[72] = ""
    assert abs(float_shares.parse("~".join(fields))["600000"] - 3337.24e8 / 10) < 1


def test_fill_turnover(tmp_path, monkeypatch):
    monkeypatch.setattr(float_shares, "_path", lambda: tmp_path / "f.json")
    float_shares.set_fetcher(lambda codes: {c: 1_000_000.0 for c in codes})
    try:
        bars = [{"volume": 50_000.0, "turnover_rate": None}, {"volume": 10.0, "turnover_rate": 2.0}]
        assert float_shares.fill_turnover("000001", bars) == 1
        assert bars[0]["turnover_rate"] == 5.0 and bars[0]["turnover_estimated"]
        assert bars[1]["turnover_rate"] == 2.0
    finally:
        float_shares.set_fetcher(None)
