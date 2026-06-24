# -*- coding: utf-8 -*-
"""CryptoPanic news fetcher and ChromaDB cache integration."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CRYPTOPANIC_COLLECTION = "cryptopanic_news"
DEFAULT_CRYPTOPANIC_SESSION = "cpnews"
DEFAULT_CRYPTOPANIC_URL = "https://cryptopanic.com/"
DEFAULT_CRYPTOPANIC_MAX_AGE_HOURS = 24
DEFAULT_CRYPTOPANIC_REFRESH_INTERVAL_SECONDS = 900


@dataclass(frozen=True)
class CryptoPanicNewsItem:
    title: str
    source: str = ""
    time_ago: str = ""
    coins: Tuple[str, ...] = ()
    fetch_time: Optional[str] = None


@dataclass(frozen=True)
class CryptoPanicNewsResult:
    items: List[CryptoPanicNewsItem]
    provider: str
    error_message: Optional[str] = None
    fetched: bool = False
    cache_used: bool = False

    @property
    def success(self) -> bool:
        return bool(self.items)


def default_chroma_path() -> str:
    """Return the default local ChromaDB path used by the previous Hermes task."""
    return str(Path.home() / "AppData" / "Local" / "hermes" / "chroma_data")


def _resolve_opencli_path(configured_path: Optional[str] = None) -> str:
    if configured_path:
        return configured_path
    npm_global = Path.home() / "AppData" / "Roaming" / "npm" / "opencli.cmd"
    if npm_global.is_file():
        return str(npm_global)
    return "opencli"


class CryptoPanicNewsService:
    """Read CryptoPanic BTC news from the local ChromaDB cache."""

    _NEWS_RE = re.compile(r"\[(\d+\s*min|\d+\s*h)\]\([^)]+\)\s*\[([^\]]+)\]\([^)]+\)")
    _COIN_RE = re.compile(r"\[([A-Z0-9]{2,6})\]\(/news/[a-z][a-z0-9-]*/\)")
    _SOURCE_RE = re.compile(r"\s+([\w.-]+\.\w{2,})(?:\s+\d+\s*sources?)?$")
    _NON_COINS = {"US", "NOW", "RWA", "A", "B", "4", "67", "ETF"}

    def __init__(
        self,
        *,
        chroma_path: Optional[str] = None,
        collection_name: str = DEFAULT_CRYPTOPANIC_COLLECTION,
        opencli_path: Optional[str] = None,
        session: str = DEFAULT_CRYPTOPANIC_SESSION,
        url: str = DEFAULT_CRYPTOPANIC_URL,
        max_age_hours: int = DEFAULT_CRYPTOPANIC_MAX_AGE_HOURS,
        refresh_interval_seconds: int = DEFAULT_CRYPTOPANIC_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self.chroma_path = chroma_path or default_chroma_path()
        self.collection_name = collection_name or DEFAULT_CRYPTOPANIC_COLLECTION
        self.opencli_path = _resolve_opencli_path(opencli_path)
        self.session = session or DEFAULT_CRYPTOPANIC_SESSION
        self.url = url or DEFAULT_CRYPTOPANIC_URL
        self.max_age_hours = max(1, int(max_age_hours or DEFAULT_CRYPTOPANIC_MAX_AGE_HOURS))
        self.refresh_interval_seconds = max(0, int(refresh_interval_seconds or 0))
        self._last_fetch_attempt_at: Optional[float] = None

    def get_latest_news(self, *, coin: str = "BTC", limit: int = 5) -> CryptoPanicNewsResult:
        """Fetch fresh CryptoPanic news, falling back to ChromaDB cache only."""
        coin = (coin or "BTC").strip().upper()
        limit = max(1, limit)
        errors: List[str] = []

        try:
            cached_first = self.read_from_chroma(coin=coin, limit=limit)
        except Exception as exc:
            cached_first = []
            errors.append(f"cache read failed: {exc}")
            logger.warning("[CryptoPanic] 读取 ChromaDB 缓存失败: %s", exc)
        if cached_first:
            return CryptoPanicNewsResult(
                items=cached_first,
                provider="CryptoPanicChroma",
                cache_used=True,
            )

        try:
            stale_cached = self.read_from_chroma(coin=coin, limit=limit, allow_stale=True)
        except Exception as exc:
            stale_cached = []
            errors.append(f"stale cache read failed: {exc}")
            logger.warning("[CryptoPanic] 读取过期 ChromaDB 缓存失败: %s", exc)
        if stale_cached:
            warning = "ChromaDB 无新鲜 CryptoPanic 缓存，已使用过期缓存"
            if errors:
                warning = f"{warning}；{'；'.join(errors)}"
            return CryptoPanicNewsResult(
                items=stale_cached,
                provider="CryptoPanicChromaStale",
                cache_used=True,
                error_message=warning,
            )

        return CryptoPanicNewsResult(
            items=[],
            provider="CryptoPanicChroma",
            error_message="；".join(errors) if errors else "ChromaDB 无可用 CryptoPanic 缓存",
        )

    @classmethod
    def parse_news(cls, markdown: str) -> List[CryptoPanicNewsItem]:
        matches = list(cls._NEWS_RE.finditer(markdown or ""))
        items: List[CryptoPanicNewsItem] = []
        for idx, match in enumerate(matches):
            time_ago = match.group(1).replace(" ", "")
            raw_title = match.group(2).strip()
            if len(raw_title) < 10 or raw_title.lower() in {"news", "rising", "get plus", "sign in"}:
                continue

            clean_title = re.sub(r"\s+\d+\s*sources?$", "", raw_title)
            source_match = cls._SOURCE_RE.search(clean_title)
            if source_match:
                title = clean_title[: source_match.start()].strip()
                source = source_match.group(1)
            else:
                title = clean_title
                source = ""
            if len(title) > 200:
                title = title[:197] + "..."

            next_start = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown)
            tail = markdown[match.end() : next_start]
            coins = cls._clean_coins(cls._COIN_RE.findall(tail))
            items.append(
                CryptoPanicNewsItem(
                    title=title,
                    source=source,
                    time_ago=time_ago,
                    coins=tuple(coins),
                )
            )
        return items

    @classmethod
    def _clean_coins(cls, coins: Iterable[str]) -> List[str]:
        seen = set()
        cleaned: List[str] = []
        for coin in coins:
            normalized = (coin or "").strip().upper()
            if not normalized or normalized in seen or normalized in cls._NON_COINS:
                continue
            seen.add(normalized)
            cleaned.append(normalized)
        return cleaned[:5]

    @staticmethod
    def deduplicate(items: Sequence[CryptoPanicNewsItem]) -> List[CryptoPanicNewsItem]:
        seen = set()
        result: List[CryptoPanicNewsItem] = []
        for item in items:
            key = item.title[:40].lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    def save_to_chroma(self, items: Sequence[CryptoPanicNewsItem]) -> Tuple[int, int]:
        if not items:
            return 0, 0
        collection = self._get_collection()
        now_iso = datetime.now().isoformat(timespec="seconds")
        ids: List[str] = []
        documents: List[str] = []
        metadatas: List[Dict[str, str]] = []
        for item in items:
            doc_id = hashlib.md5(item.title.encode("utf-8")).hexdigest()
            ids.append(doc_id)
            documents.append(item.title)
            metadatas.append(
                {
                    "title": item.title,
                    "source": item.source,
                    "time_ago": item.time_ago,
                    "coins": ",".join(item.coins),
                    "fetch_time": item.fetch_time or now_iso,
                }
            )

        existing = collection.get(ids=ids, include=[])
        existing_ids = set(existing.get("ids", []))
        collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        return len(ids) - len(existing_ids), collection.count()

    def read_from_chroma(
        self,
        *,
        coin: str = "BTC",
        limit: int = 5,
        allow_stale: bool = False,
    ) -> List[CryptoPanicNewsItem]:
        collection = self._get_collection()
        cutoff = datetime.now() - timedelta(hours=self.max_age_hours)
        try:
            fetch_limit = max(limit * 20, 100)
            try:
                fetch_limit = min(max(collection.count(), fetch_limit), 1000)
            except Exception:
                pass
            raw = collection.get(limit=fetch_limit, include=["documents", "metadatas"])
        except Exception as exc:
            logger.warning("[CryptoPanic] 读取 ChromaDB 失败: %s", exc)
            return []

        rows: List[Tuple[datetime, CryptoPanicNewsItem]] = []
        documents = raw.get("documents") or []
        metadatas = raw.get("metadatas") or []
        for document, metadata in zip(documents, metadatas):
            item = self._item_from_chroma(document, metadata or {})
            if not item.title:
                continue
            if not self._item_matches_coin(item, coin):
                continue
            fetched_at = self._parse_fetch_time(item.fetch_time)
            if fetched_at is None:
                continue
            if not allow_stale and fetched_at < cutoff:
                continue
            rows.append((fetched_at, item))

        rows.sort(key=lambda row: row[0], reverse=True)
        return [item for _, item in rows[:limit]]

    def _get_collection(self) -> Any:
        try:
            import chromadb
        except ImportError as exc:
            raise RuntimeError("chromadb 未安装，请运行 pip install chromadb") from exc

        os.makedirs(self.chroma_path, exist_ok=True)
        client = chromadb.PersistentClient(path=self.chroma_path)
        return client.get_or_create_collection(
            name=self.collection_name,
            metadata={"description": "CryptoPanic crypto news"},
        )

    @classmethod
    def _item_from_chroma(cls, document: str, metadata: Dict[str, Any]) -> CryptoPanicNewsItem:
        coins = cls._clean_coins(str(metadata.get("coins") or "").split(","))
        return CryptoPanicNewsItem(
            title=str(metadata.get("title") or document or "").strip(),
            source=str(metadata.get("source") or "").strip(),
            time_ago=str(metadata.get("time_ago") or "").strip(),
            coins=tuple(coins),
            fetch_time=str(metadata.get("fetch_time") or "").strip() or None,
        )

    @staticmethod
    def _parse_fetch_time(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed

    @staticmethod
    def _item_matches_coin(item: CryptoPanicNewsItem, coin: str) -> bool:
        target = (coin or "BTC").strip().upper()
        if item.coins:
            return target in {c.upper() for c in item.coins}
        text = item.title.lower()
        if target == "BTC":
            return "bitcoin" in text or "btc" in text or "比特币" in text
        return target.lower() in text

    def _filter_items(
        self,
        items: Sequence[CryptoPanicNewsItem],
        *,
        coin: str,
        limit: int,
    ) -> List[CryptoPanicNewsItem]:
        filtered = [item for item in items if self._item_matches_coin(item, coin)]
        return filtered[:limit]


def build_cryptopanic_news_service_from_env() -> CryptoPanicNewsService:
    return CryptoPanicNewsService(
        chroma_path=os.getenv("CRYPTOPANIC_CHROMA_PATH") or default_chroma_path(),
        collection_name=os.getenv("CRYPTOPANIC_CHROMA_COLLECTION", DEFAULT_CRYPTOPANIC_COLLECTION),
        opencli_path=os.getenv("CRYPTOPANIC_OPENCLI_PATH") or None,
        session=os.getenv("CRYPTOPANIC_OPENCLI_SESSION", DEFAULT_CRYPTOPANIC_SESSION),
        max_age_hours=int(os.getenv("CRYPTOPANIC_MAX_AGE_HOURS", DEFAULT_CRYPTOPANIC_MAX_AGE_HOURS)),
        refresh_interval_seconds=int(
            os.getenv(
                "CRYPTOPANIC_REFRESH_INTERVAL_SECONDS",
                DEFAULT_CRYPTOPANIC_REFRESH_INTERVAL_SECONDS,
            )
        ),
    )


def main() -> int:
    service = build_cryptopanic_news_service_from_env()
    try:
        result = service.get_latest_news(coin="BTC", limit=25)
    except Exception as exc:
        print(f"获取 CryptoPanic 新闻失败: {exc}", file=sys.stderr)
        return 1

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"CryptoPanic 加密新闻速报 ({now})")
    print("=" * 45)
    for idx, item in enumerate(result.items, 1):
        coins = f" [{','.join(item.coins)}]" if item.coins else ""
        source = f" ({item.source})" if item.source else ""
        time_ago = f"[{item.time_ago}] " if item.time_ago else ""
        print(f"{idx}. {time_ago}{coins} {item.title}{source}")
    if not result.items:
        print(result.error_message or "未获取到新闻内容")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
