# Changelog

All notable changes to Stele. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-10-31

First stable release: offline document-to-Markdown conversion on Apple Silicon.

### Added

- Conversion of PDFs (digital, scanned, mixed), Office files, HTML, CSV, TXT, Markdown and
  images into one Markdown document per file, with `assets/` and `metadata.json`.
- Per-page routing across Docling, Apple Vision, PaddleOCR-VL 1.6 and GLM-OCR (4-bit, MLX),
  driven by a quality check on the produced text instead of engine confidence.
- Three modes: Automatic, Fast and Advanced.
- Chart data tables, extracted only when every value is printed in the image.
- Memory budget on the kernel's phys_footprint with per-engine leases and idle model eviction.
- Per-page checkpoints: conversions resume after a crash or restart.
- Local web interface with drag and drop of files and folders, preview, per-file and
  per-job streaming ZIP, resource panel and history.
- `Stele.app` menu bar app (`make app`): starts the server, opens the browser, stops the
  server on quit.
- Test suite (208 tests, including kill -9 recovery and browser tests in Chrome and WebKit)
  and CI on GitHub Actions with Apple Silicon runners.
- English README (Italian version in `README.it.md`), demo GIF and technical decisions backed
  by `benchmarks/REPORT.md`.

### Fixed

- Markdown files keep their front matter, callouts and checkboxes: they are published as
  written instead of being re-parsed by Docling.
- Images linked by Markdown and HTML files (next to the file in the uploaded folder) are
  copied into `assets/` instead of being dropped; links that leave the upload folder are
  never followed.
- Spell-check ratio counts UTF-16 units, so text after emoji or math letters like 𝑥 is no
  longer skipped.
- A file uploaded after a large folder no longer waits for the whole folder: the queue is
  round-robin across uploads.
- Engines load their models one at a time: concurrent first loads could fail with
  "cannot import name 'AutoProcessor' from 'transformers'".
- An upload with every file rejected, or one that fails, no longer leaves an empty job stuck
  on "Caricamento in corso…".
- The preview shows an error message instead of the server's JSON error when a result is gone.
- The file selection is kept until the upload succeeds, so a failed upload can be retried.
- Vision helper output parsed line by line; resource manager deadlock when evicting idle
  models; local server hardening (allowed hosts) and conversion edge cases.

[1.0.0]: https://github.com/Sero3105/stele/releases/tag/v1.0.0
