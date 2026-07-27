import hashlib
import os
from pathlib import Path
from uuid import UUID

from django.conf import settings


MIME_SUFFIXES = {
    "application/json": ".json",
    "text/markdown": ".md",
    "text/plain": ".txt",
}


class ArtifactStorageError(ValueError):
    pass


def artifact_root() -> Path:
    root = Path(settings.DASHBOARD_PLATFORM_ARTIFACT_ROOT).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def write_artifact(series_id: UUID, version: int, content: str, mime_type: str) -> tuple[Path, str, int]:
    encoded = content.encode("utf-8")
    if len(encoded) > settings.DASHBOARD_PLATFORM_MAX_ARTIFACT_BYTES:
        raise ArtifactStorageError(
            f"아티팩트는 {settings.DASHBOARD_PLATFORM_MAX_ARTIFACT_BYTES:,}바이트 이하여야 합니다."
        )
    root = artifact_root()
    directory = root / str(series_id)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    suffix = MIME_SUFFIXES.get(mime_type, ".bin")
    path = directory / f"{version}{suffix}"
    try:
        with path.open("xb") as output:
            output.write(encoded)
        os.chmod(path, 0o600)
    except FileExistsError as exc:
        raise ArtifactStorageError("같은 아티팩트 리비전 파일이 이미 존재합니다.") from exc
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path, hashlib.sha256(encoded).hexdigest(), len(encoded)


def read_artifact(path_value: str, expected_sha256: str) -> str:
    root = artifact_root()
    path = Path(path_value).expanduser().resolve()
    if path == root or root not in path.parents:
        raise ArtifactStorageError("허용된 아티팩트 저장소 밖의 파일입니다.")
    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise ArtifactStorageError("아티팩트 파일을 읽을 수 없습니다.") from exc
    if not hashlib.sha256(encoded).hexdigest() == expected_sha256:
        raise ArtifactStorageError("아티팩트 해시가 저장된 값과 일치하지 않습니다.")
    try:
        return encoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactStorageError("텍스트 아티팩트만 API로 읽을 수 있습니다.") from exc
