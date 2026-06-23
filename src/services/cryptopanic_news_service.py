# -*- coding: utf-8 -*-
"""CryptoPanic news fetcher and ChromaDB cache integration."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
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
    """Fetch CryptoPanic news via opencli and store/read it in ChromaDB."""

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
            logger.warning("[CryptoPanic] 优先读取 ChromaDB 缓存失败，将尝试抓取: %s", exc)
        if cached_first:
            return CryptoPanicNewsResult(
                items=cached_first,
                provider="CryptoPanicChroma",
                cache_used=True,
            )

        if self._should_attempt_fetch():
            self._last_fetch_attempt_at = time.monotonic()
            try:
                items = self.fetch_news()
                if items:
                    self.save_to_chroma(items)
                    filtered = self._filter_items(items, coin=coin, limit=limit)
                    if filtered:
                        return CryptoPanicNewsResult(
                            items=filtered,
                            provider="CryptoPanic",
                            fetched=True,
                        )
            except Exception as exc:
                errors.append(f"fetch failed: {exc}")
                logger.warning("[CryptoPanic] 抓取失败，将尝试读取 ChromaDB 缓存: %s", exc)
        else:
            logger.info("[CryptoPanic] 距离上次抓取不足 %ss，直接读取 ChromaDB 缓存", self.refresh_interval_seconds)

        try:
            cached = self.read_from_chroma(coin=coin, limit=limit)
        except Exception as exc:
            cached = []
            errors.append(f"cache read failed: {exc}")
            logger.warning("[CryptoPanic] 回退读取 ChromaDB 缓存失败: %s", exc)
        if cached:
            return CryptoPanicNewsResult(
                items=cached,
                provider="CryptoPanicChroma",
                cache_used=True,
                error_message="；".join(errors) if errors else None,
            )

        return CryptoPanicNewsResult(
            items=[],
            provider="CryptoPanic",
            error_message="；".join(errors) if errors else "CryptoPanic 抓取失败且 ChromaDB 无可用缓存",
        )

    def _should_attempt_fetch(self) -> bool:
        if self.refresh_interval_seconds <= 0 or self._last_fetch_attempt_at is None:
            return True
        return time.monotonic() - self._last_fetch_attempt_at >= self.refresh_interval_seconds

    def fetch_news(self) -> List[CryptoPanicNewsItem]:
        markdown = self._fetch_content()
        items = self.parse_news(markdown)
        return self.deduplicate(items)

    def _run_opencli(self, args: Sequence[str], *, timeout: int = 90) -> Optional[Any]:
        cmd = [self.opencli_path, *args]
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("[CryptoPanic] opencli command timed out: %s", " ".join(args))
            return None
        except FileNotFoundError:
            logger.warning("[CryptoPanic] opencli not found: %s", self.opencli_path)
            return None

        if completed.returncode != 0:
            logger.warning(
                "[CryptoPanic] opencli %s failed: %s",
                " ".join(args),
                completed.stderr.strip(),
            )
            return None

        text = completed.stdout.strip()
        if not text:
            return ""
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text

    def _fetch_content(self) -> str:
        opened = self._run_opencli(["browser", self.session, "open", self.url], timeout=20)
        if not isinstance(opened, dict):
            raise RuntimeError("failed to open CryptoPanic via opencli")

        tab_id = opened.get("page") or ""
        if not tab_id:
            raise RuntimeError("opencli did not return a page id")

        poll_js = (
            "(async()=>{"
            "for(let i=0;i<15;i++){"
            "const len=document.body.innerText.length;"
            "if(len>5000)return JSON.stringify({ok:true,len:len});"
            "await new Promise(r=>setTimeout(r,2000))"
            "}"
            "return JSON.stringify({ok:false,len:document.body.innerText.length})"
            "})()"
        )

        time.sleep(3)
        self._run_opencli(["browser", self.session, "eval", "--tab", tab_id, poll_js], timeout=20)
        extracted = self._run_opencli(["browser", self.session, "extract", "--tab", tab_id], timeout=20)
        self._run_opencli(["browser", self.session, "close"], timeout=10)

        if not isinstance(extracted, dict):
            raise RuntimeError("failed to extract CryptoPanic content")
        content = extracted.get("content", "")
        if len(content) < 5000 or "安全验证" in content:
            raise RuntimeError(f"CryptoPanic content is not ready ({len(content)} chars)")
        return content

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

    def read_from_chroma(self, *, coin: str = "BTC", limit: int = 5) -> List[CryptoPanicNewsItem]:
        collection = self._get_collection()
        cutoff = datetime.now() - timedelta(hours=self.max_age_hours)
        try:
            raw = collection.get(limit=max(limit * 5, 25), include=["documents", "metadatas"])
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
            if fetched_at is None or fetched_at < cutoff:
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
