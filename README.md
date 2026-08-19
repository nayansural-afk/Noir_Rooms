# Noir Rooms

AI-powered Persian cinema news manager for Telegram channel `@noir_rooms`.

## Architecture

This project intentionally follows the useful parts of the existing Light Room Bot:

- scheduled GitHub Actions
- Python entrypoint
- `TELEGRAM_TOKEN` and `GROQ_API_KEY` in GitHub Secrets
- Groq OpenAI-compatible chat completion
- Telegram publishing
- JSON history committed after every successful run

The Lightroom/Pillow/XMP pipeline is removed.

## Pipeline

RSS sources
→ normalize
→ source credibility
→ freshness
→ audience-interest scoring
→ engagement prior
→ URL/title deduplication
→ candidate ranking
→ Groq editorial verification
→ Telegram publish
→ history
→ strategy update

## Secrets

Required:

- `TELEGRAM_TOKEN`
- `GROQ_API_KEY`

Do not put either value in source code.

## Telegram permissions

The bot must be an administrator of `@noir_rooms` with permission to post messages.

## Analytics limitation

The Bot API is used for publishing, but it should NOT be treated as a complete historical
channel analytics API. The code therefore stores `views`, `forwards`, and `reactions` as
`null` unless a future real analytics adapter supplies them.

Telegram's MTProto API exposes channel statistics through `stats.getBroadcastStats` and
message statistics through `stats.getMessageStats`, subject to Telegram's administrator
and channel-size eligibility rules. Those methods are user-only, so a future analytics
adapter should use a properly authorized Telegram client session rather than pretending
the Bot API provides the data.

## Editorial / copyright policy

The bot summarizes source material in original Persian wording. It does not copy source
articles. Every published post keeps the direct source URL.

Source feeds can change or disappear. Failed feeds are skipped rather than replaced with
invented data.

Images are intentionally not part of v1. An image pipeline can be added later with an
explicit rights/licensing decision.

## Initial strategy

The strategy starts with the requested approximate distribution:

- Movie / Breaking: 30%
- Actors & Directors: 20%
- Upcoming Movies: 15%
- Trailer / First Look: 10%
- Box Office: 10%
- Awards / Festivals: 5%
- Streaming: 5%
- Experimental / Analysis: 5%

The implementation keeps a 30% exploration floor. Once real metrics are available,
learned weights can influence the remaining 70%.

## GitHub Actions

The workflow is intentionally simple and iPad-friendly. It runs four times per day,
commits `news_history.json`, and requires no local computer.
