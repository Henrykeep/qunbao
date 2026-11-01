
def test_group_no_spaced():
    from bark_copy import pick_copy
    assert pick_copy('群号：123 456 789','','')=='123456789'
