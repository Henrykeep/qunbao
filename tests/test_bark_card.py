import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "digest"))
from bark import build_payload


def test_bank_card_copied_whole():
    assert build_payload("通知", "银行卡号 6222 0212 3456 7890")["copy"] == "6222021234567890"
    assert build_payload("通知", "银行卡号：6222021234567890123")["copy"] == "6222021234567890123"


def test_bark_placeholder_case_insensitive():
    from digest import bark
    assert bark.strip_md("[PHOTO] [voice message]") == "[图片] [语音]"


def test_group_label_strips_edge_brackets():
    from digest.bark import group_label
    assert group_label("（计科2班") == "计科2班"


def test_group_label_strips_stacked_counts():
    from digest.bark import group_label
    assert group_label("计科2班（358人）【12】") == "计科2班"
