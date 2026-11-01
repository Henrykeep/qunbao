import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "digest"))
from bark import build_payload


def test_bank_card_copied_whole():
    assert build_payload("通知", "银行卡号 6222 0212 3456 7890")["copy"] == "6222021234567890"
    assert build_payload("通知", "银行卡号：6222021234567890123")["copy"] == "6222021234567890123"
