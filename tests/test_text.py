from lazyfinds import code as C
from lazyfinds import text as T


def test_fold_makes_bengali_digits_ascii_and_drops_invisible_marks():
    assert T.fold("হিসাব নং ৮৮১২৭৫") == "হিসাব নং 881275"
    assert T.fold("Re\u200bport\u200d") == "report"


def test_query_terms_drop_stopwords_and_kind_words():
    assert T.query_terms("penguins in a video") == ["penguins"]
    assert T.query_terms("আমার জন্মদিনের ছবি") == ["জন্মদিনের"]
    assert T.kinds_asked("penguins in a video") == {"video"}
    assert T.kinds_asked("আতশবাজির ভিডিও") == {"video"}


def test_bengali_endings():
    assert T.bengali_stem("টমেটোর") == "টমেটো"
    assert T.bengali_stem("চারাগুলো") == "চারা"
    assert T.fts_term("টমেটোর") == '"টমেটো"*'


def test_code_like_words():
    assert T.is_code_like("txn") is False and T.is_code_like("58k3q") and T.is_code_like("881275")


def test_passages_break_at_sentences_and_overlap_by_one():
    text = " ".join(f"Sentence number {i} is here." for i in range(200))
    parts = T.passages(text, size=300)
    assert all(len(p) <= 300 for p in parts)
    assert all(p.endswith(".") for p in parts)
    assert parts[1].split(". ")[0] + "." in parts[0]   # the last sentence of one starts the next


def test_mark_finds_whole_words():
    line = T.mark("The drain pump filter drains slowly.", ["drain"])
    assert "«drain»" in line and "«drains»" in line


def test_code_names_split():
    assert C.split_name("refreshAccessToken") == ["refresh", "access", "token"]
    assert C.split_name("HTTPServer") == ["http", "server"]
    assert C.split_name("read_csv") == ["read", "csv"]


def test_code_blocks_start_at_definitions():
    lines = ["import os", "", "@cache", "def a():", "    return 1", "", "class B:", "    pass"]
    assert C.blocks(lines) == [(1, 2), (3, 6), (7, 8)]
    body = "def refresh_token(user):\n    pass"
    assert C.definition_line("refresh_token", body, 10) == 10
