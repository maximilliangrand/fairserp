# Releases and deprecation

## Versioning

fairserp follows [Semantic Versioning](https://semver.org/). Before 1.0, a minor version
(0.x) may contain breaking changes; each one is listed under "Breaking" in the release notes.

What counts as the public surface:
- the native API (`/v1/*`) request and response models;
- the SerpApi migration endpoint's behavior documented in `docs/CAPABILITIES.md`;
- `FAIRSERP_*` configuration variables;
- the Python client, the TypeScript client and the engine adapter contract
  (`fairserp.engines.base.Engine`, `fairserp.testing.conformance`).

## Deprecation

A public behavior is deprecated for at least one minor release before removal: it keeps
working, logs a warning once per process, and is listed under "Deprecated" in the release
notes with its replacement.

Exceptions, which may change in any release with a note:
- an engine whose provider changes its terms, shuts down its API, or is found to violate the
  project's sourcing line (see CONTRIBUTING.md) can be disabled or moved behind an opt-in;
- rights metadata is updated whenever a provider's terms change.

## Cutting a release

1. Update `version` in `pyproject.toml`, `fairserp/__init__.py` and
   `clients/typescript/package.json`.
2. Make sure `main` is green (required CI) and regenerate `docs/CAPABILITIES.md`.
3. Tag: `git tag v0.3.0 && git push origin v0.3.0`.
4. `.github/workflows/release.yml` publishes `ghcr.io/<owner>/fairserp:0.3.0` (plus `0.3`
   and `sha-…` tags) and a GitHub release with the wheel and sdist. A tag with a suffix such
   as `v0.3.0-rc.1` is published as a prerelease.

Publishing to PyPI or npm is an owner decision and not automated. A new GHCR package
starts private; make it public once in the package settings on GitHub.
