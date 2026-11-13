# Changelog

## Unreleased

### Fixes
- **Licensed under the GNU GPL v3.0 or later.** cloudisle-dl had no licence before. A [Legal Notice](LEGAL.md) now
  says what the tool does and that data rights stay with the publisher.

## 0.1.1 - 2026-11-10

### Features
- **Public one-line installers.** Install or update without cloning, a GitHub account or a browser. They set up uv,
  Python and the dependencies, and leave running workers and downloaded data alone.

## 0.1.0 - 2026-11-10

First version.

### Features
- **Command line for Linux, macOS and Windows:** `init`, `start`, `status`, `logs`, `stop`, `resume`, `run` and
  `report`.
- **Background worker** that stops gracefully on request.
- **Resumable, validated downloads** with LAZ packs, state kept inside the destination, and conservative rate limits.
