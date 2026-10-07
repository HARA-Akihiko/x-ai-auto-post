import asyncio
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Article, Base, DraftSource, DraftStatus, PostDraft, Source, utcnow
from app.services.articles import (
    ArticleAnalyzer, ArticleCollector, MAX_FEED_BYTES, candidates, canonicalize_url,
)
from app.services.auth import ServiceError


@pytest.fixture
def factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def article(index=1, **values):
    fields = dict(
        canonical_url=f"https://example.com/{index}", url=f"https://example.com/{index}",
        title=f"New AI model {index}", source_name="OpenAI", summary="Useful AI inference SDK",
        content_hash=hashlib.sha256(str(index).encode()).hexdigest(), published_at=utcnow(), score=index,
    )
    fields.update(values)
    return Article(**fields)


def test_canonical_url_removes_tracking_and_normalizes_host():
    assert canonicalize_url("HTTPS://Example.COM:443/item?b=2&utm_source=x&a=1#news") == (
        "https://example.com/item?a=1&b=2"
    )
    for url in ("javascript:alert(1)", "file:///etc/passwd", "https://user@example.com",
                "https://example.com/hi\n", "https://example.com/%00", "https://example.com/%xx"):
        with pytest.raises(ValueError):
            canonicalize_url(url)


def test_collector_is_bounded_deduplicates_and_isolates_failures(factory):
    with factory() as session:
        session.add_all([
            Source(name="OpenAI", url="https://openai.com/news/rss.xml", priority=50),
            Source(name="GitHub", url="https://github.blog/feed/", priority=20),
            Source(name="User input", url="https://attacker.example/feed"),
        ])
        session.commit()
    rss = b"""<rss version="2.0"><channel>
      <item><title>AI model release</title><link>https://example.com/a?utm_source=rss</link>
      <description>&lt;p&gt;Useful &lt;b&gt;inference&lt;/b&gt;&lt;/p&gt;</description>
      <pubDate>Wed, 07 Oct 2026 06:00:00 GMT</pubDate></item>
      <item><title>AI model release</title><link>https://example.com/b</link>
      <description>&lt;p&gt;Useful &lt;b&gt;inference&lt;/b&gt;&lt;/p&gt;</description></item>
      <item><title>other</title><link>javascript:alert(1)</link></item>
      </channel></rss>"""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "openai.com" in str(request.url):
            return httpx.Response(503)
        return httpx.Response(200, content=rss)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            collector = ArticleCollector(SimpleNamespace(), client)
            with factory() as session:
                assert len(await collector.collect(session)) == 1
                assert await collector.collect(session) == []
                rows = session.scalars(select(Article)).all()
                assert len(rows) == 1
                assert rows[0].canonical_url == "https://example.com/a"
                assert rows[0].summary == "Useful inference"
    asyncio.run(run())
    assert not any("attacker" in url for url in seen)


def test_collector_rejects_oversized_feed(factory):
    with factory() as session:
        session.add(Source(name="GitHub", url="https://github.blog/feed/"))
        session.commit()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * (MAX_FEED_BYTES + 1))
        )) as client:
            with factory() as session:
                assert await ArticleCollector(SimpleNamespace(), client).collect(session) == []
    asyncio.run(run())


def test_candidates_are_recent_relevant_bounded_and_not_inflight(factory):
    with factory() as session:
        session.add_all([
            article(1), article(2, published_at=utcnow() - timedelta(days=8)),
            article(3, published_at=utcnow() + timedelta(days=1)),
            article(4, title="Gardening", summary="Flowers"), article(5), article(6), article(7),
        ])
        draft = PostDraft(slot="inflight", text="text", status=DraftStatus.PUBLISHING)
        session.add(draft)
        session.flush()
        busy = session.scalar(select(Article).where(Article.canonical_url == "https://example.com/7"))
        session.add(DraftSource(draft_id=draft.id, article_id=busy.id))
        session.commit()
        rows = candidates(session, SimpleNamespace(article_max_age_days=7, candidate_limit=2))
        assert [row.canonical_url for row in rows] == ["https://example.com/6", "https://example.com/5"]


def test_general_ai_news_excluded_but_developer_themes_are_relevant(factory):
    with factory() as session:
        session.add_all([
            article(1, title="AI model creates paintings", summary="Diffusion art is beautiful", score=999),
            article(2, title="ClaudeCode developer release", summary="Coding tools", score=50),
            article(3, title="Codex CLI update", summary="Developer workflow", score=40),
            article(4, title="MCP context tools", summary="Agentic IDE integrations", score=30),
            article(5, title="AI Copilot", summary="GitHub API and token context", score=20),
        ])
        session.commit()
        selected = candidates(session, SimpleNamespace(article_max_age_days=7, candidate_limit=5))
        assert [row.canonical_url for row in selected] == [
            "https://example.com/2", "https://example.com/3",
            "https://example.com/4", "https://example.com/5",
        ]


class Responses:
    def __init__(self, value):
        self.value = value
        self.prompts = []

    async def complete(self, prompt):
        self.prompts.append(prompt)
        return self.value


def test_analyzer_validates_ids_scores_and_skips_low_value():
    rows = [article(1), article(2)]
    for index, row in enumerate(rows, 1):
        row.id = index
    ranks = [
        dict(id=1, importance=8, novelty=8, utility=8, post_value=8),
        dict(id=2, importance=2, novelty=2, utility=2, post_value=2),
    ]
    client = Responses(json.dumps({"rankings": ranks}))
    assert asyncio.run(ArticleAnalyzer(client).analyze(rows)) is rows[0]
    assert len(client.prompts) == 1
    assert "untrusted" in client.prompts[0]
    for key, value in (("id", 99), ("importance", True), ("novelty", 11), ("utility", "9")):
        bad = [dict(ranks[0], **{key: value}), ranks[1]]
        with pytest.raises(ServiceError, match="invalid_analysis"):
            asyncio.run(ArticleAnalyzer(Responses(json.dumps({"rankings": bad}))).analyze(rows))
    assert asyncio.run(ArticleAnalyzer(Responses(json.dumps({"rankings": [ranks[1]]}))).analyze([rows[1]])) is None
    assert asyncio.run(ArticleAnalyzer(client).analyze([])) is None


def test_search_fallback_is_explicit_optin_bounded_and_official_only(factory):
    with factory() as session:
        session.add(Source(name="OpenAI", url="https://openai.com/news/rss.xml", priority=50))
        session.commit()
    entry = {
        "url": "https://openai.com/index/new-model?utm_source=search",
        "title": "AI model improvements", "summary": "Useful SDK inference improvements",
        "source": "OpenAI", "published_at": (utcnow() - timedelta(minutes=1)).isoformat(),
    }
    client = SimpleNamespace(complete=AsyncMock(return_value=json.dumps({"articles": [entry]})))
    settings = SimpleNamespace(web_search_enabled=False, article_max_age_days=7)
    collector = ArticleCollector(settings, None)

    async def run():
        with factory() as session:
            assert await collector.search_fallback(session, client) == []
            client.complete.assert_not_awaited()
            settings.web_search_enabled = True
            rows = await collector.search_fallback(session, client)
            assert len(rows) == 1
            assert rows[0].canonical_url == "https://openai.com/index/new-model"
            assert client.complete.call_args.kwargs == {"web_search": True}
            assert await collector.search_fallback(session, client) == []
            for replacement in (
                {"url": "https://attacker.example/ai"},
                {"published_at": (utcnow() + timedelta(days=1)).isoformat()},
                {"source": "Unknown"}, {"title": "x" * 501},
            ):
                client.complete.return_value = json.dumps({"articles": [dict(entry, **replacement)]})
                assert await collector.search_fallback(session, client) == []
            client.complete.side_effect = ServiceError("responses_forbidden")
            assert await collector.search_fallback(session, client) == []
    asyncio.run(run())
    with factory() as session:
        assert len(session.scalars(select(Article)).all()) == 1
