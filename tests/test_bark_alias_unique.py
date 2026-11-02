import collections, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "digest"))
import bark


def test_alias_no_cross_placeholder_duplicates():
    """同一别名只能归一个占位，否则先匹配的表项悄悄吞掉后者。"""
    seen = collections.defaultdict(set)
    for k, v in bark.EN_ALIAS.items():
        for a in v.split("|"):
            if "(?:" in v.split("|")[0] or "\\s" in v.split("|")[0] or a in ("Video", "Voice", "Group", "Missed", "Meet"):
                continue
            seen[a.lower()].add(k)
    assert not {a: ks for a, ks in seen.items() if len(ks) > 1}
