"""Check the archive used for manual installation and release staging."""

from zipfile import ZipFile

from scripts.package_release import package


def test_release_archive(tmp_path):
    archive = tmp_path / "mychevrolet.zip"
    package("0.2.0", archive)
    root = "custom_components/mychevrolet/"
    with ZipFile(archive) as release:
        assert root + "onyonstar/auth/gm_auth.py" in release.namelist()
        assert root + "onyonstar/LICENSE-OnStarJS.txt" in release.namelist()
        assert root + "brand/icon.png" in release.namelist()
        assert not any(
            "microsoft_tokens" in name or "__pycache__" in name
            for name in release.namelist()
        )
