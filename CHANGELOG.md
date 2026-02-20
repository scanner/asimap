# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.3.0] - 2026-02-19

### Added

- Support for IMAP MOVE extension (RFC 6851)

## [2.2.5] - 2026-02-11

### Changed

- Replace O(n) list lookups for UID and message key indexing with O(1) dict lookups, reducing CPU usage during FETCH, SEARCH, COPY, and EXPUNGE commands

## [2.2.4] - 2026-02-10

### Changed

- Reduce CPU usage in idle mailbox polling by skipping expensive `compact_sequence` serialization and full database commits when no messages or flags have changed
- Add py-spy sampling profiler to Docker images for CPU profiling

### Fixed

- Fix UnicodeEncodeError in BODYSTRUCTURE fetch when Content-Description, Content-ID, or Content-Location headers contain non-latin-1 characters (ASIMAP-5Y)

## [2.2.0] - 2026-02-09

### Added

- Support RFC 5258 LIST-EXTENDED: selection options (SUBSCRIBED, REMOTE, RECURSIVEMATCH), return options (SUBSCRIBED, CHILDREN), multiple mailbox patterns, CHILDINFO extended data, and `\Subscribed`/`\NonExistent` attributes (GH-417)
- Support RFC 5819 LIST-STATUS: STATUS as a LIST return option emits `* STATUS` responses alongside LIST results, reducing round-trips for clients with many folders
- Advertise LIST-EXTENDED and LIST-STATUS in CAPABILITY response

## [2.1.35] - 2025-02-09

### Changed

- MH advisory file locking disabled by default to prevent file descriptor exhaustion with large mailbox counts (ASIMAP-5Q). Set env var `ENABLE_MH_FILE_LOCKING=true` to re-enable for environments coordinating with external MH command-line clients.

## [2.1.34] - 2025-02-09

### Fixed

- Fix Drone CI docker builds: pass PYTHON_VERSION as literal build arg since plugins/docker does not expand custom environment variables

## [2.1.33] - 2025-02-09

### Fixed

- Fix orphaned asyncio task on client disconnect: cancel and await the subprocess reader task when the IMAP client connection exits
- Fix test expectations for header encoding changed by Python 3.13.12's email policy behavior

### Changed

- Pin Python version (3.13.12) across Make.rules, Dockerfile, and Drone CI to ensure consistent behavior

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
