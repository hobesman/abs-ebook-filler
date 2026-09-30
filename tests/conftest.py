import pytest

from abs_ebook_filler.core.config import Settings
from abs_ebook_filler.core.service import Service
from abs_ebook_filler.core.state import State


@pytest.fixture
def library(tmp_path):
    """A fake ABS library folder with one audiobook, visible locally at <tmp>/library."""
    book_dir = tmp_path / "library" / "Brandon Sanderson" / "The Way of Kings"
    book_dir.mkdir(parents=True)
    (book_dir / "01.m4b").write_bytes(b"audio")
    return tmp_path / "library"


@pytest.fixture
def settings(tmp_path, library):
    return Settings(
        _env_file=None,
        abs_url="http://abs",
        abs_token="tok",
        shelfmark_url="http://sm",
        shelfmark_api_key="key",
        path_map=f"/audiobooks:{library.as_posix()}",
        data_dir=str(tmp_path / "data"),
        poll_interval=0,
        download_timeout=5,
        web_password="pw",
    )


@pytest.fixture
def service(settings):
    svc = Service(settings, state=State(settings.data_dir + "/state.db"))
    yield svc
    svc.state.close()
