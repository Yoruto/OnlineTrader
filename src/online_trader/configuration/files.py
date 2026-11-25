"""Explicit local parameter files; imports never retain account or notification data."""

from pathlib import Path
import os
import tempfile

from online_trader.configuration.dsv import DsvProfile
from online_trader.errors import TraderError


MAX_PARAMETER_FILE_BYTES = 1_048_576


def load_profile(path, *, dsv=False):
    """Read only the requested file, with a bound before and after the read."""
    try:
        source = Path(path)
        if source.stat().st_size > MAX_PARAMETER_FILE_BYTES:
            raise TraderError("E_CONFIG_FILE", "Parameter file exceeds the supported size")
        with source.open("rb") as stream:
            raw = stream.read(MAX_PARAMETER_FILE_BYTES + 1)
        if len(raw) > MAX_PARAMETER_FILE_BYTES:
            raise TraderError("E_CONFIG_FILE", "Parameter file exceeds the supported size")
    except OSError as error:
        raise TraderError("E_CONFIG_FILE", "Cannot read the selected parameter file") from error
    return DsvProfile.from_dsv_json(raw) if dsv else DsvProfile.from_json(raw)


def export_profile(profile, path):
    """Publish a complete safe business JSON as a new file; never replace a file."""
    if not isinstance(profile, DsvProfile):
        raise TraderError("E_CONFIG_FILE", "Expected a validated parameter profile")
    return publish_new_bytes(profile.to_json().encode("utf-8"), path)


def publish_new_bytes(raw, path):
    """Publish complete local configuration bytes exclusively on the same volume."""
    target = Path(path)
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".online-trader-parameters-", suffix=".tmp",
                                         dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # The temporary file is on the same volume. Linking is atomic and fails if
        # the requested destination already exists, including concurrent exports.
        os.link(temporary, target)
        return target
    except FileExistsError as error:
        raise TraderError("E_DUPLICATE_CONFLICT", "Export destination already exists") from error
    except OSError as error:
        raise TraderError("E_CONFIG_FILE", "Cannot export the parameter file") from error
    finally:
        if temporary is not None:
            try:temporary.unlink()
            except FileNotFoundError:pass


def _atomic_replace_bytes(raw, path, *, prefix, error_code, error_message):
    target = Path(path)
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", prefix=prefix, suffix=".tmp",
                                         dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
        return target
    except OSError as error:
        raise TraderError(error_code, error_message) from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def publish_replace_bytes(raw, path):
    """Atomically replace one validated local configuration file.

    Runtime defaults and user overrides are configuration, not trading facts.
    The temporary file is flushed on the target volume before replacement so a
    crash leaves either the old or the complete new document.
    """
    return _atomic_replace_bytes(
        raw,
        path,
        prefix=".online-trader-config-",
        error_code="E_CONFIG_FILE",
        error_message="Cannot replace the local configuration file",
    )


def publish_runtime_file_bytes(
    raw,
    path,
    *,
    error_code="E_RUNTIME_STATUS_WRITE",
    error_message="Cannot replace the runtime auxiliary file",
):
    """Atomically replace a non-authoritative runtime status or diagnostic file."""
    return _atomic_replace_bytes(
        raw,
        path,
        prefix=".online-trader-runtime-",
        error_code=error_code,
        error_message=error_message,
    )
