# AGENTS.md: house rules for Input Overlay

Read [README.md](README.md) before any work. User-facing setup (download, OBS Browser Source, Linux, and building from source) is there.

- Always start from the latest `main`. The owner sometimes edits by hand.
- Work on a branch and open a **draft PR**. Never push to or merge `main`.
- The repository is public and MIT-licensed. Do not change `LICENSE`.
- This is a Windows tray app. OBS shows the overlay as a Browser Source. When you change code, build and test locally before a PR is marked ready for review (see README, Build from source).
- GitHub Actions minutes are constrained. Prefer a local build and test. Windows CI (`.github/workflows/build.yml`) runs on `v*` tags and by hand only. Linux CI (`.github/workflows/build-linux.yml`) is the same for `linux-v*` tags.
- Commit as `Ethral-T <82991477+Ethral-T@users.noreply.github.com>` (the owner's GitHub noreply address). Never use a personal email or a made-up noreply address.
