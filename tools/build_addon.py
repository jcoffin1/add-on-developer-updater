"""Build and audit the Add-on Developer Updater package."""

from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / "globalPlugins" / "addonDeveloperUpdater"
sys.path.insert(0, str(MODULES))

import engine  # noqa: E402
import publisher  # noqa: E402


def build(expected_version: str = "") -> Path:
    manifest = ROOT / "manifest.ini"
    metadata = engine.values(manifest)
    version = metadata.get("version", "")
    if expected_version and version != expected_version:
        raise RuntimeError(f"Manifest version {version} does not match requested version {expected_version}")
    errors = engine.validate(manifest)
    if errors:
        raise RuntimeError("Manifest validation failed: " + "; ".join(errors))

    output = publisher.build_package(
        {"name": metadata["name"], "version": version, "manifest": str(manifest)},
        ROOT / "outputs",
    )
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise RuntimeError("Package contains duplicate archive paths")
        generated = [name for name in names if "__pycache__" in Path(name).parts or Path(name).suffix.casefold() in {".pyc", ".pyo"}]
        if generated:
            raise RuntimeError("Package contains generated Python files: " + ", ".join(generated[:5]))
        packaged_manifest = archive.read("manifest.ini").decode("utf-8-sig")
        packaged_values = {
            key.strip().casefold(): value.strip().strip('"\'')
            for key, value in (
                line.split("=", 1)
                for line in packaged_manifest.splitlines()
                if "=" in line and not line.lstrip().startswith(("#", ";"))
            )
        }
        if packaged_values.get("version") != version:
            raise RuntimeError("Packaged manifest version does not match the source manifest")
        if "globalPlugins/addonDeveloperUpdater/_vendor/PyYAML-LICENSE.txt" not in names:
            raise RuntimeError("Package is missing the bundled PyYAML license")

    digest = hashlib.sha256(output.read_bytes()).hexdigest().upper()
    print(f"Built {output}")
    print(f"SHA256 {digest}")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="", help="Require this exact manifest version")
    arguments = parser.parse_args()
    build(arguments.version)
