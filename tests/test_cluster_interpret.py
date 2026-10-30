import json

from backend.app.services.cluster_service import interpret_clusters


def _result():
    return {"clusters": [{"id": 0, "name": "缩量整理", "size": 3, "return_3d": 1.2, "pattern": [{"day": "t1", "pct": 1.0}]}]}


def test_ai_text_added():
    seen = {}

    def chat(prompt):
        seen["prompt"] = prompt
        return json.dumps({"items": [{"id": 0, "text": "三日缩量小涨"}]})

    result = interpret_clusters(_result(), chat=chat)
    assert result["clusters"][0]["ai_text"] == "三日缩量小涨"
    assert result["clusters"][0]["name"] == "缩量整理"
    assert "pct=1.0" in seen["prompt"]


def test_ai_failure_keeps_rule_label():
    from backend.app.core.ai_client import AiFailed

    def chat(prompt):
        raise AiFailed("读取超时")

    result = interpret_clusters(_result(), chat=chat)
    assert "ai_text" not in result["clusters"][0] and "保留规则标签" in result["ai_note"]
