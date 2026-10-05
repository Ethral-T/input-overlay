# Changelog

Versions follow the `vX.Y.Z` tags that the release workflow (`.github/workflows/build.yml`) builds from. Pushing a tag that starts with `v`
builds the zip and creates a draft release.

## Unreleased

- Numpad: an optional numeric keypad (Settings, Keyboard); numpad Enter is a key of its own, apart from the main Enter.
- Up to four controllers at once, or **Auto** (as many as are connected), each with its own artwork, gyro displays and an optional colour per player.
- Free layout: drag the keyboard, mouse and controllers anywhere in the Settings preview.
- Update notice: off by default; when turned on it looks at the GitHub releases page about once a day. "Check now" asks once.
- Controller readers each run on their own thread: the GameCube adapter's USB scan no longer freezes the other controllers every couple of seconds.
- Size: the overlay is scaled with a transform inside a box of exactly its scaled size, and shrinks to fit when it is bigger than the Browser Source,
  so it is no longer cut off at the bottom or right in OBS.
- Settings: the preview card stays in view (on the right on wide windows), Position, Size and the OBS source size sit under the preview, the page
  uses your highlight colour, the key picker fits its card, the "active" tag follows Stream Deck, tray and other-tab preset switches live, and the
  preview shows a controller set to "When connected" only while one is connected (a tickbox shows it anyway).
- Robustness: a stalled client (a frozen OBS source) no longer holds up the others; a damaged `config.json` is kept as `config.bad` and one bad preset
  is skipped; the log trims itself; failing controller readers back off; keys whose release Windows never delivered are released.
- Security: pages only run their own scripts, controller artwork is stripped of scripts and outside links, the API only answers the page itself,
  every response says not to guess the file type, and `?accent=` only takes a real colour.
- Two-PC streaming: the program only listens on the network when started with `--allow-lan <address>` naming the PC that may connect; that PC also needs a
  secret link, wrong guesses lock an address out, whole networks are refused, and `--tls-cert`/`--tls-key` give https. Settings shows the link. See the README.
- Notices: the controller drawings are described as made by the project owner, traced from public product images with the parts placed by hand.

- Keyboard, mouse and controller overlay for OBS Browser Source, with presets, Stream Deck preset switching and themes.
- Controller support: Xbox, PlayStation, Switch, Switch 2 Pro, GameCube adapter and Steam Controller artwork, plus a gyro display.
- Retro Pixel controller styling; updated controller art, lighting and outlines.
- MIT licence and third-party notices; the README is written for users.
- Hardening: the overlay and build are hardened, dependencies are pinned with hashes, and the bundled libraries are checked against recorded checksums.
- Release packages now include `docs/THEMES.md` and `docs/theme-template/`.
- Tests (`pytest`) for config, themes and the request guard; they run in the build workflow.
