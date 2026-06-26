# Releasing

Releases are **version-driven**: bump the version and the `release.yml` workflow does the
rest (auto-tag → build → publish), mirroring NautilusTrader's release pipeline.

## Cut a release

1. Bump `version` in **both** `pyproject.toml` (`[project]`) and `Cargo.toml` (`[package]`)
   to the same value (the workflow fails if they differ).
2. Update `CHANGELOG.md`.
3. Merge to `main`.

`release.yml` then:
- creates the `vX.Y.Z` git tag (when this version has no tag yet) and a GitHub Release with
  auto-generated notes;
- builds 9 wheels (linux + macOS + Windows × py3.12/3.13/3.14) + an sdist and attaches
  them to the Release;
- publishes to **PyPI** (if `PYPI_PUBLISH=true`) and **crates.io** (if `CRATES_PUBLISH=true`).

If the version is unchanged, no tag is created and nothing is built/published.

## What ships where

| Target | Artifact | Gate |
|---|---|---|
| GitHub Release | wheels + sdist + notes | always (on a new version) |
| PyPI | wheels + sdist → `pip install sinopac-nt-community` | repo variable `PYPI_PUBLISH=true` |
| crates.io | the Rust crate → `cargo add sinopac-nt-community` | repo variable `CRATES_PUBLISH=true` |

The publish jobs are **off by default** so the first release produces a GitHub Release +
wheels without needing any external setup. Enable them once the one-time setup below is done.

## One-time: PyPI Trusted Publishing (no stored token)

1. On PyPI → your account → **Publishing** → add a *pending* trusted publisher:
   - PyPI Project Name: `sinopac-nt-community`
   - Owner: `Martingale42`  ·  Repository: `sinopac-nt-community`
   - Workflow name: `release.yml`  ·  Environment: `pypi`
2. In the GitHub repo → Settings → **Environments** → create environment `pypi` (optional
   protection rules).
3. Repo → Settings → Secrets and variables → Actions → **Variables** → add `PYPI_PUBLISH = true`.

## One-time: crates.io Trusted Publishing (no stored token)

1. On crates.io → the `sinopac-nt-community` crate → **Settings → Trusted Publishing** →
   add GitHub publisher: Owner `Martingale42`, Repo `sinopac-nt-community`, Workflow `release.yml`.
   (crates.io may require the first version to be published once with an API token before
   Trusted Publishing can be configured; if so, set `CARGO_REGISTRY_TOKEN` as a repo secret
   for the first publish, then switch to Trusted Publishing.)
2. Repo → Variables → add `CRATES_PUBLISH = true`.

## Recommended first-release order

To have **v0.1.0 publish to all three** on the first run, do the PyPI + crates.io setup and
set both variables **before** `release.yml` lands on `main`. Otherwise the first run produces
only the GitHub Release + wheels; enable publishing afterward and the next version bump
(e.g. `0.1.1`) publishes everywhere.

## Using the adapter from Rust

The crate is both a Python extension (`cdylib`, in the wheel) and a Rust library (`rlib`).
Pure-Rust consumers:

```toml
# via crates.io once published:
sinopac-nt-community = "0.1"
# or via git before/without a crates.io release:
sinopac-nt-community = { git = "https://github.com/Martingale42/sinopac-nt-community", tag = "v0.1.0" }
```

```rust
use sinopac_nt::{SinopacHttpClient, SinopacWebSocketClient};
```

Do **not** enable the `python`/`extension-module` features in a pure-Rust build (they are for
the wheel only). The default `high-precision` feature matches the published NautilusTrader
wheels; pass `default-features = false` if you build against a standard-precision core.

## Action pinning

All third-party actions in `ci.yml` and `release.yml` are pinned to commit SHAs, **except**
`dtolnay/rust-toolchain@1.96.0`, which is intentionally referenced by tag — the tag name
selects the Rust toolchain version (the repo's `rust-toolchain.toml` also pins `1.96.0`).
When bumping an action, re-resolve its SHA (e.g. `gh api repos/<owner>/<repo>/commits/<ref> --jq .sha`).
