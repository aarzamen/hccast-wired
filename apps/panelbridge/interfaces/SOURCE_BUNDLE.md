# Application and GND source bundle

IMPLEMENTED builder; UNIT-TESTED with synthetic application trees and the exact
pinned upstream archive. Producing the final application snapshot is a separate
integrator step after source freeze. This tool does not publish anything.

## Scope

The archive contains the explicitly listed PanelBridge files and the unchanged
GNOME Network Displays 0.99.0 source archive. The allowlist in
`packaging_tools/source_bundle.py` covers current Python app/helper/UI sources,
tests, interfaces, packaging scripts/service/policy files, all four native
patches, the build recipe, every current native C/header file, original SVG,
rendered PNG icons, app license and notices. Every allowlisted file is required.
The new rescue service module, launcher, unit and tests are included.

The allowlist is exact, not an extension-based or recursive export rule. A new
application, installation, removal or build input requires an explicit allowlist
update. Unlisted files are excluded; they are not automatically classified as
irrelevant to a future binary. The integrator must review the final inventory
against the delivered version before calling the source delivery complete.

Only `vendor/gnome-network-displays-0.99.0.tar.gz` is included from `vendor`.
Its required SHA-256 is
`b6314d25be7589c621b106c1712b00277c9b4d01d4796a78e987f10c0c1d1400`.
The builder verifies both its bytes and the exact `expected` assignment in
`native/wfd/build-worker.sh`. A different archive or recipe pin is refused.
The recorded upstream commit is `eba3ce5dada5f6065e77041c973e73e3c2886bfd`.

Expanded vendor source/build directories, caches, repository metadata, evidence,
runtime enrollment/bindings, credentials, and other unlisted files are excluded.
The original archive retains its upstream notices and original internal metadata;
it is not rewritten or repacked under a different hash.

**This is application plus GND source only.** Distro libraries/tools supplied
separately through APT are not bundled here, and their corresponding source,
patches, package rules and notices are not assembled by this tool. The builder
does not establish a complete dependency-source delivery for a redistributed
binary or system image. Follow `THIRD_PARTY_NOTICES.md` and the final distribution
review for those remaining requirements. No binary hash, source-to-binary match,
installation-information completeness, release approval or legal clearance is
inferred from an archive build.

## API and CLI

```python
from packaging_tools.source_bundle import build_bundle

result = build_bundle(project_root, output_dir, name="panelbridge-app-source.tar.gz")
# result.path, result.sha256, result.file_count
```

`project_root` is the repository containing `apps/panelbridge`. `output_dir` must
be a descendant of that repository owned by the invoking user. Relative output
paths are interpreted from `project_root`, never the process working directory.
Parent traversal, symlinked directory components, outside output paths, and unsafe
archive filenames are refused. New output directories use owner-only permissions.
The invoking user must own the project directory.

From the project root, the integrator can run this after the final freeze:

```bash
python3 apps/panelbridge/packaging_tools/source_bundle.py \
  --project-root "$PWD" \
  --output-dir artifacts/source \
  --name panelbridge-app-source.tar.gz
```

The builder uses only the Python standard library. The CLI prints JSON with the
artifact path, SHA-256, entry count and explicit incomplete-dependency-source
flag, or returns exit code 2 with a refusal reason. Errors identify allowlisted
relative filenames without echoing detected private text.

Artifact creation uses an exclusive new file with owner-only permissions.
There is no overwrite or delete option. Existing artifacts and destination
symlinks are refused. All source/privacy/archive checks finish before creating
the output directory or artifact. An I/O failure while saving can leave a partial
new file; it is preserved and reported as failure, never reported as a successful
bundle. The caller must choose another name or separately handle that file.

## Archive and manifest

The outer archive is gzip-compressed USTAR, with lexically ordered regular-file
entries under `panelbridge-source/`. It has no links, special files, absolute
paths, parent traversal or directory entries. UID, GID and modification time are
zero; owner/group names are empty. Source permissions are normalized to 0644,
with a fixed reviewed set of build/launcher files using 0755. Gzip uses an empty
filename, zero timestamp, fixed compression level and no host-specific header.
Input mtimes, permissions, output path/name and umask do not affect archive bytes.
Byte reproducibility is tested under the same Python/zlib implementation; a
different compressor implementation is not assumed to emit identical DEFLATE.

`SOURCE_MANIFEST.json` records each payload's relative path, SHA-256, byte count
and normalized mode, including the generated `UPSTREAM_PIN.json`. The manifest
does not hash itself. The returned outer archive hash covers every byte including
the manifest. The pin record identifies the archive/version/commit, build recipe
and successful pin check. Both JSON documents have deterministic formatting.

The source manifest explicitly records:

- `complete_binary_dependency_source: false`
- `distro_dependency_sources_included: false`
- `source_binary_match_verified: false`

Source bytes and hashes are read fresh on every invocation. Only the reviewed
upstream pin is fixed. Each source read checks size and metadata before/after
reading; a concurrently changing file fails. This is not a transaction across
the whole checkout. Freeze edits before producing the matching delivery snapshot.

## File and privacy checks

Sources are opened relative to directory descriptors with no-follow flags.
Symlinks, hard links, FIFOs, sockets, directories used as files and unreadable
inputs are refused. Text must be UTF-8 without NUL bytes; the only allowed binary
payloads are the exact pinned archive and named PNG assets with PNG signatures.
Each input is bounded to 16 MiB and the app snapshot to 64 MiB. The pinned archive
is inspected without extracting it: member paths, duplicates, types, counts,
sizes and text privacy are checked before it is copied unchanged.

Public text is rejected on Unix/macOS home paths, Windows user-home paths, PEM
private-key markers, and colon/hyphen/dotted MAC-address representations. PNG
bytes are also scanned for directly embedded text. Compressed upstream members
are scanned after decompression. The builder does not rewrite, redact or scrub
the input; a rejection needs source review by the integrator.

The synthetic MAC exception is deliberately narrow:

1. Only allowlisted files in `tests/` may use the local-unicast fixture namespace
   with hexadecimal compact prefix `0200000000` and any final octet.
2. Only `tests/test_rescue_service.py` may also use compact address
   `030000000001`, the existing invalid-multicast rejection fixture.
3. Neither exception applies to implementation, packaging, documentation,
   assets or upstream members. No other locally administered address range is
   automatically considered synthetic.

This pattern gate and exact allowlist do not identify every possible secret,
personal identifier or compressed-image metadata encoding. They are concrete
export checks, not a claim of comprehensive privacy review. The integrator still
reviews the final inventory and artifact before any separately authorized export.

## Verification

`tests/test_source_bundle.py` extracts synthetic builds and checks every manifest
digest; requires all four patches/build/native inputs; compares archive bytes
after metadata/name/umask changes; checks changed-source hashes, exclusion,
pin mismatch, missing required files, privacy exception boundaries, unsafe file
types, output containment and no-overwrite behavior; and executes the CLI.
It does not build the changing actual application snapshot.
