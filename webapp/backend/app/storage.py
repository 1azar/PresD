import hashlib
import zipfile
from pathlib import Path

from fastapi import UploadFile

from .config import settings
from .errors import APIError


def user_root(user_id: str) -> Path:
    root = (settings.data_root / "users" / user_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def relative_to_data(path: Path) -> str:
    return str(path.resolve().relative_to(settings.data_root.resolve()))


def from_data(relative: str) -> Path:
    root = settings.data_root.resolve()
    result = (root / relative).resolve()
    try:
        result.relative_to(root)
    except ValueError as exc:
        raise APIError(404, "not_found", "Файл не найден") from exc
    return result


async def read_limited(upload: UploadFile, limit: int) -> bytes:
    data = bytearray()
    while chunk := await upload.read(min(1024 * 1024, limit + 1 - len(data))):
        data.extend(chunk)
        if len(data) > limit:
            raise APIError(413, "file_too_large", f"Файл превышает лимит {limit // 1024 // 1024} МБ")
    return bytes(data)


def validate_pptx(data: bytes) -> str:
    if len(data) > settings.max_template_bytes:
        raise APIError(413, "template_too_large", "Шаблон превышает 100 МБ")
    digest = hashlib.sha256(data).hexdigest()
    try:
        from io import BytesIO
        with zipfile.ZipFile(BytesIO(data)) as archive:
            if "ppt/presentation.xml" not in archive.namelist():
                raise APIError(422, "invalid_pptx", "В архиве нет ppt/presentation.xml")
            bad = archive.testzip()
            if bad:
                raise APIError(422, "invalid_pptx", "PPTX повреждён")
    except zipfile.BadZipFile as exc:
        raise APIError(422, "invalid_pptx", "Файл не является корректным PPTX") from exc
    return digest
