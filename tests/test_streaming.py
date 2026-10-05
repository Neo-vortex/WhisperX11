from whisperx11.streaming import SegmentAgreement


def feed(previews, final):
    a = SegmentAgreement()
    typed = [a.preview(p) for p in previews]
    return [t for t in typed if t], a.final(final)


def test_commits_agreed_words_and_types_only_the_rest():
    typed, actions = feed(["The quick.", "The quick brown fox.", "The quick brown fox jumps over"],
                          "The quick brown fox jumps over the lazy dog.")
    assert typed == ["The ", "quick brown "]
    assert actions == [("text", "fox jumps over the lazy dog. ")]


def test_punctuation_differences_do_not_break_agreement():
    typed, actions = feed(["Hello, world how", "Hello world, how are you"], "Hello, world, how are you?")
    assert typed == ["Hello world, "]
    assert actions == [("text", "how are you? ")]


def test_final_disagreeing_with_committed_text_is_replaced():
    typed, actions = feed(["Send the report to", "Send the report to the"], "Sent a report to the team.")
    assert typed == ["Send the report "]
    assert actions == [("backspace", len("Send the report ")), ("text", "Sent a report to the team. ")]


def test_nothing_committed_types_final():
    typed, actions = feed(["Hi"], "Hi there.")
    assert typed == []
    assert actions == [("text", "Hi there. ")]


def test_persian_with_zwnj():
    p1 = "امروز جلسه‌ی تیم"
    p2 = "امروز جلسه‌ی تیم ساعت ده"
    typed, actions = feed([p1, p2], "امروز جلسه‌ی تیم ساعت ده صبح برگزار شد.")
    assert typed == ["امروز جلسه‌ی "]
    assert actions == [("text", "تیم ساعت ده صبح برگزار شد. ")]


def test_empty_final_after_commit_erases():
    typed, actions = feed(["uh huh yes", "uh huh yes"], "")
    assert typed == ["uh huh "]
    assert actions == [("backspace", 7)]
