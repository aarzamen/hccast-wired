# PanelBridge release path

IMPLEMENTED and UNIT-TESTED with synthetic release sets. Running it produces an
experimental ARM64 install set for the selected application, `0.1.0~dev4`; it
does not create a tag, publish anything, or establish physical acceptance.

## What is released and what is not

The selected product is the PanelBridge application for the Pi 500
LUODIER/EBPSI extended desktop. Its deliverable is the single-file installer
`panelbridge_<version>_arm64_experimental.run`, which embeds the Debian package,
the matching application and patched-sender source archive, and the package
receipt. The repository's root Python wheel (`uv build`) packages only the
legacy MIT HCCAST USB driver. A successful wheel build delivers nothing of the
wireless product and is not part of this path.

PanelBridge is GPL-3.0-or-later because its sender modifies GNOME Network
Displays. The package therefore installs the source archive next to the binary
at `usr/share/panelbridge/panelbridge-app-source.tar.gz`, with `LICENSE`,
`THIRD_PARTY_NOTICES.md` and the Debian `copyright` file. The legacy driver keeps
the root MIT license and is not covered by these notices.

## Steps

1. **Freeze the source.** Every file that ships must be in `SOURCE_FILES` in
   `packaging_tools/source_bundle.py`. Commit before building; the builders read
   the working tree.
2. **Build the worker on the target platform.** On the Pi 500 (Debian 13 ARM64)
   run `native/wfd/build-worker.sh` from a checkout placed in a neutral
   directory, not a home directory, then strip the result:

   ```bash
   strip --strip-all -o panelbridge-wfd-worker vendor/panelbridge-wfd-graceful-build/build/src/panelbridge-wfd-worker
   ```

   The retained development worker fails the release check on purpose: its
   runtime search path and debug records name private build paths.
3. **Optionally record dependencies.** Run `dpkg-shlibdeps` against the stripped
   worker and write the JSON receipt described in `build_package.py`.
4. **Assemble and inspect.** From the checkout root, as the unprivileged owner:

   ```bash
   python3 -I apps/panelbridge/packaging_tools/release.py \
     --project-root "$PWD" --output-dir output/release-<date> \
     --worker /absolute/path/to/panelbridge-wfd-worker
   ```

   The orchestrator builds `output/release-<date>/source`, `package` and
   `installer` in order, reads every artifact back, and writes
   `RELEASE_MANIFEST.json` only after the cross-checks pass.
5. **Review the manifest and the artifacts** before any separately authorized
   export. Publication of source, artifacts or releases always needs its own
   explicit assignment under [AGENTS.md](../../AGENTS.md).

Without `--worker` the orchestrator stops after the source bundle. That stage
is platform independent and runs in CI on every push to prove the public tree
still produces a clean, complete source delivery.

## What the orchestrator checks

- The source bundle contains every patched-sender input (`PATCHED_SENDER_SOURCES`
  in `release.py`): the pinned upstream archive and its pin record, all four
  patches, the build recipe, every PanelBridge native file, and both notices.
  The upstream archive must hash to the reviewed pin.
- The package carries that exact source archive, the worker, the provenance
  record, and notice files identical to the bundled `LICENSE` and
  `THIRD_PARTY_NOTICES.md`.
- The worker is an AArch64 ELF with no home-directory text, no development
  staging or temporary build paths, and no debug sections.
- The receipt names the version, the worker hash and the artifact hashes it
  was built from, and does not claim release readiness or a verified
  source-to-binary match.
- The installer embeds exactly the package, source archive and receipt that
  were just inspected.
- No installed text member carries a personal path, private key or address.

## What it does not check

- That the worker was compiled from the bundled source. The receipt records
  `worker_source_match_verified: false`; reproducible native builds are future
  work.
- Distro dependency sources, offline closure, or installation on a fresh image.
  Those remain the integrator's physical gates.
- Visible playback, repeatable start/stop, reconnect, reboot or power-cycle
  recovery on the Pi 500. The manifest states
  `physical_acceptance: not_established_by_this_build`; the owner's record is
  [docs/PI500.md](../../docs/PI500.md) and the claim ledger is
  [docs/VALIDATION.md](../../docs/VALIDATION.md).

The manifest's `release_ready` is always `false`. Flipping it is not a build
step; it is the integrator's decision after the acceptance gates in
[docs/SCOPE.md](../../docs/SCOPE.md) are met and recorded.
