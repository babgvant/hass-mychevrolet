"""Build and validate the standalone integration archive."""

import json
import re
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "mychevrolet"
REQUIRED = {
    "__init__.py",
    "brand/icon.png",
    "config_flow.py",
    "manifest.json",
    "onyonstar/api.py",
    "onyonstar/client.py",
    "onyonstar/auth/gm_auth.py",
    "services.yaml",
    "translations/en.json",
}


def package(version: str, output: Path) -> None:
    """Check release metadata and package the files installed by HACS."""
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    hacs = json.loads((ROOT / "hacs.json").read_text())
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", version):
        raise ValueError("Expected a stable semantic version")
    if manifest["version"] != version or manifest["domain"] != COMPONENT.name:
        raise ValueError(
            "Release version or integration domain does not match the manifest"
        )
    if hacs["name"] != manifest["name"]:
        raise ValueError("HACS and manifest names differ")
    files = {
        path.relative_to(COMPONENT).as_posix(): path
        for path in COMPONENT.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix in {".py", ".json", ".yaml", ".png", ".txt"}
        and not path.name.startswith(("gm_tokens", "microsoft_tokens"))
    }
    if missing := REQUIRED - files.keys():
        raise ValueError(f"Required integration files missing: {sorted(missing)}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            archive.write(path, f"custom_components/mychevrolet/{name}")


if __name__ == "__main__":
    package(sys.argv[1], ROOT / "dist" / "mychevrolet.zip")
