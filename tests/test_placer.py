import pytest

from abs_ebook_filler.core import placer
from abs_ebook_filler.core.config import Settings


def test_path_mapping_longest_prefix():
    s = Settings(path_map="/audiobooks:/library, /audiobooks/kids:/kids", _env_file=None)
    m = s.path_mappings
    assert placer.map_path("/audiobooks/Author/Book", m) == "/library/Author/Book"
    assert placer.map_path("/audiobooks/kids/Book", m) == "/kids/Book"
    assert placer.map_path("/audiobooksX/Book", m) == "/audiobooksX/Book"  # no partial-segment match
    assert placer.map_path("/other/Book", m) == "/other/Book"
    assert placer.map_path("/audiobooks", m) == "/library"


def test_safe_filename():
    assert placer.safe_filename("The Way of Kings", "Brandon Sanderson") == "The Way of Kings - Brandon Sanderson.epub"
    assert placer.safe_filename('What: "If"?', "A/B") == "What If - AB.epub"
    assert placer.safe_filename("", "") == "Unknown.epub"
    assert len(placer.safe_filename("x" * 500)) <= 185


def test_prepare_and_commit(tmp_path):
    (tmp_path / "01.m4b").write_bytes(b"audio")
    final, temp = placer.prepare_target(str(tmp_path), "Book - Author.epub")
    temp.write_bytes(b"epub-data")
    placed = placer.commit(temp, final)
    assert placed.read_bytes() == b"epub-data"
    assert not temp.exists()
    with pytest.raises(placer.PlacementError):
        placer.prepare_target(str(tmp_path), "Book - Author.epub")  # never overwrite


def test_commit_rejects_empty(tmp_path):
    final, temp = placer.prepare_target(str(tmp_path), "x.epub")
    temp.write_bytes(b"")
    with pytest.raises(placer.PlacementError):
        placer.commit(temp, final)
    assert not final.exists()


def test_missing_folder(tmp_path):
    with pytest.raises(placer.PlacementError):
        placer.prepare_target(str(tmp_path / "nope"), "x.epub")
