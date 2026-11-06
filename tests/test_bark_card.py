import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "digest"))
from bark import build_payload, group_label


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


def test_group_name_strips_mute_tag():
    from digest import bark
    assert bark.MEMBER_COUNT.sub("", "计算机2班(免打扰)") == "计算机2班"


def test_group_label_strips_leading_tag():
    assert group_label("[置顶] 计算机2班(358)") == "计算机2班"


def test_group_label_keeps_paired_brackets():
    from bark_group import group_label
    assert group_label("【通知】计算机2班") == "【通知】计算机2班"
    assert group_label("计算机2班（官方群）") == "计算机2班(官方群)"
    assert group_label("(abc") == "abc"


def test_group_label_strips_leading_count_dash():
    from digest.bark import group_label
    assert group_label("358人 - 计算机2班") == "计算机2班"


def test_group_label_strips_lead_prefixed_count():
    from digest.bark import group_label
    assert group_label("在线12人 - 计科2班") == group_label("计科2班")
    assert group_label("未读5 | 计科2班") == group_label("计科2班")


def test_bark_group_strips_99plus_and_new_msg_counts():
    from bark_group import group_label
    for s in ("计算机2班 (3条新消息)", "计算机2班 [99+]", "[99+] 计算机2班"):
        assert group_label(s) == "计算机2班"
