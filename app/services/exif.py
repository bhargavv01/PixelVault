"""EXIF metadata extraction utility.

Extracts camera metadata (DateTimeOriginal, camera model, GPS coordinates)
from image files using Pillow.  Only reads the file header — never decodes
full pixel data — so it is safe to call on the 4 GB RAM target server.

This module is a pure utility: no side effects, no state, no disk writes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from PIL import Image
from PIL.ExifTags import GPS as GPS_TAGS, Base as ExifBase

logger = logging.getLogger("pixel_vault.services.exif")

# EXIF tag IDs we care about
_TAG_DATETIME_ORIGINAL = ExifBase.DateTimeOriginal       # 36867
_TAG_CAMERA_MODEL = ExifBase.Model                       # 272
_TAG_GPS_INFO = ExifBase.GPSInfo                         # 34853

# GPS sub-tag IDs (inside the GPSInfo IFD)
_GPS_LATITUDE_REF = GPS_TAGS.GPSLatitudeRef              # 1
_GPS_LATITUDE = GPS_TAGS.GPSLatitude                     # 2
_GPS_LONGITUDE_REF = GPS_TAGS.GPSLongitudeRef            # 3
_GPS_LONGITUDE = GPS_TAGS.GPSLongitude                   # 4


def _dms_to_decimal(dms: tuple, ref: str) -> float:
    """Convert EXIF GPS degrees-minutes-seconds to decimal degrees.

    Args:
        dms: Tuple of (degrees, minutes, seconds) — each may be a float
             or an ``IFDRational``.
        ref: Reference hemisphere character: 'N', 'S', 'E', or 'W'.

    Returns:
        Signed decimal degrees (negative for S/W).
    """
    degrees = float(dms[0])
    minutes = float(dms[1])
    seconds = float(dms[2])
    decimal = degrees + minutes / 60.0 + seconds / 3600.0

    if ref in ("S", "W"):
        decimal = -decimal

    return round(decimal, 6)


def _parse_gps(exif_data: Image.Exif) -> tuple[float, float] | None:
    """Extract GPS coordinates from EXIF data.

    Returns:
        ``(latitude, longitude)`` as signed decimal degrees, or ``None``
        if the required GPS tags are missing or malformed.
    """
    gps_ifd = exif_data.get_ifd(_TAG_GPS_INFO)
    if not gps_ifd:
        return None

    try:
        lat_dms = gps_ifd[_GPS_LATITUDE]
        lat_ref = gps_ifd[_GPS_LATITUDE_REF]
        lon_dms = gps_ifd[_GPS_LONGITUDE]
        lon_ref = gps_ifd[_GPS_LONGITUDE_REF]
    except KeyError:
        logger.debug("GPS IFD present but missing required sub-tags.")
        return None

    try:
        lat = _dms_to_decimal(lat_dms, lat_ref)
        lon = _dms_to_decimal(lon_dms, lon_ref)
    except (TypeError, ValueError, IndexError, ZeroDivisionError) as exc:
        logger.warning("Failed to parse GPS coordinates: %s", exc)
        return None

    return (lat, lon)


def _parse_datetime(raw: str) -> datetime | None:
    """Parse EXIF DateTimeOriginal string to a timezone-aware datetime.

    EXIF format is ``"YYYY:MM:DD HH:MM:SS"``.  We assume UTC when no
    timezone offset is embedded (common for most cameras).

    Returns:
        A UTC-aware ``datetime``, or ``None`` if parsing fails.
    """
    # Common EXIF datetime format
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    logger.debug("Unparseable EXIF datetime: %r", raw)
    return None


def extract_exif(blob_path: str) -> dict[str, Any]:
    """Extract EXIF metadata from an image file.

    Only reads the file header via ``PIL.Image.open()`` — full pixel
    data is never decoded.  Safe for large files on low-RAM servers.

    Args:
        blob_path: Absolute path to the image blob on disk.

    Returns:
        A dict with keys ``taken_at``, ``camera``, and ``gps``.
        Any field may be ``None`` if the corresponding EXIF tag is
        absent or unparseable.
    """
    result: dict[str, Any] = {
        "taken_at": None,
        "camera": None,
        "gps": None,
    }

    try:
        with Image.open(blob_path) as img:
            exif = img.getexif()
            if not exif:
                logger.debug("No EXIF data in %s", blob_path)
                return result

            # DateTimeOriginal
            raw_dt = exif.get(_TAG_DATETIME_ORIGINAL)
            if raw_dt and isinstance(raw_dt, str):
                result["taken_at"] = _parse_datetime(raw_dt)

            # Camera model
            raw_model = exif.get(_TAG_CAMERA_MODEL)
            if raw_model and isinstance(raw_model, str):
                result["camera"] = raw_model.strip()

            # GPS coordinates
            result["gps"] = _parse_gps(exif)

    except Exception as exc:
        # Never crash the upload pipeline for EXIF failures.
        # Return whatever we managed to extract (possibly all None).
        logger.warning("EXIF extraction failed for %s: %s", blob_path, exc)

    return result
