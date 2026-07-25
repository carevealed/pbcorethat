#!/usr/bin/env python3
"""
pbcorethat.py — California Revealed PBCore XML generator (v2)

A cross-platform (macOS / Windows / Linux) Python 3 rewrite of the legacy
CAVPP `pbcorethat` bash microservice.

Reads an Archipelago "AV data baseline" CSV export (machine-readable headers)
plus a directory of packaged Object ID folders, and writes one
<ObjectID>_PBCore.xml per object, describing the object at the moment of
packaging: descriptive metadata from the CSV, physical item parts (when
present), and one instantiation per digital file (prsv / mezz / access / vtt)
with technical metadata harvested from the files themselves.

Only rows with obj_media_type of "Sound" or "Moving Image" are processed.
Rows with no obj_object_identifier, rows whose folder is not found, and
folders with no matching row are skipped and reported at the end of the run.

Technical metadata harvesting uses, in order of preference:
  1. MediaInfo CLI (`mediainfo --Output=JSON`) — richest output
  2. pymediainfo (if installed, with libmediainfo)
  3. ffprobe (from FFmpeg)
If none are available the tool still runs, harvesting only file size,
modification date, and MD5 sidecar checksums, and notes the limitation.

Usage:
    python3 pbcorethat.py /path/to/MARC_folder
    py pbcorethat.py D:\\batches\\xxchillco          (Windows)

Options:
    --csv PATH        Use a specific CSV instead of the first *.csv found in
                      the target directory.
    --log [PATH]      Write a timestamped log. Without PATH, the log is
                      written into the target directory as
                      pbcorethat_YYYYMMDD-HHMMSS.log
    --validate        Validate output against pbcore-2_1.xsd (requires lxml;
                      looks for the XSD next to this script unless --xsd).
    --xsd PATH        Explicit path to pbcore-2_1.xsd for --validate.
    --dry-run         Report what would be done without writing XML.
    --version         Print version and exit.

Exit codes:
    0  everything eligible was generated cleanly
    1  fatal error (bad arguments, no CSV, unreadable inventory)
    2  run completed, but items were skipped or warnings were raised
"""

import argparse
import csv
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

__version__ = "2.0.0"

# ---------------------------------------------------------------------------
# Institutional constants — edit these to tune output without touching logic
# ---------------------------------------------------------------------------

PBCORE_NS = "http://www.pbcore.org/PBCore/PBCoreNamespace.html"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
SCHEMA_LOCATION = (
    "http://www.pbcore.org/PBCore/PBCoreNamespace.html "
    "https://raw.githubusercontent.com/WGBH/PBCore_2.1/master/pbcore-2.1.xsd"
)

ORG_SOURCE = "California Revealed"
PRSV_LOCATION = "California Revealed Digital Repository"
ACCESS_LOCATION = "californiarevealed.org"
SUBJECT_TOPIC_SOURCE = "Library of Congress Subject Headings"
SUBJECT_ENTITY_SOURCE = "Library of Congress Name Authority File"
GENRE_SOURCE = "Library of Congress Genre/Form Terms"
SPATIAL_SOURCE = "Library of Congress Name Authority File"
TEMPORAL_SOURCE = "Library of Congress Extended Date/Time Format"
LANGUAGE_SOURCE = "ISO 639.2"
COUNTRY_AUTHORITY = "ISO 3166.1"
PHYSICAL_FORMAT_SOURCE = "PBCore instantiationPhysical Controlled Vocabulary"
GENERATIONS_SOURCE = "PBCore instantiationGenerations Controlled Vocabulary"
RELATION_TYPE_SOURCE = "PBCore relationType"

PBCORE_SUFFIX = "_PBCore.xml"

# filename token → generation label / storage location
FILE_CLASSES = {
    "prsv":   {"generation": "Preservation Master", "location": PRSV_LOCATION},
    "mezz":   {"generation": "Mezzanine Copy",      "location": ACCESS_LOCATION},
    "access": {"generation": "Access Copy",         "location": ACCESS_LOCATION},
    "vtt":    {"generation": "Caption File",        "location": ACCESS_LOCATION},
}

# creator-role columns are obj_creator__name_label_<role>_role; the role key
# decides which PBCore agent element the name lands in.
CREATOR_ROLES = {
    "creator", "producer", "director", "writer", "interviewer", "performer",
    "filmmaker", "author", "composer", "artist",
}
PUBLISHER_ROLES = {"publisher", "distributor"}
RIGHTS_ROLES = {"copyright_holder"}          # handled under pbcoreRightsSummary

MEDIA_TYPES_PROCESSED = {"sound", "moving image"}

# extension → MIME fallback when the probe backend doesn't report one
MIME_FALLBACK = {
    "mkv": "video/x-matroska", "mov": "video/quicktime", "mp4": "video/mp4",
    "wav": "audio/vnd.wave", "m4a": "audio/mp4", "mp3": "audio/mpeg",
    "flac": "audio/flac", "mxf": "application/mxf", "avi": "video/x-msvideo",
    "vtt": "text/vtt", "dv": "video/dv",
}

# splits multi-value descriptive cells (subjects, genres, names, languages)
VALUE_SPLIT_RE = re.compile(r"[;\n]")
# Archipelago multipart delimiter:  #1:: value
MULTIPART_RE = re.compile(r"#(\d+)::\s?")
# part token inside filenames: xxchillco_000049_f00001_prsv.wav
PART_TOKEN_RE = re.compile(r"_(?:f|p|r|t)(\d{1,6})_")
MD5_RE = re.compile(r"\b[0-9a-fA-F]{32}\b")

log = logging.getLogger("pbcorethat")

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def clean(value):
    """Strip a CSV cell; return '' for None."""
    return (value or "").strip()


def split_values(cell):
    """Split a descriptive cell on ';' / newlines into a clean list."""
    return [v.strip() for v in VALUE_SPLIT_RE.split(cell or "") if v.strip()]


def split_multipart(cell):
    """
    Split an Archipelago multipart cell ('#1:: a\\n#2:: b') into an ordered
    list. A cell without the delimiter is a single-item list. Empty → [].
    """
    cell = (cell or "").strip()
    if not cell:
        return []
    matches = list(MULTIPART_RE.finditer(cell))
    if not matches:
        return [cell]
    parts = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(cell)
        parts[int(m.group(1))] = cell[m.end():end].strip()
    return [parts[k] for k in sorted(parts)]


def part_value(values, index, part_count):
    """
    Value for physical part `index` (1-based) from a multipart list.
    A single value with multiple parts applies to every part.
    """
    if not values:
        return ""
    if len(values) == 1 and part_count > 1:
        return values[0]
    if index - 1 < len(values):
        return values[index - 1]
    return ""


def seconds_to_hms(seconds):
    """Format float seconds as HH:MM:SS (vendor-style duration)."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def read_md5_sidecar(path):
    """Return the first 32-hex-digit checksum from <path>.md5, or ''."""
    sidecar = path.with_name(path.name + ".md5")
    if not sidecar.is_file():
        return ""
    try:
        text = sidecar.read_text(errors="replace")
    except OSError as err:
        log.warning("Could not read sidecar %s: %s", sidecar.name, err)
        return ""
    m = MD5_RE.search(text)
    if not m:
        log.warning("Sidecar %s exists but contains no MD5 value", sidecar.name)
        return ""
    return m.group(0)


# ---------------------------------------------------------------------------
# Technical metadata probing (MediaInfo preferred, ffprobe fallback)
# ---------------------------------------------------------------------------


class Prober:
    """Wraps whichever probing backend is available on this system."""

    def __init__(self):
        self.backend = None
        self.tool_path = None
        self._pymediainfo = None

        mi = shutil.which("mediainfo")
        if mi:
            self.backend, self.tool_path = "mediainfo", mi
            return
        try:
            import pymediainfo  # noqa: F401
            self._pymediainfo = pymediainfo
            self.backend = "pymediainfo"
            return
        except ImportError:
            pass
        ff = shutil.which("ffprobe")
        if ff:
            self.backend, self.tool_path = "ffprobe", ff

    def describe(self):
        return {
            "mediainfo": f"MediaInfo CLI ({self.tool_path})",
            "pymediainfo": "pymediainfo library",
            "ffprobe": f"ffprobe ({self.tool_path})",
            None: "none — file size / dates / checksums only",
        }[self.backend]

    def probe(self, path):
        """
        Return a normalized dict:
        {'general': {...}, 'video': [ {...} ], 'audio': [ {...} ]}
        or None if probing failed / unavailable.
        """
        try:
            if self.backend == "mediainfo":
                return self._normalize_mediainfo(self._run_mediainfo(path))
            if self.backend == "pymediainfo":
                data = self._pymediainfo.MediaInfo.parse(str(path)).to_data()
                return self._normalize_mediainfo({"media": data})
            if self.backend == "ffprobe":
                return self._normalize_ffprobe(self._run_ffprobe(path))
        except Exception as err:  # a probe failure must never kill the run
            log.warning("Technical probe failed for %s: %s", path.name, err)
        return None

    def _run_mediainfo(self, path):
        out = subprocess.run(
            [self.tool_path, "--Output=JSON", str(path)],
            capture_output=True, text=True, timeout=300, check=True,
        )
        return json.loads(out.stdout)

    def _run_ffprobe(self, path):
        out = subprocess.run(
            [self.tool_path, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=300, check=True,
        )
        return json.loads(out.stdout)

    @staticmethod
    def _normalize_mediainfo(data):
        tracks = (data.get("media") or {}).get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]
        result = {"general": {}, "video": [], "audio": []}
        for t in tracks:
            kind = (t.get("@type") or t.get("track_type") or "").lower()
            get = lambda *keys: next(
                (str(t[k]).strip() for k in keys if t.get(k) not in (None, "")), "")
            if kind == "general":
                result["general"] = {
                    "format": get("Format", "format"),
                    "mime": get("InternetMediaType", "internet_media_type"),
                    "file_size": get("FileSize", "file_size"),
                    "duration_s": get("Duration", "duration"),
                    "overall_bit_rate": get("OverallBitRate", "overall_bit_rate"),
                }
            elif kind == "video":
                encoding = get("Format", "format")
                version = get("Format_Version", "format_version")
                profile = get("Format_Profile", "format_profile")
                if version:
                    encoding = f"{encoding} {version}".strip()
                if profile:
                    encoding = f"{encoding} {profile}".strip()
                result["video"].append({
                    "encoding": encoding,
                    "width": get("Width", "width"),
                    "height": get("Height", "height"),
                    "frame_rate": get("FrameRate", "frame_rate"),
                    "bit_depth": get("BitDepth", "bit_depth"),
                    "aspect_ratio": get("DisplayAspectRatio_String",
                                        "other_display_aspect_ratio",
                                        "DisplayAspectRatio",
                                        "display_aspect_ratio"),
                    "frame_count": get("FrameCount", "frame_count"),
                    "scan_type": get("ScanType", "scan_type"),
                    "chroma": get("ChromaSubsampling", "chroma_subsampling"),
                    "bit_rate": get("BitRate", "bit_rate"),
                    "duration_s": get("Duration", "duration"),
                })
            elif kind == "audio":
                result["audio"].append({
                    "encoding": get("Format", "format"),
                    "sampling_rate": get("SamplingRate", "sampling_rate"),
                    "bit_depth": get("BitDepth", "bit_depth"),
                    "channels": get("Channels", "channel_s", "channel_count"),
                    "bit_rate": get("BitRate", "bit_rate"),
                    "duration_s": get("Duration", "duration"),
                })
        # pymediainfo reports durations in ms
        for section in [result["general"], *result["video"], *result["audio"]]:
            d = section.get("duration_s")
            if d:
                try:
                    val = float(d)
                    if val > 100000:  # almost certainly milliseconds
                        section["duration_s"] = str(val / 1000.0)
                except ValueError:
                    pass
        return result

    @staticmethod
    def _normalize_ffprobe(data):
        fmt = data.get("format", {})
        result = {"general": {
            "format": fmt.get("format_long_name") or fmt.get("format_name", ""),
            "mime": "",
            "file_size": fmt.get("size", ""),
            "duration_s": fmt.get("duration", ""),
            "overall_bit_rate": fmt.get("bit_rate", ""),
        }, "video": [], "audio": []}
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                fr = s.get("avg_frame_rate") or s.get("r_frame_rate") or ""
                if "/" in fr:
                    num, _, den = fr.partition("/")
                    try:
                        fr = f"{float(num) / float(den):.3f}".rstrip("0").rstrip(".")
                    except (ValueError, ZeroDivisionError):
                        fr = ""
                friendly = {"ffv1": "FFV1", "h264": "H.264/AVC",
                            "hevc": "H.265/HEVC", "prores": "Apple ProRes",
                            "mjpeg": "Motion JPEG", "mpeg2video": "MPEG-2",
                            "v210": "Uncompressed 10-bit 4:2:2 (v210)",
                            "dvvideo": "DV"}
                encoding = (friendly.get(s.get("codec_name", ""))
                            or s.get("codec_long_name")
                            or s.get("codec_name", ""))
                profile = s.get("profile") or ""
                if profile and profile.lower() not in encoding.lower():
                    encoding = f"{encoding} {profile}".strip()
                result["video"].append({
                    "encoding": encoding,
                    "width": str(s.get("width", "") or ""),
                    "height": str(s.get("height", "") or ""),
                    "frame_rate": fr,
                    "bit_depth": str(s.get("bits_per_raw_sample", "") or ""),
                    "aspect_ratio": s.get("display_aspect_ratio", ""),
                    "frame_count": str(s.get("nb_frames", "") or ""),
                    "scan_type": {"progressive": "Progressive",
                                  "tt": "Interlaced", "bb": "Interlaced",
                                  "tb": "Interlaced", "bt": "Interlaced"}.get(
                                      s.get("field_order", ""), ""),
                    "chroma": "",
                    "bit_rate": s.get("bit_rate", ""),
                    "duration_s": s.get("duration", ""),
                })
            elif s.get("codec_type") == "audio":
                result["audio"].append({
                    "encoding": (s.get("codec_long_name")
                                 or s.get("codec_name", "")),
                    "sampling_rate": s.get("sample_rate", ""),
                    "bit_depth": str(s.get("bits_per_raw_sample", "")
                                     or s.get("bits_per_sample", "") or ""),
                    "channels": str(s.get("channels", "") or ""),
                    "bit_rate": s.get("bit_rate", ""),
                    "duration_s": s.get("duration", ""),
                })
        return result


# ---------------------------------------------------------------------------
# XML building
# ---------------------------------------------------------------------------


def el(parent, tag, text="", **attrs):
    """Append a namespaced child element (skipped entirely if no text and no
    attrs are meaningful — caller guards on text where required)."""
    node = ET.SubElement(parent, f"{{{PBCORE_NS}}}{tag}",
                         {k: v for k, v in attrs.items() if v})
    if text:
        node.text = str(text)
    return node


def maybe(parent, tag, text, **attrs):
    """Append the element only when text is non-empty."""
    text = clean(text)
    if text:
        return el(parent, tag, text, **attrs)
    return None


class ObjectRecord:
    """One eligible CSV row, with multipart fields pre-split."""

    def __init__(self, row):
        self.row = {k: clean(v) for k, v in row.items() if k}
        self.object_id = self.row.get("obj_object_identifier", "")
        self.media_type = self.row.get("obj_media_type", "")
        self.partner = self.row.get("obj_partner_name", "")
        self.stream = self.row.get("obj_prod_stream", "")
        # pre-split every ip_* multipart column
        self.ip = {
            key: split_multipart(value)
            for key, value in self.row.items()
            if key.startswith("obj_av_item_parts__ip_")
        }
        try:
            declared = int(self.row.get("obj_av_item_parts__count", "") or 0)
        except ValueError:
            declared = 0
        longest = max((len(v) for v in self.ip.values()), default=0)
        self.part_count = max(declared, longest, 0)

    def get(self, key):
        return self.row.get(key, "")

    def ip_part(self, key, index):
        return part_value(self.ip.get(f"obj_av_item_parts__{key}", []),
                          index, self.part_count)

    def has_physical_data(self):
        return any(v for v in self.ip.values()) or self.part_count > 0


def add_agents(root, record):
    """pbcoreCreator / pbcoreContributor / pbcorePublisher from role columns."""
    role_re = re.compile(r"obj_creator__name_label_(\w+?)_role$")
    creators, contributors, publishers = [], [], []
    for column, cell in record.row.items():
        m = role_re.match(column)
        if not m or not cell:
            continue
        role_key = m.group(1)
        if role_key in RIGHTS_ROLES:
            continue  # copyright holder is emitted with rights
        role_label = role_key.replace("_", " ").title()
        for name in split_values(cell):
            if role_key in CREATOR_ROLES:
                creators.append((name, role_label))
            elif role_key in PUBLISHER_ROLES:
                publishers.append((name, role_label))
            else:
                contributors.append((name, role_label))
    for name, role in creators:
        wrap = el(root, "pbcoreCreator")
        el(wrap, "creator", name)
        el(wrap, "creatorRole", role)
    for name, role in contributors:
        wrap = el(root, "pbcoreContributor")
        el(wrap, "contributor", name)
        el(wrap, "contributorRole", role)
    for name, role in publishers:
        wrap = el(root, "pbcorePublisher")
        el(wrap, "publisher", name)
        el(wrap, "publisherRole", role)


def add_descriptive(root, record):
    r = record.get
    maybe(root, "pbcoreAssetType", r("obj_asset_type__label"))
    maybe(root, "pbcoreAssetDate", r("obj_created_date__date_free"),
          dateType="Created")
    maybe(root, "pbcoreAssetDate", r("obj_published_date__date_free"),
          dateType="Published")

    maybe(root, "pbcoreIdentifier", record.object_id,
          source=ORG_SOURCE, annotation="Object Identifier")
    maybe(root, "pbcoreIdentifier", r("obj_project_identifier"),
          source=ORG_SOURCE, annotation="Project Identifier")
    maybe(root, "pbcoreIdentifier", r("obj_ia_url__url"),
          source=ORG_SOURCE, annotation="Internet Archive URL")
    maybe(root, "pbcoreIdentifier", r("obj_nid_link"),
          source=ORG_SOURCE, annotation="California Revealed Node URL")
    maybe(root, "pbcoreIdentifier", r("obj_ark_identifier"),
          source="California Digital Library", annotation="ARK Identifier")
    maybe(root, "pbcoreIdentifier", r("obj_oclc_number"),
          source="OCLC", annotation="OCLC Number")

    maybe(root, "pbcoreTitle", r("label"), titleType="Main")
    maybe(root, "pbcoreTitle", r("obj_alternative_title"),
          titleType="Alternative")
    maybe(root, "pbcoreTitle", r("obj_series_title"), titleType="Series")

    for topic in split_values(r("obj_subject_topic__label")):
        el(root, "pbcoreSubject", topic, subjectType="Topic",
           source=SUBJECT_TOPIC_SOURCE)
    for entity in split_values(r("obj_subject_entity__label")):
        el(root, "pbcoreSubject", entity, subjectType="Entity",
           source=SUBJECT_ENTITY_SOURCE)

    maybe(root, "pbcoreDescription", r("obj_description"),
          descriptionType="Content Summary")

    for genre in split_values(r("obj_genre__label")):
        el(root, "pbcoreGenre", genre, source=GENRE_SOURCE)

    for place in split_values(r("obj_spatial_coverage__label")):
        wrap = el(root, "pbcoreCoverage")
        el(wrap, "coverage", place, source=SPATIAL_SOURCE)
        el(wrap, "coverageType", "Spatial")
    temporal = r("obj_temporal_coverage__date_free")
    if temporal:
        wrap = el(root, "pbcoreCoverage")
        el(wrap, "coverage", temporal, source=TEMPORAL_SOURCE)
        el(wrap, "coverageType", "Temporal")

    add_agents(root, record)

    partner = record.partner or ORG_SOURCE
    for text, annotation in (
        (r("obj_copyright_statement"), "Copyright Statement"),
        (r("obj_creator__name_label_copyright_holder_role"),
         "Copyright Holder"),
        (r("obj_copyright_holder_info"), "Copyright Holder Info"),
        (r("obj_copyright_date__date_free"), "Copyright Date"),
        (r("obj_copyright_notice"), "Copyright Notice"),
    ):
        if clean(text):
            wrap = el(root, "pbcoreRightsSummary")
            el(wrap, "rightsSummary", clean(text),
               source=partner, annotation=annotation)


def add_physical_instantiations(root, record):
    """One 'Physical Asset' instantiation per item part (AV stream objects)."""
    if not record.has_physical_data():
        return
    part_count = max(record.part_count, 1)
    partner = record.partner or ORG_SOURCE
    for i in range(1, part_count + 1):
        root.append(ET.Comment("Physical Asset"
                               + (f" - Part {i}" if part_count > 1 else "")))
        inst = el(root, "pbcoreInstantiation")
        maybe(inst, "instantiationIdentifier", record.object_id,
              source=ORG_SOURCE, annotation="Item Identifier")
        call_no = (record.ip_part("ip_call_number", i)
                   or record.get("obj_call_number"))
        maybe(inst, "instantiationIdentifier", call_no,
              source=partner, annotation="Call Number")
        temp_id = (record.ip_part("ip_temporary_id", i)
                   or record.get("obj_temporary_id"))
        maybe(inst, "instantiationIdentifier", temp_id,
              source=partner, annotation="Temporary Identifier")
        maybe(inst, "instantiationPhysical",
              record.ip_part("ip_gauge_and_format", i),
              source=PHYSICAL_FORMAT_SOURCE)
        el(inst, "instantiationLocation", partner)
        maybe(inst, "instantiationMediaType",
              record.ip_part("ip_media_type", i) or record.media_type)
        maybe(inst, "instantiationGenerations",
              record.ip_part("ip_generation__label", i),
              source=GENERATIONS_SOURCE)
        maybe(inst, "instantiationDuration",
              record.ip_part("ip_duration", i))
        maybe(inst, "instantiationColors", record.ip_part("ip_colors_tid", i))
        for lang in split_values(record.get("obj_language__value")):
            el(inst, "instantiationLanguage", lang, source=LANGUAGE_SOURCE)

        frame_rate = record.ip_part("ip_frame_rate_tid", i)
        aspect = record.ip_part("ip_aspect_ratio", i)
        if frame_rate or aspect:
            track = el(inst, "instantiationEssenceTrack")
            el(track, "essenceTrackType",
               "Image" if record.media_type.lower() == "moving image"
               else "Audio")
            if frame_rate:
                el(track, "essenceTrackFrameRate",
                   frame_rate.lower().replace("fps", "").strip(),
                   unitsOfMeasure="fps")
            maybe(track, "essenceTrackAspectRatio", aspect)

        annotations = (
            ("Part", str(i) if part_count > 1 else ""),
            ("Sides or Parts", record.ip_part("ip_sides_parts", i)),
            ("Silent or Sound", record.ip_part("ip_silent_sound_tid", i)),
            ("Running Speed", record.ip_part("ip_running_speed", i)),
            ("Track Standard", record.ip_part("ip_track_standard_tid", i)),
            ("Stock Manufacturer",
             record.ip_part("ip_stock_manufacturer", i)),
            ("Base Type", record.ip_part("ip_base_type", i)),
            ("Condition Notes", record.ip_part("ip_condition_notes", i)),
            ("Additional Technical Notes",
             record.ip_part("ip_additional_technical_notes", i)),
            ("Container and Item Annotations",
             record.ip_part("ip_container_item_annotations", i)),
        )
        for annotation, text in annotations:
            maybe(inst, "instantiationAnnotation", text, annotation=annotation)


class PackageFile:
    """One digital file inside an Object ID folder."""

    def __init__(self, path, object_id):
        self.path = path
        self.name = path.name
        self.stem = path.stem
        self.ext = path.suffix.lower().lstrip(".")
        m = PART_TOKEN_RE.search(self.name)
        self.part = int(m.group(1)) if m else None
        if self.ext == "vtt":
            self.file_class = "vtt"
        elif "_prsv" in self.stem:
            self.file_class = "prsv"
        elif "_mezz" in self.stem:
            self.file_class = "mezz"
        elif "_access" in self.stem:
            self.file_class = "access"
        else:
            self.file_class = None


def scan_package(folder, object_id):
    """Classify every candidate file in an object folder."""
    files, unclassified = [], []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.suffix.lower() in (".md5", ".csv"):
            continue
        if path.name.endswith(PBCORE_SUFFIX) or path.suffix.lower() == ".xml":
            continue
        pf = PackageFile(path, object_id)
        if pf.file_class:
            files.append(pf)
        else:
            unclassified.append(path.name)
    order = {"prsv": 0, "mezz": 1, "access": 2, "vtt": 3}
    files.sort(key=lambda f: (order[f.file_class], f.name))
    return files, unclassified


def add_file_instantiation(root, record, pf, files, probe_data):
    """One pbcoreInstantiation for a digital file, richest data available."""
    info = FILE_CLASSES[pf.file_class]
    root.append(ET.Comment(info["generation"]))
    inst = el(root, "pbcoreInstantiation")

    el(inst, "instantiationIdentifier", pf.name,
       source=ORG_SOURCE, annotation="File Name")
    md5 = read_md5_sidecar(pf.path)
    maybe(inst, "instantiationIdentifier", md5,
          source=ORG_SOURCE, version="MD5", annotation="Checksum")

    try:
        mtime = datetime.fromtimestamp(pf.path.stat().st_mtime, timezone.utc)
        el(inst, "instantiationDate",
           mtime.strftime("%Y-%m-%dT%H:%M:%SZ"), dateType="File Modification")
    except OSError as err:
        log.warning("Could not stat %s: %s", pf.name, err)

    general = (probe_data or {}).get("general", {})
    videos = (probe_data or {}).get("video", [])
    audios = (probe_data or {}).get("audio", [])
    v = videos[0] if videos else {}

    if v.get("width"):
        el(inst, "instantiationDimensions", v["width"],
           unitsOfMeasure="pixels", annotation="Width")
    if v.get("height"):
        el(inst, "instantiationDimensions", v["height"],
           unitsOfMeasure="pixels", annotation="Height")

    mime = general.get("mime") or MIME_FALLBACK.get(pf.ext, "")
    maybe(inst, "instantiationDigital", mime, source="IANA Media Types")
    el(inst, "instantiationStandard", pf.ext)
    el(inst, "instantiationLocation", info["location"])
    el(inst, "instantiationMediaType",
       "Text" if pf.file_class == "vtt" else record.media_type)
    el(inst, "instantiationGenerations", info["generation"])

    file_size = general.get("file_size")
    if not file_size:
        try:
            file_size = str(pf.path.stat().st_size)
        except OSError:
            file_size = ""
    maybe(inst, "instantiationFileSize", file_size, unitsOfMeasure="bytes")
    maybe(inst, "instantiationDuration",
          seconds_to_hms(general.get("duration_s")))
    maybe(inst, "instantiationDataRate", general.get("overall_bit_rate"),
          unitsOfMeasure="bits/second")
    if pf.file_class != "vtt":
        for lang in split_values(record.get("obj_language__value")):
            el(inst, "instantiationLanguage", lang, source=LANGUAGE_SOURCE)

    # captions available? note them on the AV copy they accompany
    if pf.file_class in ("access", "mezz"):
        vtts = [f for f in files if f.file_class == "vtt"
                and f.part == pf.part]
        if vtts:
            el(inst, "instantiationAlternativeModes",
               "Captions (WebVTT): " + "; ".join(f.name for f in vtts))

    for track in videos:
        t = el(inst, "instantiationEssenceTrack")
        el(t, "essenceTrackType", "Video")
        maybe(t, "essenceTrackEncoding", track.get("encoding"))
        maybe(t, "essenceTrackDataRate", track.get("bit_rate"),
              unitsOfMeasure="bits/second")
        maybe(t, "essenceTrackFrameRate", track.get("frame_rate"),
              unitsOfMeasure="fps")
        maybe(t, "essenceTrackBitDepth", track.get("bit_depth"))
        maybe(t, "essenceTrackAspectRatio", track.get("aspect_ratio"))
        maybe(t, "essenceTrackAnnotation", track.get("frame_count"),
              annotation="Frame Count")
        maybe(t, "essenceTrackAnnotation", track.get("scan_type"),
              annotation="Scan Type")
        maybe(t, "essenceTrackAnnotation", track.get("chroma"),
              annotation="Color Sampling")
    for track in audios:
        t = el(inst, "instantiationEssenceTrack")
        el(t, "essenceTrackType", "Audio")
        maybe(t, "essenceTrackEncoding", track.get("encoding"))
        maybe(t, "essenceTrackDataRate", track.get("bit_rate"),
              unitsOfMeasure="bits/second")
        maybe(t, "essenceTrackSamplingRate", track.get("sampling_rate"),
              unitsOfMeasure="Hz")
        maybe(t, "essenceTrackBitDepth", track.get("bit_depth"))
        maybe(t, "essenceTrackAnnotation", track.get("channels"),
              annotation="Channels")

    # relations: everything ultimately traces back to the physical/original
    relation_target, relation_annotation = "", ""
    if pf.file_class == "prsv":
        relation_target = record.object_id
        relation_annotation = "Object Identifier"
    else:
        prsvs = [f for f in files if f.file_class == "prsv"
                 and f.part == pf.part]
        if pf.file_class == "vtt":
            accesses = [f for f in files if f.file_class == "access"
                        and f.part == pf.part]
            if accesses:
                relation_target = accesses[0].name
                relation_annotation = "File Name"
        if not relation_target and prsvs:
            relation_target = prsvs[0].name
            relation_annotation = "File Name"
        if not relation_target:
            relation_target = record.object_id
            relation_annotation = "Object Identifier"
    if relation_target:
        rel = el(inst, "instantiationRelation")
        el(rel, "instantiationRelationType", "Derived from",
           source=RELATION_TYPE_SOURCE)
        el(rel, "instantiationRelationIdentifier", relation_target,
           annotation=relation_annotation)

    maybe(inst, "instantiationAnnotation", general.get("format"),
          annotation="Container Format")


def add_annotations_and_extensions(root, record):
    r = record.get
    extent = ""
    if record.part_count:
        parts = split_multipart(r("obj_av_item_parts__ip_sides_parts"))
        unit = parts[0] if len(set(parts)) == 1 and parts else ""
        extent = unit or (f"{record.part_count} part"
                          + ("s" if record.part_count != 1 else ""))
    for annotation, text in (
        ("Extent", extent),
        ("Significance", r("obj_significance")),
        ("Condition", r("obj_condition_list__value")),
        ("Condition Note", r("obj_condition_note")),
        ("Collection Guide Title", r("obj_collection_guide__title")),
        ("Collection Guide URL", r("obj_collection_guide__url")),
        ("Transcript", r("obj_transcript")),
        ("Transcript URL", r("obj_transcript_url")),
        ("Funder", r("obj_funder")),
        ("Grant Cycle", r("obj_grant_cycle")),
    ):
        maybe(root, "pbcoreAnnotation", text, annotation=annotation)

    for element, value, authority in (
        ("CountryOfCreation", r("obj_country_of_creation__value"),
         COUNTRY_AUTHORITY),
        ("Project Note", r("obj_project_note"), ORG_SOURCE),
    ):
        if clean(value):
            ext = el(root, "pbcoreExtension")
            wrap = el(ext, "extensionWrap")
            el(wrap, "extensionElement", element)
            el(wrap, "extensionValue", clean(value))
            el(wrap, "extensionAuthorityUsed", authority)


def build_pbcore(record, files, prober):
    ET.register_namespace("", PBCORE_NS)
    ET.register_namespace("xsi", XSI_NS)
    root = ET.Element(f"{{{PBCORE_NS}}}pbcoreDescriptionDocument")
    root.set(f"{{{XSI_NS}}}schemaLocation", SCHEMA_LOCATION)

    add_descriptive(root, record)
    add_physical_instantiations(root, record)
    for pf in files:
        probe_data = None
        if pf.file_class != "vtt" and prober.backend:
            probe_data = prober.probe(pf.path)
            if probe_data is None:
                log.warning("No technical metadata harvested for %s", pf.name)
        add_file_instantiation(root, record, pf, files, probe_data)
    add_annotations_and_extensions(root, record)

    ET.indent(root, space="   ")
    return ET.ElementTree(root)


# ---------------------------------------------------------------------------
# Validation (optional)
# ---------------------------------------------------------------------------


def load_validator(xsd_path):
    try:
        from lxml import etree
    except ImportError:
        log.warning("--validate requested but lxml is not installed "
                    "(pip install lxml); skipping validation.")
        return None
    if not xsd_path.is_file():
        log.warning("--validate requested but XSD not found at %s; "
                    "skipping validation.", xsd_path)
        return None
    schema = etree.XMLSchema(etree.parse(str(xsd_path)))

    def validate(path):
        doc = etree.parse(str(path))
        if schema.validate(doc):
            return True, ""
        return False, "; ".join(str(e) for e in schema.error_log)
    return validate


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------


def find_inventory(target_dir, override):
    if override:
        path = Path(override)
        return path if path.is_file() else None
    candidates = sorted(p for p in target_dir.glob("*.csv")
                        if not p.name.startswith("."))
    return candidates[0] if candidates else None


def setup_logging(target_dir, log_arg):
    log.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    log.addHandler(console)
    if log_arg is None:
        return None
    if log_arg == "":
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_path = target_dir / f"pbcorethat_{stamp}.log"
    else:
        log_path = Path(log_arg)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s: %(message)s"))
    log.addHandler(handler)
    return log_path


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="pbcorethat",
        description="Generate PBCore XML for packaged California Revealed "
                    "AV objects from an Archipelago baseline CSV export.")
    parser.add_argument("directory",
                        help="Folder containing the baseline CSV export and "
                             "the Object ID package folders")
    parser.add_argument("--csv", help="Path to a specific inventory CSV")
    parser.add_argument("--log", nargs="?", const="", default=None,
                        metavar="PATH",
                        help="Write a timestamped log (default: into the "
                             "target directory)")
    parser.add_argument("--validate", action="store_true",
                        help="Validate output against the PBCore 2.1 XSD "
                             "(requires lxml)")
    parser.add_argument("--xsd",
                        default=str(Path(__file__).resolve().parent
                                    / "pbcore-2_1.xsd"),
                        help="Path to pbcore-2_1.xsd for --validate")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be generated without writing")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    target_dir = Path(args.directory).expanduser()
    if not target_dir.is_dir():
        print(f"Error: {target_dir} is not a directory.", file=sys.stderr)
        return 1

    log_path = setup_logging(target_dir, args.log)

    inventory = find_inventory(target_dir, args.csv)
    if not inventory:
        log.error("No inventory CSV found in %s", target_dir)
        return 1
    log.info("pbcorethat v%s", __version__)
    log.info("Inventory: %s", inventory.name)

    prober = Prober()
    log.info("Technical metadata backend: %s", prober.describe())
    if not prober.backend:
        log.warning("Install MediaInfo (https://mediaarea.net) or FFmpeg's "
                    "ffprobe for full technical metadata harvesting.")

    validator = load_validator(Path(args.xsd)) if args.validate else None

    try:
        with open(inventory, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, csv.Error) as err:
        log.error("Could not read inventory %s: %s", inventory, err)
        return 1
    if not rows:
        log.error("Inventory %s contains no data rows.", inventory.name)
        return 1

    generated, skipped_no_id, skipped_media = [], [], []
    skipped_no_folder, unclassified_files, invalid = [], {}, []
    warnings_before = getattr(log, "_warning_count", 0)

    class WarningCounter(logging.Filter):
        count = 0

        def filter(self, record):
            if record.levelno >= logging.WARNING:
                WarningCounter.count += 1
            return True
    counter = WarningCounter()
    log.addFilter(counter)

    seen_ids = set()
    for row in rows:
        record = ObjectRecord(row)
        label = record.get("label") or "(no title)"
        nid = record.get("node_id_no_changes")
        if not record.object_id:
            skipped_no_id.append(f"node {nid or '?'} — {label}")
            continue
        seen_ids.add(record.object_id)
        if record.media_type.lower() not in MEDIA_TYPES_PROCESSED:
            skipped_media.append(
                f"{record.object_id} ({record.media_type or 'no media type'})")
            continue
        folder = target_dir / record.object_id
        if not folder.is_dir():
            skipped_no_folder.append(record.object_id)
            continue

        files, odd = scan_package(folder, record.object_id)
        if odd:
            unclassified_files[record.object_id] = odd
        if not files:
            log.warning("%s: folder contains no prsv/mezz/access/vtt files; "
                        "generating descriptive-only PBCore.",
                        record.object_id)

        out_path = folder / f"{record.object_id}{PBCORE_SUFFIX}"
        if args.dry_run:
            log.info("[dry run] would write %s (%d file instantiation(s))",
                     out_path.name, len(files))
            generated.append(record.object_id)
            continue

        try:
            tree = build_pbcore(record, files, prober)
            if out_path.exists():
                log.info("%s: replacing existing %s",
                         record.object_id, out_path.name)
            tree.write(out_path, encoding="UTF-8", xml_declaration=True)
            log.info("Wrote %s (%d file instantiation(s))",
                     out_path.name, len(files))
            generated.append(record.object_id)
        except OSError as err:
            log.error("Failed writing %s: %s", out_path, err)
            invalid.append(f"{record.object_id}: write failed — {err}")
            continue

        if validator:
            ok, detail = validator(out_path)
            if ok:
                log.info("%s validates against PBCore 2.1", out_path.name)
            else:
                invalid.append(f"{out_path.name}: {detail}")
                log.warning("%s failed XSD validation: %s",
                            out_path.name, detail)

    orphan_folders = sorted(
        p.name for p in target_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
        and p.name not in seen_ids)

    # ------------------------------------------------------------------ report
    line = "-" * 62
    log.info(line)
    log.info("RUN SUMMARY")
    log.info("  PBCore generated:                  %d", len(generated))
    log.info("  Skipped — no object identifier:    %d", len(skipped_no_id))
    for item in skipped_no_id:
        log.info("      %s", item)
    log.info("  Skipped — media type not Sound/Moving Image: %d",
             len(skipped_media))
    for item in skipped_media:
        log.info("      %s", item)
    log.info("  Skipped — no matching folder:      %d", len(skipped_no_folder))
    for item in skipped_no_folder:
        log.info("      %s", item)
    if orphan_folders:
        log.info("  Folders with no inventory row:     %d",
                 len(orphan_folders))
        for item in orphan_folders:
            log.info("      %s", item)
    if unclassified_files:
        log.info("  Unclassified files (not prsv/mezz/access/vtt; "
                 "not described):")
        for oid, names in unclassified_files.items():
            for name in names:
                log.info("      %s: %s", oid, name)
    if invalid:
        log.info("  Validation / write problems:       %d", len(invalid))
        for item in invalid:
            log.info("      %s", item)
    if log_path:
        log.info("Log written to %s", log_path)
    log.info(line)

    had_issues = any([skipped_no_id, skipped_media, skipped_no_folder,
                      orphan_folders, unclassified_files, invalid,
                      counter.count > warnings_before])
    return 2 if had_issues else 0


if __name__ == "__main__":
    sys.exit(main())
