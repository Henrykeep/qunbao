
def test_group_no_spaced():
    from bark_copy import pick_copy
    assert pick_copy('群号：123 456 789','','')=='123456789'


def test_bare_domain_link():
    from bark_copy import pick_copy
    assert pick_copy('问卷 wj.qq.com/s/123 今晚截止','','')=='http://wj.qq.com/s/123'
    assert pick_copy('官网是qq.com 明天开会','','')!='http://qq.com'


def test_bark_copy_segmented_voucher():
    from bark_copy import pick_copy
    assert pick_copy("核销码：ABCD-1234-EFGH", None, "") == "ABCD-1234-EFGH"


def test_meeting_id_before_password():
    from bark_copy import pick_copy
    assert pick_copy("Zoom 会议号 8123456789 密码 abc123", None, "") == "8123456789"
    assert pick_copy("会议ID 987 654 321 密码 1234", None, "") == "987654321"


def test_period_range_without_first_unit():
    import bark
    assert bark.strip_md("下午3-5点开会") == "15-17点开会"
