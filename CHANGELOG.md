# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Item Identifier convention for physical part instantiations in multipart
  objects is pending review of vendor-supplied multipart PBCore examples.
  Parts currently share the object-level Item Identifier, distinguished by
  per-part Call Number, Temporary Identifier, and a `Part` annotation.

## [2.0.0] - 2026-07-27

Ground-up rewrite of the legacy CAVPP `pbcorethat` bash microservice
(distributed via the Homebrew `cavppers` cask) as a single cross-platform
Python 3 script.

### Added

- Cross-platform support: macOS, Windows, and Linux with no adaptation steps;
  Python 3.9+ standard library only, no required third-party packages.
- Input support for the Archipelago **AV data baseline** CSV export with
  machine-readable headers, replacing the Islandora 7-style export with
  human-readable labels and positional columns.
- Parsing of Archipelago's `#N::` multipart delimiter in
  `obj_av_item_parts__ip_*` columns, with values aligned across fields by
  part number and one Physical Asset instantiation emitted per item part.
  Single un-delimited values apply to every part; part counts are reconciled
  across `obj_av_item_parts__count` and the longest `ip_*` value list.
- Media-type filtering: only rows with `obj_media_type` of `Sound` or
  `Moving Image` are processed. Text and Still Image rows (handled by a
  separate DC-based XML tool) are skipped and reported.
- Skip-and-report behavior for rows missing `obj_object_identifier`
  (reported with node ID and title), rows with no matching package folder,
  package folders with no inventory row, and files not classifiable as
  prsv / mezz / access / vtt.
- Technical metadata harvesting from the digital files via a tiered probe
  backend: MediaInfo CLI (preferred), pymediainfo, then ffprobe, with
  graceful degradation to file size, modification date, and checksum-only
  output when no probe tool is available. Harvested fields include
  dimensions, codec, frame rate, bit depth, aspect ratio, frame count,
  scan type, chroma subsampling, sample rate, channels, duration, file
  size, and data rate.
- MD5 sidecar (`*.md5`) values captured as checksum
  `instantiationIdentifier` elements.
- WebVTT caption support: caption files are described as discrete
  "Caption File" instantiations related to their access copies, and access
  copies with matching captions receive `instantiationAlternativeModes`.
- Per-part Call Number and Temporary Identifier on Physical Asset
  instantiations, drawn from `ip_call_number` and `ip_temporary_id` with
  fallback to the object-level values.
- Derivation relations linking each caption file to its access copy, each
  access/mezzanine copy to its preservation master, and each preservation
  master to the physical item, matched by part token (`_fNNNNN_`, with
  legacy `_pNN_` / `_rNN_` / `_tNN_` also recognized).
- `--log [PATH]` timestamped run log, `--validate` XSD validation via lxml
  against `pbcore-2_1.xsd` (resolved from the script's own directory,
  overridable with `--xsd`), `--dry-run` preview mode, `--csv` explicit
  inventory selection, and `--version`.
- Exit codes for scripting integration: `0` clean, `1` fatal error,
  `2` completed with skips or warnings.
- End-of-run summary reporting every skipped or anomalous item by category.
- Institutional constants block (locations, vocabulary sources, generation
  labels, agent role mapping, MIME fallbacks) editable without touching
  program logic.

### Changed

- Output structure is now a flat, vendor-style PBCore 2.1
  `pbcoreDescriptionDocument` with schema-valid element ordering, replacing
  the legacy `pbcoreCollection` / nested `pbcorePart` structure.
- File instantiation dates use `dateType="File Modification"` from the
  filesystem, rather than implying a transfer-created date the tool cannot
  verify.
- `instantiationStandard` follows the vendor convention of the file
  extension, with the container's long-form name recorded in an
  "Container Format" annotation.
- Existing `<ObjectID>_PBCore.xml` files are replaced on rerun (and the
  replacement logged), reflecting that the record describes the package's
  current state.

### Removed

- Homebrew distribution and all external tool dependencies of the bash
  version: `csvprintf`, `xmlstarlet`, `xsltproc`, and the XSLT stylesheets
  (`csv2pbcore.xsl` and related transforms).
- Requirement to maintain a dedicated Islandora-style CSV export solely for
  this tool.

[Unreleased]: https://github.com/your-org/pbcorethat/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/your-org/pbcorethat/releases/tag/v2.0.0
