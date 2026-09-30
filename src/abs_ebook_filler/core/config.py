from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Audiobookshelf
    abs_url: str = "http://audiobookshelf:80"
    abs_token: str = ""
    abs_library_ids: str = ""  # comma-separated; empty = all book libraries

    # Shelfmark
    shelfmark_url: str = "http://shelfmark:8084"
    shelfmark_api_key: str = ""
    shelfmark_books_dir: str = ""  # optional local mount of Shelfmark's /books

    # Filesystem: "container_prefix:local_prefix[,more...]"
    path_map: str = ""

    # Behaviour
    min_score: int = 0
    max_candidates: int = 8
    poll_interval: float = 3.0
    # Clock only runs while Shelfmark is working on a download; time waiting in Shelfmark's own
    # queue (it runs MAX_CONCURRENT_DOWNLOADS at once) is limited separately by queue_wait_timeout.
    download_timeout: float = 600.0
    queue_wait_timeout: float = 3600.0
    # Books downloading at once, overall and per source. Anna's Archive (direct_download) rate-limits
    # hard, so it stays at 1; torrents/usenet via Prowlarr can run side by side. Shelfmark's own
    # MAX_CONCURRENT_DOWNLOADS must be at least download_concurrency for this to help.
    download_concurrency: int = 3
    direct_download_concurrency: int = 1
    worker_concurrency: int = 0  # legacy name for download_concurrency; used if set
    # Automatic re-queue of a download that failed on a rate limit, after the cooldown.
    rate_limit_retries: int = 2
    http_timeout: float = 60.0
    # Release searches hit every enabled source; Shelfmark's own budget is release_search_timeout (300s).
    search_timeout: float = 330.0
    # Max release searches at once. Keep at 1: Anna's Archive rate-limits (429) parallel searches.
    search_concurrency: int = 1
    # Saved search results (pre-search and normal searches) are reused for this long.
    search_cache_hours: float = 24.0
    # Pre-search: pause between books, and how many books the web UI offers by default.
    presearch_delay: float = 5.0
    presearch_count: int = 100

    @property
    def search_cache_seconds(self) -> float:
        return self.search_cache_hours * 3600

    @property
    def total_download_slots(self) -> int:
        return max(1, self.worker_concurrency or self.download_concurrency)

    def source_limit(self, source: str) -> int:
        """Max simultaneous downloads for one release source."""
        if (source or "").lower() == "direct_download":
            return max(1, min(self.direct_download_concurrency, self.total_download_slots))
        return self.total_download_slots

    # Web
    web_user: str = "admin"
    web_password: str = ""
    web_port: int = 8090

    # State
    data_dir: str = Field(default="/data")

    @property
    def library_ids(self) -> list[str]:
        return [x.strip() for x in self.abs_library_ids.split(",") if x.strip()]

    @property
    def path_mappings(self) -> list[tuple[str, str]]:
        pairs = []
        for chunk in self.path_map.split(","):
            chunk = chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            src, dst = chunk.split(":", 1)
            pairs.append((src.rstrip("/") or "/", dst.rstrip("/") or "/"))
        # Longest prefix first so nested mappings win.
        return sorted(pairs, key=lambda p: len(p[0]), reverse=True)

    def redacted(self) -> dict:
        out = self.model_dump()
        for k in ("abs_token", "shelfmark_api_key", "web_password"):
            if out.get(k):
                out[k] = "********"
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
