
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
