# pbcorethat

**PBCore 2.1 XML generator for California Revealed digital preservation packages.**

`pbcorethat` generates one `<ObjectID>_PBCore.xml` metadata record per packaged audiovisual object, describing the object at the moment of file normalization and packaging. Each record combines descriptive metadata from an Archipelago **AV data baseline** CSV export with technical metadata harvested directly from the preservation, mezzanine, access, and caption files.

This is a ground-up, cross-platform Python 3 rewrite of the legacy [`pbcorethat` bash microservice](https://github.com/CAVPP/cavppers) from the California Audiovisual Preservation Project (CAVPP) `cavppers` toolset. The rewrite replaces the Homebrew-distributed bash xmlstarlet xsltproc pipeline with a single dependency-free Python script that runs on macOS, Windows, and Linux, and produces richer PBCore output.

## Features

- **Single-file, standard-library-only** — runs anywhere Python 3.9+ is installed
- **Machine-readable CSV input** — reads the California Revealed Archipelago AV data baseline export directly.
- **Selective processing** — only objects with `obj_media_type` of `Sound` or `Moving Image` are processed; `Text` and `Still Image` objects (which receive DC-based XML from a separate tool) are skipped and reported
- **Skip-and-report** — rows missing an `obj_object_identifier`, rows with no matching package folder, folders with no inventory row, and unclassified files are all reported in a run summary rather than halting the batch
- **Multipart-aware** — parses California Revealed Archipelago's `#N::` multipart delimiter in `obj_av_item_parts__ip_*` columns and aligns values across fields by part number, emitting one Physical Asset instantiation per item part
- **Technical metadata harvesting** — probes each file with MediaInfo (preferred), pymediainfo, or ffprobe, capturing dimension, codecs, frame rate, bit depth, sample rate, channels, duration, and data rates; degrades to just file size, modification date, and checksum when no probe tool is available
- **Checksum capture** — reads existing `.md5` sidecars into MD5 `instantiationIdentifier` elements
- **Caption support** — WebVTT files are described as discrete Caption File instantiations related to their access copies, and access copies with captions receive `instantiationAlternativeModes`
- **Audit-friendly** — optional timestamped log file, XSD validation, dry-run mode, and scripting-ready exit codes

## Requirements

**Required:** Python 3.9 or later. Tested and used on Python 3.13.

- macOS: preinstalled, or install via [python.org](https://www.python.org) or Homebrew
- Windows: [python.org](https://www.python.org) installer — check *Add python.exe to PATH* during setup

**Recommended** (for full technical metadata harvesting; the first one found is used):

1. [MediaInfo CLI](https://mediaarea.net/en/MediaInfo) — richest output. macOS: `brew install mediainfo`. Windows: install the CLI build and ensure `mediainfo.exe` is on PATH.
2. `pymediainfo` — `pip install pymediainfo` (requires the MediaInfo library)
3. `ffprobe` — bundled with [FFmpeg](https://ffmpeg.org)

**Optional** (for `--validate`): `pip install lxml`, with `pbcore-2_1.xsd` kept alongside the script (included in this repository).

## Installation

Clone the repository or download `pbcorethat.py`. No build or install step is needed.

```
git clone https://github.com/<your-org>/pbcorethat.git
```

## Usage

Point the script at the MARC directory containing the Object ID folders:

```
python3 pbcorethat.py /path/to/marc_folder             # macOS / Linux
py pbcorethat.py D:\batches\xxchillco                  # Windows
```

Options:

```
--csv PATH        Use a specific inventory CSV instead of the first *.csv found (preferred)
--log [PATH]      Write a timestamped log (default: into the target directory)
--validate        Validate output against the PBCore 2.1 XSD (requires lxml)
--xsd PATH        Explicit path to pbcore-2_1.xsd for --validate
--dry-run         Report what would be generated without writing anything
--version         Print version and exit
```

Exit codes: `0` clean run, `1` fatal error (bad arguments, missing or unreadable CSV), `2` completed with skips or warnings. The run summary lists every skipped item by category.

### Expected input layout

```
marc_folder/
├── TestPartner_av-baseline-data-<timestamp>.csv
├── xxchillco_000043/
│   ├── xxchillco_000043_prsv.mkv
│   ├── xxchillco_000043_prsv.mkv.md5
│   ├── xxchillco_000043_access.mp4
│   ├── xxchillco_000043_access.mp4.md5
│   ├── xxchillco_000043_access.vtt
│   └── xxchillco_000043_access.vtt.md5
└── xxchillco_000047/
    ├── xxchillco_000047_f00001_prsv.wav
    ├── xxchillco_000047_f00001_access.m4a
    ├── xxchillco_000047_f00001_access.vtt
    └── ... (parts f00002 through f00022, with sidecars)
```

Files are classified by name token: `_prsv`, `_mezz`, `_access`, and the `.vtt` extension. Part tokens of the form `_fNNNNN_` (and legacy `_pNN_`, `_rNN_`, `_tNN_`) associate derivatives with the correct preservation master and physical part. Anything else in a package folder is left untouched and listed in the run summary.

### Output

Each object folder receives `<ObjectID>_PBCore.xml`, a PBCore 2.1 `pbcoreDescriptionDocument` containing, in schema order: descriptive metadata from the CSV (identifiers, titles, subjects, description, genre, coverage, agents by role, rights); one Physical Asset instantiation per item part, populated from the aligned `ip_*` columns; one instantiation per digital file with harvested technical metadata, checksums, and derivation relations (caption → access copy → preservation master → physical item); and closing `pbcoreAnnotation` and `pbcoreExtension` elements. Existing `_PBCore.xml` files are replaced, and the replacement is logged, since the record represents the current state of the package.

The XML comments between instantiations (`<!--Physical Asset-->`, `<!--Preservation Master-->`, and so on) are human-readability aids only; `instantiationGenerations` carries the authoritative machine-readable equivalent.

### Multipart (`#N::`) fields

Archipelago exports repeatable item-part fields as `#1:: value` / `#2:: value` lines within a single cell, omitting the delimiter when only one value is present. `pbcorethat` splits these, orders them by part number, and aligns them across all `ip_*` columns. A single un-delimited value in a multipart object is applied to every part. Part counts are reconciled across `obj_av_item_parts__count` and the longest `ip_*` list.

## Configuration

Institutional values live in a constants block at the top of `pbcorethat.py` and can be edited without touching the logic: repository and access location strings, controlled-vocabulary `source` attributes, generation labels per file class, the mapping of role columns to `pbcoreCreator` / `pbcoreContributor` / `pbcorePublisher`, and MIME-type fallbacks.

## Migration from legacy cavppers pbcorethat

| | Legacy (bash) | v2 (this repo) |
|---|---|---|
| Platform | macOS via Homebrew | macOS, Windows, Linux |
| Dependencies | csvprintf, mediainfo, xmlstarlet, xsltproc | none required; MediaInfo or ffprobe recommended |
| Input CSV | Islandora 7 export, human-readable labels, positional columns | Archipelago AV data baseline export, machine-readable headers |
| Structure | `pbcoreCollection` with nested `pbcorePart` | flat `pbcoreDescriptionDocument`, vendor-style |
| Filtering | none | Sound / Moving Image only, with skip reporting |
| Captions | not supported | Caption File instantiations + `instantiationAlternativeModes` |
| Validation / logging | none | `--validate`, `--log`, `--dry-run`, exit codes |

## Known considerations

In multipart objects, every Physical Asset instantiation currently shares the object-level Item Identifier, distinguished by per-part Call Number, Temporary Identifier, and a `Part` annotation. Whether the Item Identifier itself should carry a part suffix (e.g. `xxchillco_000047_f00014`) is pending review of vendor-supplied multipart PBCore examples; see the constants and `add_physical_instantiations()` if you need to adjust the convention.

## Acknowledgments

Derived from the original `pbcorethat` in the [CAVPP `cavppers`](https://github.com/CAVPP/cavppers) toolset. PBCore is a metadata standard of the [Public Broadcasting Metadata Dictionary Project](https://pbcore.org); the bundled `pbcore-2_1.xsd` is the PBCore 2.1 schema published by WGBH.
