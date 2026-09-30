import pytest

from abs_ebook_filler.core.matching import build_query, clean_title, primary_author, score


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Rule 1: number in square brackets
        ("Mistborn [2] The Well of Ascension", "The Well of Ascension"),
        ("[03] The Hero of Ages", "The Hero of Ages"),
        ("Series [1.5] Novella Title", "Novella Title"),
        # Rule 2: number, space-dash-space
        ("Stormlight Archive 1 - The Way of Kings", "The Way of Kings"),
        ("Book 01 - Title", "Title"),
        ("The Expanse 4 - Cibola Burn", "Cibola Burn"),
        # Rule 3: number, dash (no space)
        ("Expanse 3-Abaddon's Gate", "Abaddon's Gate"),
        ("Dresden Files 07-Dead Beat", "Dead Beat"),
        # Rule 4: standalone two-digit number
        ("Discworld 01 The Colour of Magic", "The Colour of Magic"),
        ("Harry Potter 07 Deathly Hallows", "Deathly Hallows"),
        ("Discworld 01 The 13 Clocks", "The 13 Clocks"),  # first two-digit number wins
        # Tags stripped
        ("The Martian (Unabridged)", "The Martian"),
        ("Dune 01 - Dune [Unabridged]", "Dune"),
        # Must not change
        ("Catch-22", "Catch-22"),
        ("Hitchhiker's Guide 42", "Hitchhiker's Guide 42"),
        ("1984", "1984"),
        ("2001: A Space Odyssey", "2001: A Space Odyssey"),
        ("Fahrenheit 451", "Fahrenheit 451"),
        ("11/22/63", "11/22/63"),
        ("Project Hail Mary", "Project Hail Mary"),
        ("Book 3-", "Book 3"),  # nothing after the dash -> rule skipped; trailing dash trimmed
        ("", ""),
    ],
)
def test_clean_title(raw, expected):
    assert clean_title(raw) == expected


def test_rule_order_brackets_before_dash():
    # Bracket rule runs first, then the " - " rule sees no number before its dash.
    assert clean_title("Saga [2] Part - Two") == "Part - Two"


def test_last_match_used_for_space_dash():
    assert clean_title("Cosmere 3 - Mistborn 2 - The Well of Ascension") == "The Well of Ascension"


def test_primary_author_and_query():
    assert primary_author("Terry Pratchett, Neil Gaiman") == "Terry Pratchett"
    assert primary_author("A & B") == "A"
    assert build_query("Good Omens", "Terry Pratchett, Neil Gaiman") == "Good Omens Terry Pratchett"
    assert build_query("Solo", "") == "Solo"


def test_score_prefers_right_book():
    good = score("The Way of Kings", "Brandon Sanderson", "The Way of Kings",
                 "Stormlight Archive 1 - The Way of Kings", "Brandon Sanderson")
    bad = score("Words of Radiance", "Brandon Sanderson", "The Way of Kings",
                "Stormlight Archive 1 - The Way of Kings", "Brandon Sanderson")
    assert good >= 95
    assert bad < good


def test_score_penalises_extra_words_but_not_subtitles():
    # Real Shelfmark results from the probe for "Dune" / "Frank Herbert".
    def s(t, a="Frank Herbert"):
        return score(t, a, "Dune", "Dune", "Frank Herbert")

    exact = s("Dune", "Herbert, Frank")
    assert exact == 100
    assert s("Dune : Now a major new film from the director of Blade Runner 2049 and Arrival") >= 95
    for other in ("God Emperor of Dune", "Dune volume 2: The Great Schools of Dune",
                  "Chapterhouse: Dune", "Dune Collection: All Books"):
        assert s(other) < exact - 15, other
    assert s("Dune", "Kevin J Anderson, Brian Herbert, Frank Herbert") >= 95


def test_score_without_author_uses_title_only():
    assert score("Dune", "", "Dune", "Dune", "Frank Herbert") == 100
