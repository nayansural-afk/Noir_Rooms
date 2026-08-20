"""
NOIR ROOMS V2 — AI Cinema News & Telegram Content Manager

V1 base:
- GitHub Actions scheduled execution
- Groq OpenAI-compatible API
- Telegram Bot API publishing
- Persistent JSON history committed back to GitHub
- News collection, scoring, AI editorial, adaptive strategy

V2 additions:
- Story-level clustering & deduplication
- Multi-source verification / confidence boost
- Content Memory (entity / topic tracking)
- AI Fact Checker (claims must be grounded in supplied text)
- Hook Intelligence (multiple hooks → select best)
- Breaking-news priority signal
- Stronger relevance gate

Required secrets:
  TELEGRAM_TOKEN
  GROQ_API_KEY

Optional environment variables:
  CHANNEL_ID=@noir_rooms
  POSTS_PER_RUN=1
  CANDIDATE_LIMIT=50
  PUBLISH_LIMIT=1
  MAX_AI_ATTEMPTS=5

Never fabricates analytics. Bot API does not provide full channel stats.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from telegram import Bot

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]

CHANNEL_ID = os.environ.get("CHANNEL_ID", "@noir_rooms")
POSTS_PER_RUN = max(1, int(os.environ.get("POSTS_PER_RUN", "1")))
CANDIDATE_LIMIT = max(10, int(os.environ.get("CANDIDATE_LIMIT", "50")))
PUBLISH_LIMIT = max(1, int(os.environ.get("PUBLISH_LIMIT", str(POSTS_PER_RUN))))
MAX_AI_ATTEMPTS = max(1, int(os.environ.get("MAX_AI_ATTEMPTS", "5")))

HISTORY_FILE = Path("news_history.json")
MODEL = "openai/gpt-oss-20b"
HISTORY_VERSION = 2

SOURCES = [
    {"name": "BBC", "url": "https://www.bbc.com/news/entertainment_and_arts", "weight": 0.98,
     "rss": "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml"},
    {"name": "The Guardian", "url": "https://www.theguardian.com/film", "weight": 0.94,
     "rss": "https://www.theguardian.com/film/rss"},
    {"name": "Variety", "url": "https://variety.com/", "weight": 0.94,
     "rss": "https://variety.com/feed/"},
    {"name": "Variety Film", "url": "https://variety.com/v/film/", "weight": 0.95,
     "rss": "https://variety.com/v/film/feed/"},
    {"name": "Deadline", "url": "https://deadline.com/", "weight": 0.94,
     "rss": "https://deadline.com/feed/"},
    {"name": "Deadline Film", "url": "https://deadline.com/v/film/", "weight": 0.95,
     "rss": "https://deadline.com/v/film/feed/"},
    {"name": "The Hollywood Reporter", "url": "https://www.hollywoodreporter.com/", "weight": 0.94,
     "rss": "https://www.hollywoodreporter.com/feed/"},
    {"name": "IndieWire", "url": "https://www.indiewire.com/", "weight": 0.88,
     "rss": "https://www.indiewire.com/feed/"},
    {"name": "TheWrap", "url": "https://www.thewrap.com/", "weight": 0.84,
     "rss": "https://www.thewrap.com/feed/"},
]

CATEGORY_KEYWORDS = {
    "breaking_news": ["breaking", "announces", "announcement", "confirmed", "official", "dies", "death", "retires"],
    "movie_news": ["movie", "film", "sequel", "remake", "production", "casting", "release"],
    "actors_directors": ["actor", "actress", "director", "star", "cast", "starring", "joins"],
    "upcoming_movies": ["upcoming", "release date", "in talks", "development", "greenlight"],
    "trailers_first_look": ["trailer", "teaser", "first look", "poster", "footage", "clip"],
    "box_office": ["box office", "gross", "opening", "million", "billion", "earnings"],
    "awards_festivals": ["oscar", "academy awards", "golden globe", "bafta", "cannes", "venice", "sundance", "festival"],
    "streaming": ["netflix", "hbo", "hbo max", "disney+", "prime video", "apple tv", "streaming", "paramount+"],
    "controversy": ["controversy", "controversial", "backlash", "dispute", "lawsuit", "feud", "scandal"],
}

CINEMA_POSITIVE = [
    "movie", "film", "cinema", "director", "actor", "actress", "cast", "casting",
    "trailer", "teaser", "box office", "oscar", "festival", "premiere", "sequel",
    "remake", "screenplay", "hollywood", "studio", "netflix", "disney", "marvel",
    "warner", "paramount", "universal", "sony pictures", "a24", "feature",
    "documentary", "animation", "series", "showrunner", "screenwriter",
]

NON_CINEMA_NEGATIVE = [
    "little league", "soccer", "football match", "nba", "nfl", "mlb", "tennis",
    "recipe", "cooking", "masterchef", "weather", "election", "congress",
    "stock market", "crypto", "bitcoin", "iphone review",
]

BREAKING_SIGNALS = [
    "dies", "dead", "passes away", "retired", "retires", "breaking",
    "exclusive", "officially announces", "confirms", "cast as",
    "box office record", "opens to", "first look", "trailer drops",
    "wins oscar", "wins academy",
]

KNOWN_ENTITIES = [
    "christopher nolan", "nolan", "tom cruise", "marvel", "dc", "batman",
    "superman", "spider-man", "star wars", "harry potter", "james bond",
    "netflix", "disney", "hbo", "warner bros", "paramount", "universal",
    "a24", "scorsese", "spielberg", "tarantino", "denis villeneuve",
    "greta gerwig", "oppenheimer", "dune", "avatar", "mission impossible",
    "oscar", "cannes", "venice", "sundance", "bafta",
]

DEFAULT_CATEGORY_WEIGHTS = {
    "movie_news": 30,
    "breaking_news": 0,
    "actors_directors": 20,
    "upcoming_movies": 15,
    "trailers_first_look": 10,
    "box_office": 10,
    "awards_festivals": 5,
    "streaming": 5,
    "controversy": 0,
    "short_analysis": 5,
}

HEADLINE_STOPWORDS = {
    "the", "a", "an", "of", "to", "and", "for", "in", "on", "with", "from",
    "new", "news", "first", "look", "film", "movie", "report", "says", "says",
    "exclusive", "official", "announces",
}


@dataclass
class NewsItem:
    id: str
    source: str
    source_weight: float
    url: str
    title: str
    published_at: str
    discovered_at: str
    category: str
    description: str = ""
    score: float = 0.0
    freshness: float = 0.0
    credibility: float = 0.0
    audience_interest: float = 0.0
    potential_engagement: float = 0.0
    status: str = "candidate"
    story_id: str = ""
    multi_source_count: int = 1
    multi_source_names: list[str] = field(default_factory=list)
    is_breaking: bool = False
    entities: list[str] = field(default_factory=list)
    verification_confidence: float = 0.5


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = parsedate_to_datetime(value)
        return dt.astimezone(timezone.utc)
    except Exception:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    clean = parsed._replace(fragment="", query="").geturl()
    return clean.rstrip("/")


def strip_html(value: str) -> str:
    value = html.unescape(value or "")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def stable_id(source: str, url: str, title: str) -> str:
    raw = f"{source}|{normalize_url(url)}|{title.lower().strip()}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def story_fingerprint(title: str) -> str:
    """Lightweight story key for clustering similar headlines."""
    tokens = sorted(token_set(title))
    # Keep strongest content tokens
    key = " ".join(tokens[:12])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def extract_entities(text: str) -> list[str]:
    lower = text.lower()
    found = [e for e in KNOWN_ENTITIES if e in lower]
    # Also capture simple Proper Name patterns (2+ capitalized words)
    for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b", text):
        name = m.group(1).strip().lower()
        if len(name) > 3 and name not in found and name not in HEADLINE_STOPWORDS:
            found.append(name)
    return found[:12]


def load_history() -> dict[str, Any]:
    default = {
        "version": HISTORY_VERSION,
        "posts": [],
        "seen_news_ids": [],
        "seen_urls": [],
        "seen_story_ids": [],
        "entity_memory": {},
        "category_stats": {},
        "time_stats": {},
        "strategy": {
            "exploration_rate": 0.30,
            "category_weights": DEFAULT_CATEGORY_WEIGHTS.copy(),
        },
        "weekly_reports": [],
        "analytics_snapshots": [],
    }
    if not HISTORY_FILE.exists():
        return default
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return default

    data.setdefault("version", HISTORY_VERSION)
    data["version"] = HISTORY_VERSION
    for k, v in default.items():
        data.setdefault(k, v if not isinstance(v, dict) else v.copy())
    data["strategy"].setdefault("exploration_rate", 0.30)
    data["strategy"].setdefault("category_weights", DEFAULT_CATEGORY_WEIGHTS.copy())
    data.setdefault("entity_memory", {})
    data.setdefault("seen_story_ids", [])
    return data


def save_history(history: dict[str, Any]) -> None:
    history["version"] = HISTORY_VERSION
    history["posts"] = history.get("posts", [])[-500:]
    history["seen_news_ids"] = history.get("seen_news_ids", [])[-2000:]
    history["seen_urls"] = history.get("seen_urls", [])[-2000:]
    history["seen_story_ids"] = history.get("seen_story_ids", [])[-2000:]
    history["analytics_snapshots"] = history.get("analytics_snapshots", [])[-180:]
    history["weekly_reports"] = history.get("weekly_reports", [])[-52:]
    # Cap entity memory
    em = history.get("entity_memory", {})
    if len(em) > 400:
        # keep most recently seen
        items = sorted(em.items(), key=lambda x: x[1].get("last_seen", ""), reverse=True)[:400]
        history["entity_memory"] = dict(items)
    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def rss_entries(xml_text: str) -> list[dict[str, str]]:
    root = ET.fromstring(xml_text)
    items = []
    for node in root.iter():
        if node.tag.split("}")[-1].lower() in {"item", "entry"}:
            fields: dict[str, str] = {}
            for child in node:
                tag = child.tag.split("}")[-1].lower()
                if tag == "link":
                    href = child.attrib.get("href")
                    fields["link"] = href or (child.text or "")
                else:
                    fields[tag] = child.text or child.attrib.get("href", "")
            if fields.get("title") and fields.get("link"):
                items.append(fields)
    return items


def is_cinema_relevant(title: str, description: str) -> bool:
    text = f"{title} {description}".lower()
    if any(neg in text for neg in NON_CINEMA_NEGATIVE):
        return False
    return any(pos in text for pos in CINEMA_POSITIVE)


def detect_breaking(title: str, description: str) -> bool:
    text = f"{title} {description}".lower()
    return any(sig in text for sig in BREAKING_SIGNALS)


def fetch_feed(source: dict[str, Any]) -> list[NewsItem]:
    try:
        response = requests.get(
            source["rss"],
            timeout=20,
            headers={"User-Agent": "NoirRoomsBot/2.0 (+Telegram cinema news bot)"},
        )
        response.raise_for_status()
        raw_items = rss_entries(response.text)
    except Exception as exc:
        print(f"[WARN] feed failed: {source['name']} | {exc}")
        return []

    result = []
    for item in raw_items[:25]:
        title = strip_html(item.get("title", ""))
        url = normalize_url(item.get("link", ""))
        description = strip_html(
            item.get("description")
            or item.get("summary")
            or item.get("content")
            or ""
        )
        if not title or not url:
            continue
        if not is_cinema_relevant(title, description):
            continue

        published = item.get("pubdate") or item.get("published") or item.get("updated")
        published_dt = parse_date(published)
        news_id = stable_id(source["name"], url, title)
        entities = extract_entities(title + " " + description)
        result.append(
            NewsItem(
                id=news_id,
                source=source["name"],
                source_weight=float(source["weight"]),
                url=url,
                title=title,
                published_at=published_dt.isoformat(),
                discovered_at=now_iso(),
                category=classify_category(title + " " + description),
                description=description[:1000],
                story_id=story_fingerprint(title),
                is_breaking=detect_breaking(title, description),
                entities=entities,
                multi_source_names=[source["name"]],
            )
        )
    return result


def classify_category(text: str) -> str:
    lower = text.lower()
    scores = {
        category: sum(lower.count(k) for k in keywords)
        for category, keywords in CATEGORY_KEYWORDS.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "movie_news"


def hours_old(item: NewsItem) -> float:
    try:
        dt = datetime.fromisoformat(item.published_at)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600)
    except Exception:
        return 72.0


def freshness_score(item: NewsItem) -> float:
    age = hours_old(item)
    if age <= 2:
        return 100.0
    if age <= 8:
        return 92.0
    if age <= 24:
        return 82.0
    if age <= 48:
        return 65.0
    if age <= 96:
        return 40.0
    return 15.0


def interest_score(item: NewsItem) -> float:
    text = f"{item.title} {item.description}".lower()
    score = 45.0
    high_interest = [
        "marvel", "dc", "batman", "superman", "star wars", "harry potter",
        "nolan", "spielberg", "tarantino", "scorsese", "netflix", "disney",
        "hbo", "oppenheimer", "avengers", "spider-man", "james bond",
        "oscar", "trailer", "box office", "christopher nolan", "tom cruise",
        "villeneuve", "dune",
    ]
    score += min(30, sum(5 for k in high_interest if k in text))
    if item.category in {"actors_directors", "trailers_first_look", "breaking_news"}:
        score += 12
    if item.is_breaking:
        score += 10
    if "exclusive" in text:
        score += 8
    return min(score, 100.0)


def engagement_score(item: NewsItem, history: dict[str, Any]) -> float:
    stats = history.get("category_stats", {}).get(item.category, {})
    published = stats.get("published", 0)
    avg_views = stats.get("avg_views")
    avg_reactions = stats.get("avg_reactions")
    base = 45.0
    if published >= 3 and isinstance(avg_views, (int, float)):
        base += min(35.0, avg_views / max(1, history.get("benchmark_views", 100)) * 20)
    if published >= 3 and isinstance(avg_reactions, (int, float)):
        base += min(20.0, avg_reactions)
    return min(base, 100.0)


def entity_penalty(item: NewsItem, history: dict[str, Any]) -> float:
    """Downrank entities covered very recently (Content Memory)."""
    em = history.get("entity_memory", {})
    if not item.entities:
        return 1.0
    penalty = 1.0
    now = datetime.now(timezone.utc)
    for ent in item.entities:
        rec = em.get(ent)
        if not rec:
            continue
        try:
            last = datetime.fromisoformat(rec.get("last_seen", ""))
            hours = (now - last).total_seconds() / 3600
            count = int(rec.get("count", 0))
            if hours < 24 and count >= 1:
                penalty *= 0.72
            elif hours < 72 and count >= 2:
                penalty *= 0.85
        except Exception:
            continue
    return max(0.45, penalty)


def score_news(item: NewsItem, history: dict[str, Any]) -> NewsItem:
    item.freshness = freshness_score(item)
    item.credibility = item.source_weight * 100
    item.audience_interest = interest_score(item)
    item.potential_engagement = engagement_score(item, history)

    # Multi-source verification boost
    ms_boost = 0.0
    if item.multi_source_count >= 3:
        ms_boost = 12.0
        item.verification_confidence = min(0.95, 0.55 + 0.15 * item.multi_source_count)
    elif item.multi_source_count == 2:
        ms_boost = 7.0
        item.verification_confidence = 0.72
    else:
        item.verification_confidence = 0.50 + (item.source_weight - 0.8) * 0.5

    base = (
        0.22 * item.freshness
        + 0.22 * item.credibility
        + 0.18 * item.audience_interest
        + 0.25 * item.potential_engagement
        + 0.13 * (item.verification_confidence * 100)
    )
    base += ms_boost
    if item.is_breaking:
        base += 8.0

    base *= entity_penalty(item, history)
    item.score = round(min(base, 100.0), 2)
    return item


def token_set(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9'-]{2,}", text.lower())
    return {w for w in words if w not in HEADLINE_STOPWORDS}


def similarity(a: str, b: str) -> float:
    x, y = token_set(a), token_set(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def cluster_stories(items: list[NewsItem]) -> list[NewsItem]:
    """
    Story-level clustering:
    Group near-duplicate headlines into one story.
    Keep the strongest item, attach multi-source metadata.
    """
    # Sort by source weight + freshness preference later; first pass group by story_id / similarity
    clusters: dict[str, list[NewsItem]] = {}
    ordered: list[NewsItem] = []

    for item in sorted(items, key=lambda x: (x.source_weight, x.freshness), reverse=True):
        matched_key = None
        for key, group in clusters.items():
            rep = group[0]
            if item.story_id == rep.story_id or similarity(item.title, rep.title) >= 0.62:
                matched_key = key
                break
        if matched_key is None:
            key = item.story_id or item.id
            clusters[key] = [item]
            ordered.append(item)
        else:
            clusters[matched_key].append(item)

    result: list[NewsItem] = []
    for key, group in clusters.items():
        # Representative = highest source weight, then freshest
        group_sorted = sorted(group, key=lambda x: (x.source_weight, -hours_old(x)), reverse=True)
        rep = group_sorted[0]
        sources = []
        for g in group_sorted:
            if g.source not in sources:
                sources.append(g.source)
        rep.multi_source_count = len(sources)
        rep.multi_source_names = sources
        # Merge entities
        ent = []
        for g in group:
            for e in g.entities:
                if e not in ent:
                    ent.append(e)
        rep.entities = ent[:15]
        # Prefer breaking if any member is breaking
        rep.is_breaking = any(g.is_breaking for g in group)
        result.append(rep)

    return result


def deduplicate(items: list[NewsItem], history: dict[str, Any]) -> list[NewsItem]:
    seen_ids = set(history.get("seen_news_ids", []))
    seen_urls = {normalize_url(x) for x in history.get("seen_urls", [])}
    seen_stories = set(history.get("seen_story_ids", []))
    output: list[NewsItem] = []

    for item in sorted(items, key=lambda x: x.score, reverse=True):
        if item.id in seen_ids or normalize_url(item.url) in seen_urls:
            continue
        if item.story_id and item.story_id in seen_stories:
            continue

        duplicate = False
        for kept in output:
            if normalize_url(item.url) == normalize_url(kept.url):
                duplicate = True
                break
            if item.story_id and item.story_id == kept.story_id:
                duplicate = True
                break
            if similarity(item.title, kept.title) >= 0.68:
                duplicate = True
                break

        if not duplicate:
            output.append(item)

    return output


def select_candidates(items: list[NewsItem], history: dict[str, Any]) -> list[NewsItem]:
    strategy = history["strategy"]
    exploration_rate = float(strategy.get("exploration_rate", 0.30))
    weights = strategy.get("category_weights", DEFAULT_CATEGORY_WEIGHTS)

    # Breaking first
    breaking = [x for x in items if x.is_breaking]
    normal = [x for x in items if not x.is_breaking]
    ranked = sorted(breaking, key=lambda x: x.score, reverse=True) + sorted(
        normal, key=lambda x: x.score, reverse=True
    )

    selected: list[NewsItem] = []
    exploitation_n = max(1, round(CANDIDATE_LIMIT * (1 - exploration_rate)))
    selected.extend(ranked[:exploitation_n])

    remaining = [x for x in ranked[exploitation_n:] if x not in selected]
    random.shuffle(remaining)
    while remaining and len(selected) < CANDIDATE_LIMIT:
        selected.append(remaining.pop())

    for item in selected:
        item.score = round(
            item.score * (0.70 + 0.30 * (weights.get(item.category, 5) / 30)),
            2,
        )

    return sorted(selected, key=lambda x: (x.is_breaking, x.score), reverse=True)[:CANDIDATE_LIMIT]


def groq_generate(prompt: str, max_tokens: int = 1100, temperature: float = 0.4, retries: int = 4) -> str:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                },
                timeout=90,
            )
            if response.status_code == 429:
                wait = min(60, (2 ** attempt) + random.uniform(0.5, 2.0))
                print(f"[RATE] Groq 429 — sleeping {wait:.1f}s (attempt {attempt + 1}/{retries})")
                time.sleep(wait)
                continue
            if response.status_code >= 400:
                body_preview = response.text[:350].replace("\n", " ")
                print(f"[WARN] Groq HTTP {response.status_code}: {body_preview}")
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            wait = min(30, (2 ** attempt) + random.uniform(0.3, 1.5))
            print(f"[WARN] Groq request error: {exc} — retry in {wait:.1f}s")
            time.sleep(wait)
    raise last_exc or RuntimeError("Groq generate failed after retries")


def ai_editorial(item: NewsItem) -> dict[str, Any]:
    sources_line = ", ".join(item.multi_source_names) if item.multi_source_names else item.source
    multi_note = (
        f"This story appears in {item.multi_source_count} source(s): {sources_line}. "
        if item.multi_source_count > 1
        else f"Single source: {item.source}. Treat unconfirmed details carefully. "
    )

    prompt = f"""
You are the Persian-language editor + fact-checker of NOIR ROOMS (cinema Telegram channel).

HARD RULES:
1. Use ONLY facts present in the supplied TITLE + DESCRIPTION.
2. Do NOT invent names, dates, numbers, quotes, cast, directors, box-office figures, or plot points.
3. If a detail is not explicitly supported by the source text → OMIT it.
4. Do not use your world knowledge to fill gaps.
5. Never present a rumor as confirmed.
6. Write original natural Persian (not machine-translation style).
7. No misleading clickbait.

{multi_note}
Verification confidence prior: {item.verification_confidence:.2f}
Breaking signal: {item.is_breaking}

SOURCE: {item.source}
OTHER SOURCES SEEN: {sources_line}
URL: {item.url}
CATEGORY: {item.category}
TITLE: {item.title}
PUBLISHED: {item.published_at}
DESCRIPTION/EXCERPT:
{item.description}

TASK:
A) Extract only claims that are explicitly supported by the text above.
B) Generate 3 different Persian hooks (punchy, max 16 words each). Pick the best as selected_hook.
C) Write the final Persian post fields.

Return JSON exactly:
{{
  "supported_claims": ["claim1", "claim2"],
  "hooks": ["hook A", "hook B", "hook C"],
  "selected_hook": "best hook",
  "hook_reason": "why this hook",
  "headline": "clear Persian headline",
  "summary": "2-4 short Persian paragraphs using only supported claims",
  "key_facts": ["fact1", "fact2", "fact3"],
  "cta": "short CTA or empty string",
  "hashtags": ["#سینما", "#فیلم"],
  "confidence": "high|medium|low",
  "publish": true,
  "reason": "editorial decision",
  "fact_check_notes": "any uncertainty"
}}

If the story is not clearly cinema/TV production related, or claims are too weak, set publish=false.
If only one weak source and story is extraordinary, prefer confidence=medium or low.
"""
    raw = groq_generate(prompt)
    data = json.loads(raw)

    # Normalize fields
    data.setdefault("hooks", [])
    data.setdefault("selected_hook", data.get("hook", ""))
    data.setdefault("hook", data.get("selected_hook", ""))
    data.setdefault("headline", "")
    data.setdefault("summary", "")
    data.setdefault("key_facts", [])
    data.setdefault("cta", "")
    data.setdefault("hashtags", [])
    data.setdefault("confidence", "medium")
    data.setdefault("publish", False)
    data.setdefault("reason", "")
    data.setdefault("supported_claims", [])
    data.setdefault("fact_check_notes", "")

    # Prefer selected_hook
    if data.get("selected_hook"):
        data["hook"] = data["selected_hook"]

    if data.get("confidence") == "low":
        data["publish"] = False

    # Extra safety: require at least some substance
    if not data.get("summary") or not data.get("hook"):
        data["publish"] = False
        data["reason"] = data.get("reason") or "missing core fields"

    return data


def format_post(item: NewsItem, edited: dict[str, Any]) -> str:
    lines = [
        edited["hook"].strip(),
        "",
        f"🎬 {edited['headline'].strip()}",
        "",
        edited["summary"].strip(),
    ]

    facts = [str(x).strip() for x in edited.get("key_facts", []) if str(x).strip()]
    if facts:
        lines += ["", "📌 نکات مهم:"]
        lines += [f"• {x}" for x in facts[:4]]

    if item.multi_source_count >= 2:
        lines += ["", f"✅ تأیید چندمنبعی: {', '.join(item.multi_source_names[:4])}"]

    cta = edited.get("cta", "").strip()
    if cta:
        lines += ["", cta]

    hashtags = [str(x).strip() for x in edited.get("hashtags", []) if str(x).strip()]
    if hashtags:
        lines += ["", " ".join(hashtags[:6])]

    lines += ["", f"🔗 منبع: {item.source}", item.url]
    return "\n".join(lines)


async def publish(bot: Bot, item: NewsItem, edited: dict[str, Any]) -> dict[str, Any]:
    text = format_post(item, edited)
    message = await bot.send_message(
        chat_id=CHANNEL_ID,
        text=text,
        disable_web_page_preview=False,
    )
    return {
        "telegram_message_id": message.message_id,
        "published_at": now_iso(),
        "text": text,
        "analytics": {
            "views": None,
            "forwards": None,
            "reactions": None,
            "source": "unavailable_via_bot_api",
        },
    }


def update_entity_memory(history: dict[str, Any], item: NewsItem) -> None:
    em = history.setdefault("entity_memory", {})
    ts = now_iso()
    for ent in item.entities:
        rec = em.setdefault(ent, {"count": 0, "last_seen": ts, "categories": []})
        rec["count"] = int(rec.get("count", 0)) + 1
        rec["last_seen"] = ts
        cats = rec.setdefault("categories", [])
        if item.category not in cats:
            cats.append(item.category)


def record_published(history: dict[str, Any], item: NewsItem, edited: dict[str, Any], result: dict[str, Any]) -> None:
    hour = datetime.now(timezone.utc).hour
    history["posts"].append({
        "news_id": item.id,
        "story_id": item.story_id,
        "source": item.source,
        "sources": item.multi_source_names,
        "multi_source_count": item.multi_source_count,
        "url": item.url,
        "title": item.title,
        "category": item.category,
        "score": item.score,
        "freshness": item.freshness,
        "credibility": item.credibility,
        "audience_interest": item.audience_interest,
        "potential_engagement": item.potential_engagement,
        "verification_confidence": item.verification_confidence,
        "is_breaking": item.is_breaking,
        "entities": item.entities,
        "hook": edited.get("hook"),
        "hooks": edited.get("hooks", []),
        "confidence": edited.get("confidence"),
        "supported_claims": edited.get("supported_claims", []),
        "fact_check_notes": edited.get("fact_check_notes", ""),
        "status": "published",
        **result,
    })
    history["seen_news_ids"].append(item.id)
    history["seen_urls"].append(item.url)
    if item.story_id:
        history.setdefault("seen_story_ids", []).append(item.story_id)

    stats = history["category_stats"].setdefault(item.category, {
        "published": 0,
        "avg_views": None,
        "avg_reactions": None,
        "samples_with_metrics": 0,
    })
    stats["published"] += 1
    history["time_stats"].setdefault(str(hour), {"published": 0})
    history["time_stats"][str(hour)]["published"] += 1
    update_entity_memory(history, item)


def update_strategy(history: dict[str, Any]) -> None:
    category_stats = history.get("category_stats", {})
    observed = []
    for category, stats in category_stats.items():
        samples = stats.get("samples_with_metrics", 0)
        avg_views = stats.get("avg_views")
        if samples and isinstance(avg_views, (int, float)):
            observed.append((category, float(avg_views)))

    if len(observed) < 2:
        history["strategy"]["exploration_rate"] = 0.30
        return

    observed.sort(key=lambda x: x[1], reverse=True)
    total = sum(max(v, 1.0) for _, v in observed)
    learned = {}
    for category, value in observed:
        learned[category] = min(45.0, max(5.0, value / total * 100.0))

    total_learned = sum(learned.values())
    if total_learned:
        for category in learned:
            learned[category] = round(learned[category] / total_learned * 70.0, 2)

    base = DEFAULT_CATEGORY_WEIGHTS.copy()
    for category, value in learned.items():
        base[category] = value

    history["strategy"]["category_weights"] = base
    history["strategy"]["exploration_rate"] = 0.30


def build_weekly_report(history: dict[str, Any]) -> dict[str, Any]:
    posts = history.get("posts", [])[-100:]
    category_counts: dict[str, int] = {}
    for post in posts:
        category = post.get("category", "unknown")
        category_counts[category] = category_counts.get(category, 0) + 1

    best_category = max(category_counts, key=category_counts.get) if category_counts else None
    best_hook = None
    metric_posts = [p for p in posts if p.get("analytics", {}).get("views") is not None]
    if metric_posts:
        best = max(metric_posts, key=lambda p: p["analytics"]["views"])
        best_hook = best.get("hook")

    report = {
        "generated_at": now_iso(),
        "version": HISTORY_VERSION,
        "posts": len(posts),
        "average_views": (
            sum(p["analytics"]["views"] for p in metric_posts) / len(metric_posts)
            if metric_posts else None
        ),
        "best_category": best_category,
        "worst_category": min(category_counts, key=category_counts.get) if category_counts else None,
        "best_hook": best_hook,
        "top_news": [
            {
                "title": p.get("title"),
                "category": p.get("category"),
                "score": p.get("score"),
                "multi_source_count": p.get("multi_source_count", 1),
            }
            for p in sorted(posts, key=lambda x: x.get("score", 0), reverse=True)[:5]
        ],
        "entity_memory_size": len(history.get("entity_memory", {})),
        "growth": None,
        "recommended_strategy": history.get("strategy", {}),
    }
    history["weekly_reports"].append(report)
    return report


def print_report(report: dict[str, Any]) -> None:
    print("\n=== NOIR ROOMS V2 REPORT ===")
    for key in ["posts", "average_views", "best_category", "worst_category", "best_hook", "growth", "entity_memory_size"]:
        print(f"{key}: {report.get(key)}")
    print("recommended_strategy:", json.dumps(report.get("recommended_strategy"), ensure_ascii=False))


async def main() -> None:
    history = load_history()

    print("NOIR ROOMS V2: collecting cinema news...")
    all_items: list[NewsItem] = []
    for source in SOURCES:
        all_items.extend(fetch_feed(source))

    print(f"Raw collected={len(all_items)}")

    # Story-level clustering (multi-source)
    clustered = cluster_stories(all_items)
    print(f"After story clustering={len(clustered)}")

    scored = [score_news(x, history) for x in clustered]
    unique = deduplicate(scored, history)
    candidates = select_candidates(unique, history)

    print(
        f"unique={len(unique)} candidates={len(candidates)} "
        f"breaking={sum(1 for c in candidates if c.is_breaking)}"
    )

    if not candidates:
        print("No eligible news items.")
        save_history(history)
        return

    bot = Bot(token=TELEGRAM_TOKEN)
    published = 0
    ai_attempts = 0

    for item in candidates:
        if published >= PUBLISH_LIMIT:
            break
        if ai_attempts >= MAX_AI_ATTEMPTS:
            print(f"Reached MAX_AI_ATTEMPTS={MAX_AI_ATTEMPTS}, stopping AI calls.")
            break

        ai_attempts += 1
        try:
            if ai_attempts > 1:
                time.sleep(1.5 + random.uniform(0.2, 1.0))
            edited = ai_editorial(item)
        except Exception as exc:
            print(f"[WARN] AI editor failed for {item.title[:80]}: {exc}")
            continue

        if not edited.get("publish", False):
            item.status = "rejected_by_editor"
            print(
                f"[SKIP] editor rejected: {item.title[:80]} | "
                f"conf={edited.get('confidence')} | reason={edited.get('reason', '')}"
            )
            continue

        try:
            result = await publish(bot, item, edited)
            record_published(history, item, edited, result)
            published += 1
            print(
                f"Published #{published}: {item.category} | "
                f"ms={item.multi_source_count} | breaking={item.is_breaking} | {item.title[:70]}"
            )
        except Exception as exc:
            print(f"[ERROR] Telegram publish failed: {exc}")
            break

    update_strategy(history)
    report = build_weekly_report(history)
    print_report(report)
    save_history(history)
    print(f"Done V2. published={published} ai_attempts={ai_attempts}")


if __name__ == "__main__":
    asyncio.run(main())
