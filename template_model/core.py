from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .analyzer import analyze_presentation
from .models import PresentationManifest


class TemplateModel:
    def __init__(
        self,
        *,
        use_vlm: bool = False,
        vlm_client: Any | None = None,
        libreoffice_path: str = "libreoffice",
        pdftoppm_path: str = "pdftoppm",
    ) -> None:
        self.use_vlm = use_vlm
        self.vlm_client = vlm_client
        self.libreoffice_path = libreoffice_path
        self.pdftoppm_path = pdftoppm_path

    def analyze(self, template_path: str | Path, output_dir: str | Path) -> PresentationManifest:
        output = Path(output_dir).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise FileExistsError(f"Output directory must not exist or must be empty: {output}")

        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
        try:
            manifest = analyze_presentation(
                template_path,
                staging,
                use_vlm=self.use_vlm,
                vlm_client=self.vlm_client,
                libreoffice=self.libreoffice_path,
                pdftoppm=self.pdftoppm_path,
            )
            manifest.save(staging)
            if output.exists():
                output.rmdir()
            os.replace(staging, output)
            manifest.output_dir = str(output)
            return manifest
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
