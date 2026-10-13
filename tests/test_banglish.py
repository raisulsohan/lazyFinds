"""Banglish: Latin spellings of Bengali words, the per-word English or Banglish decision, and how a query is searched
in both forms."""

import pytest

from lazyfinds import banglish as B
from lazyfinds import text as T


def best(word: str) -> str:
    return B.spell(word)[0].text


def forms(word: str) -> list[str]:
    return [s.text for s in B.spell(word, 4)]


@pytest.mark.parametrize("latin, bengali", [
    # the words of the queries that failed before
    ("atoshbaji", "আতশবাজি"), ("basha", "বাসা"), ("theke", "থেকে"), ("kaj", "কাজ"), ("er", "এর"),
    ("jonmodiner", "জন্মদিনের"),
    # consonants and their digraphs
    ("khata", "খাতা"), ("ghum", "ঘুম"), ("chhobi", "ছবি"), ("theke", "থেকে"), ("bhalo", "ভাল"),
    ("khabar", "খাবার"), ("sundor", "সুন্দর"),
    # conjuncts, and two consonants that aren't one
    ("jonmo", "জন্ম"), ("nibondhon", "নিবন্ধন"), ("parke", "পার্কে"), ("porikkha", "পরীক্ষা"),
    ("shonchoy", "সঞ্চয়"), ("ongko", "অঙ্ক"),
    # a doubled letter as য-ফলা, w as ব-ফলা
    ("biddut", "বিদ্যুৎ"), ("shotti", "সত্যি"), ("bakko", "বাক্য"), ("bishshash", "বিশ্বাস"),
    # vowel signs and vowels on their own
    ("lagano", "লাগানো"), ("boi", "বই"), ("amar", "আমার"), ("kemon", "কেমন"),
    # ং ঃ ঁ ৎ য়
    ("bangla", "বাংলা"), ("rong", "রং"), ("duhkho", "দুঃখ"), ("chand", "চাঁদ"), ("hoy", "হয়"), ("khawa", "খাওয়া"),
])
def test_known_words(latin, bengali):
    assert best(latin) == T.fold(bengali)


@pytest.mark.parametrize("spellings, bengali", [
    (["atoshbaji", "atashbaji", "atoshbazi", "aatoshbaji"], "আতশবাজি"),
    (["biddut", "bidyut", "biddyut"], "বিদ্যুৎ"),
    (["chobi", "chhobi"], "ছবি"),
    (["bhalo", "valo"], "ভাল"),
    (["nibondhon", "nibondhan"], "নিবন্ধন"),
    (["porikkha", "porikkhar"], "পরীক্ষা"),
])
def test_several_spellings_of_one_word(spellings, bengali):
    for latin in spellings:
        assert best(latin).startswith(T.fold(bengali)), latin


def test_ambiguous_letters_give_several_spellings():
    assert {T.fold("বিদ্যুৎ"), T.fold("বিদ্যুত")} <= set(forms("biddut"))   # t or ৎ
    assert {T.fold("লাগানো"), T.fold("লাগান")} <= set(forms("lagano"))     # o: ো or the inherent vowel
    assert T.fold("জনম") in forms("jonmo")                                  # a conjunct or two letters
    assert len(B.spell("kaj", 4)) > 1


def test_spellings_of_real_words_are_marked_known():
    assert B.spell("atoshbaji")[0].known
    assert not B.spell("xqzv")[0].known


def test_not_latin_gives_nothing():
    assert B.spell("") == () and B.spell("৮৮") == () and B.spell("txn58") == ()


def decide(query: str) -> list[bool]:
    return B.decide(T.split_words(T.fold(query)))


@pytest.mark.parametrize("query", [
    "a bicycle in the park", "fireworks at night", "washing machine won't drain", "penguins in a video",
    "rat in the kitchen", "dim light", "bill payment", "birthday cake", "how volcanoes form", "TXN-58K3Q",
    "skiing in the snowy mountains", "can I work from home", "mountain lake at dusk",
])
def test_english_stays_english(query):
    assert not any(decide(query))
    assert B.convert(query) is None


def test_banglish_words_are_found_per_word():
    assert decide("atoshbaji") == [True]
    assert decide("parke cycle") == [True, False]                   # cycle stays English
    assert decide("jonmodiner cake") == [True, False]
    assert decide("basha theke office er kaj") == [True, True, False, True, True]
    assert decide("rat er akash") == [True, True, True]             # rat is রাত with Banglish around it
    assert decide("ami kaj") == [True, True]                        # everyday Banglish on its own
    assert decide("rate atoshbaji") == [True, True]                 # rat + e: রাতে
    assert decide("kemon acho") == [True, True]                     # acho is a rare English word
    assert decide("জন্ম nibondhon") == [False, True]


def test_english_words_with_english_endings():
    assert B.english("penguins") and B.english("skiing") and B.english("sunflowers")
    assert B.english("atoshbaji") is None


def test_conversion_keeps_english_words():
    c = B.convert("basha theke office er kaj")
    assert c.shown == "বাসা থেকে office এর কাজ"
    assert c.words["basha"][0] == T.fold("বাসা")
    assert B.convert("parke cycle").shown == "পার্কে cycle"


def test_rare_english_words_need_banglish_around_them():
    for query in ("pounce", "sinew", "depute", "photos of the pounce at night"):
        assert B.convert(query) is None, query
    assert "acho" in B.convert("kemon acho").words


def test_name_queries_have_bengali_forms_with_and_without_composed_letters():
    queries = B.name_queries("bashay", B.convert("bashay"))
    assert "বাসা\u09df" in queries                   # য় as one letter, as a keyboard may type it
    assert "বাসা\u09af\u09bc" in queries             # য় as য and a nukta, as lazyFinds folds it
