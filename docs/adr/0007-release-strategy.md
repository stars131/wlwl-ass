# 0007 — Tauri release strategy

- **Status:** Accepted
- **Date:** 2026-05-02

## Context

Phase 0 stood up the GUI scaffolding. Phase 1 reached feature parity with
the Qt launcher. Phase 2's question is "how do we ship binaries?" — i.e.
build matrices, signing, code-update channels, version bumping.

Constraints:

- Targets: Windows x64, macOS universal (arm64 + x64), Linux x64. Linux
  ARM is post-MVP.
- Signing: Apple Developer ID (notarisation) + Windows code-signing cert
  (EV preferable). Both cost money + admin overhead, so we ship the
  pipeline first and wire signing in once the org acquires certs.
- Updates: Tauri's `tauri-plugin-updater` reads a small `latest.json`
  manifest from a URL we control; binaries are signed with a key we
  generate (see `tauri signer generate`). The plugin lives behind a
  feature flag so dev builds don't try to hit the internet.

## Decision

1. **Builds happen via GitHub Actions** in `.github/workflows/tauri-build.yml`
   on a manual trigger (`workflow_dispatch`). Once signing is set up the
   trigger flips to `release: types: [published]` and the workflow uploads
   to the GitHub Release page.
2. **Matrix:** `ubuntu-latest`, `windows-latest`, `macos-latest`.
   - Linux ARM and Mac x64-only are deferred until users ask.
3. **Signing variables** are documented as `secrets.*` in the workflow
   file (commented out for now). Once filled the same workflow notarises
   on macOS and signs with the Windows cert; no other code changes
   required.
4. **Updater channel:** plugin scaffolding lands in Tauri config behind a
   placeholder pubkey (`__placeholder__`). When the first signed build
   ships, `npm run tauri:signer generate` produces the keypair; the
   public key is committed to `tauri.conf.json`'s `updater.pubkey` and
   the private key goes into `secrets.TAURI_SIGNING_PRIVATE_KEY`. Update
   server URL is `https://github.com/<org>/<repo>/releases/latest/download/latest.json`.
5. **Versioning:** semantic-version stamped in three places:
   - `gui/package.json :: version`
   - `gui/src-tauri/Cargo.toml :: package.version`
   - `gui/src-tauri/tauri.conf.json :: version`
   A future `scripts/bump-version.mjs` updates all three; until then,
   bumps are manual + reviewed.

## Consequences

- ✅ One artifact per OS, build matrix is reviewable in PRs.
- ✅ Signing flip is config-only — no code refactor required.
- ✅ Update channel design is consistent with how other Tauri 2 apps
  (desktop-cc-gui, etc.) ship.
- ⚠️ Until signing certs exist, Windows users see SmartScreen warnings
  and macOS users see the Gatekeeper "unidentified developer" dialog.
  Mitigation: distribute portable zips alongside installers and document
  the right-click → Open workaround for macOS.
- ⚠️ `tauri-plugin-updater` runs at app startup. We disable it in dev
  builds (`if cfg!(debug_assertions)`) so the test loop doesn't keep
  hitting placeholder URLs.

## Alternatives considered

### electron-updater on Tauri (impossible)

Not applicable; Tauri has its own updater plugin. Listed for the record.

### squirrel.windows, sparkle.macos

Mature, but each is platform-specific and would mean separate update
channels. Tauri's plugin handles both (and Linux AppImage) in one
pipeline. Rejected.

### Manual installers without auto-update

Ships faster, but every patch becomes a "go to GitHub and download"
chore for users. Rejected once we have more than a handful of users.

## Implementation notes (cookbook)

- Signing key generation:

  ```bash
  cd gui
  npx @tauri-apps/cli signer generate -w ~/.tauri/ga.key
  ```

  Add the public key to `tauri.conf.json` under `plugins.updater.pubkey`,
  put the private key (and its password) into the GitHub repository
  secrets `TAURI_SIGNING_PRIVATE_KEY` /
  `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`.

- Apple notarisation requires a paid Apple Developer ID and an
  app-specific password. The workflow expects:

  ```text
  APPLE_CERTIFICATE             base64-encoded p12
  APPLE_CERTIFICATE_PASSWORD    p12 password
  APPLE_SIGNING_IDENTITY        e.g. "Developer ID Application: GA, Inc"
  APPLE_ID                      Apple developer login email
  APPLE_PASSWORD                app-specific password
  APPLE_TEAM_ID                 e.g. "ABCDE12345"
  ```

- Windows: a code-signing cert (EV preferred) provides the SmartScreen
  reputation boost. Either the workflow signs with the cert in CI, or we
  sign locally and upload the signed binaries to the release.

## Status

Phase 2 ships:

- `tauri-build.yml` matrix workflow (manual trigger).
- This ADR.
- `tauri-plugin-updater` integration scaffolding lands in Phase 2.1 once
  the placeholder pubkey is replaced with a real one. Tracked separately.

The actual signed release is gated on the org acquiring certs.
