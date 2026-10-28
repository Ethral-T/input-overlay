# AGENTS.md: house rules for Input Overlay

Read [README.md](README.md) before any work. User-facing setup (download, OBS Browser Source, Linux, and building from source) is there.

- Always start from the latest `main`. The owner sometimes edits by hand.
- Work on a branch and open a **draft PR**. Never push to or merge `main`.
- The repository is public and MIT-licensed. Do not change `LICENSE`.
- This is a Windows tray app. OBS shows the overlay as a Browser Source. When you change code, build and test locally before a PR is marked ready for review (see README, Build from source, and **CI / CD policy** below).
- Commit as `Ethral-T <82991477+Ethral-T@users.noreply.github.com>` (the owner's GitHub noreply address). Never use a personal email or a made-up noreply address.

## CI / CD policy

**Goal (agent speed).** GitHub Actions protects **release artifacts**, not every pull request. Local pytest and a local build protect `main` day to day. Actions minutes are constrained: prefer local. When minutes are exhausted, say CI could not run. Leave the workflow triggers as they are.

Ordinary pull requests and pushes to `main` run no GitHub Actions. There is no Actions check to wait on. Agents and reviewers use the local path in README, Build from source.

| When | What runs |
|---|---|
| Ordinary PR, or a push to `main` | No GitHub Actions. Local pytest and a local build are the gate. |
| Manual **Run workflow** (`workflow_dispatch` on Build or Build (Linux)) | That one workflow: tests, package, smoke, artifact upload. No GitHub release. Windows packages a PyInstaller zip and checks `lib/` hashes. Linux packages a source tarball. |
| Tag push `v*` (example `v1.2.0`) | Windows only (`.github/workflows/build.yml`): pytest, bundled-lib hash check, PyInstaller package, smoke test, artifact upload, then a **draft** GitHub release (zip and SHA-256). The tag must equal `v` plus `version.py`. |
| Tag push `linux-v*` (example `linux-v1.2.0`) | Linux only (`.github/workflows/build-linux.yml`): pytest, source tarball, smoke test, artifact upload, then a **draft** release with `--latest=false` so the Windows release stays Latest. The tag must equal `linux-v` plus `version.py`. |
| A human publishes a draft release | No workflow. Publishing is a manual click on GitHub. The Linux draft stays off Latest (`Set as the latest release` unticked) so Windows remains the public latest download. |

**Before a PR is ready for review.** Run tests locally (`pip install -r requirements-test.txt`, then `python -m pytest tests`). When the Windows tray, the packaged build, or the OBS Browser Source changes, also build and exercise that surface locally (`build.bat` on Windows, or the Linux source run in README). An ordinary PR does not need a green Actions run. Leave `pull_request` and `push` to `main` off both workflows.

**When Actions is required.** Before a real `v*` or `linux-v*` tag, and before a public Publish, run the matching workflow with **Actions → Run workflow**, or push that tag. The tag run is what creates the draft. Public release packaging is tag or hand only. A merge to `main` does not package a release. Drafts stay drafts until a human presses Publish.

**No full-matrix default.** Keep Windows and Linux Actions off ordinary PRs. A skipped review, a missing local test, or exhausted minutes is a reason to run pytest and the local build, or to say CI could not run. It is not a reason to add PR triggers.
