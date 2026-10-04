# Changelog

Versions follow the `vX.Y.Z` tags that the release workflow (`.github/workflows/build.yml`) builds from. Pushing a tag that starts with `v`
builds the zip and creates a draft release.

## Unreleased

- Keyboard, mouse and controller overlay for OBS Browser Source, with presets, Stream Deck preset switching and themes.
- Controller support: Xbox, PlayStation, Switch, Switch 2 Pro, GameCube adapter and Steam Controller artwork, plus a gyro display.
- Retro Pixel controller styling; updated controller art, lighting and outlines.
- MIT licence and third-party notices; the README is written for users.
- Hardening: the overlay and build are hardened, dependencies are pinned with hashes, and the bundled libraries are checked against recorded checksums.
- Release packages now include `docs/THEMES.md` and `docs/theme-template/`.
- Tests (`pytest`) for config, themes and the request guard; they run in the build workflow.
