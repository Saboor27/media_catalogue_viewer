# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/).

## [0.1.0] - 2026-11-27

First public version.

### Added
- `generic_scraper.py`: config-driven scraper for listing → detail sites (URL-pattern and next-link pagination,
  parallel HTTP fetching with retry/back-off, robots.txt support, optional Selenium browser step, resumable
  SQLite storage, `--test` dry run, ignore rules, optional downloads, JSON/CSV export).
- `generic_viewer.py`: local web viewer with automatic field-type detection, search, filters, sorting, grouping,
  "browse data" lists, saved views, optional password and network sharing.
- Sample rules for books.toscrape.com and an annotated template (`sites/`).
- Rules-file reference (`docs/CONFIGURATION.md`).
- Offline pytest suite with a local mock shop, and a GitHub Actions workflow.
