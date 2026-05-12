# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Feishu (Lark) "link secretary" that monitors the user's own outgoing messages via WebSocket, extracts URLs, fetches metadata, and replies with a structured interactive card. See `DESIGN.md` for the authoritative specification.

## Development Rules (from AGENTS.md)

- **Document-first**: modify `DESIGN.md` before touching code. It is the single source of truth.
- **No emojis**: absolutely none in code, comments, docs, or commit messages.
- **Explicit failures**: external data errors must surface visibly; silent skips are forbidden.
- **Single entry point**: all execution starts from `main.py` only.
- **Logging**: use `logging`, never `print`.
- **Type hints**: all Python code requires complete type annotations.
- **Time**: store/compute in UTC; convert to Beijing time (CST) or US Eastern only for display.

## Environment

- Python dependency/env management: `uv` exclusively.
- Production and scheduled runs: Docker container.
- `uv` path: standard; Docker at `/usr/local/bin/docker`.

## Architecture

@DESIGN.md

## Key Constraints

- lark-cli can only listen to conversations where the bot or user account is present.
- YouTube Data API requires a proxy or overseas server in mainland China; fallback is OG tag scraping (no API key, no duration).
- `interactive` card *receiving* is not fully supported by lark-cli, but *sending* works normally.
