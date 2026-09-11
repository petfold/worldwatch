# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

Started 2026-09-11, so 0.1.0's entry is written from the git history rather
than recorded at the time. Note that 0.1.0 was uploaded to PyPI directly rather
than by a `v*` tag push, so this repo has no release tags yet — unlike the
sibling projects, where the tag is what publishes.

## [0.1.0] — 2026-09-10

First PyPI release: calibrated anomaly detection over heterogeneous world data
streams, aiming for lead time over the news.

### Added

- **P0 pollers — all eight Tier-1 feeds live**, each modelled on its own terms:
  GDELT news events from the raw 15-minute export files, Wikipedia pageviews as
  a count channel informing the model, BTC/ETH spot prices through `log1p` and
  a scale-sane model config, alongside the remaining Tier-1 sources.
- Test CI and the stack-wide `[test]` extra; BSD-3-Clause license declared in
  `pyproject`.
- PyPI packaging metadata: description, readme, project URLs and keywords.

### Changed

- **Renamed from `worldmonitor` to `worldwatch`** throughout — the repo URL,
  the deploy path, the HTTP User-Agent and the README title.
- Documentation: expanded README; the push channel decided as self-hosted ntfy
  on the VPS; a setup guide for Ubuntu 24.04 given the 3.12 requirement.

### Fixed

- `docs/` path references corrected to `doc/`, the directory that actually
  exists — the design documents had been linked from a path that did not.
- `.gitignore` covers `.env` (operator credentials) and restores rules that had
  been dropped.
