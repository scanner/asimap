# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.1.32] - 2025-02-08

### Fixed

- Handle messages with badly encoded headers gracefully instead of crashing (GH-456): per-header error handling in email generator falls back to raw encoding when Python's fold_binary() fails

## [2.1.31] - 2025-02-07

### Fixed

- Fix iOS 18+ Mail client compatibility (GH-429): INTERNALDATE responses now use the correct RFC 3501 date-time format instead of RFC 2822 format
- Fix duplicate UID in FETCH responses when client uses `UID FETCH` with explicit UID in fetch attributes
- Fix LIST response returning the root "" folder as a selectable mailbox instead of filtering it out
- Fix stale `\HasChildren`/`\HasNoChildren` flags in LIST responses by verifying against actual folder hierarchy

### Added

- linting upraded to `ruff`
- updated how we build requirements, structure Makefiles
