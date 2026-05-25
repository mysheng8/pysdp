"""setup.py — post-install hook to download SDPCLI binary.

All package metadata is in pyproject.toml. This file only adds the
post-install hook that downloads SDPCLI from GitHub Releases.
"""
from setuptools import setup
from setuptools.command.install import install
from setuptools.command.develop import develop


class _PostInstallMixin:
    def _post_install(self):
        try:
            from scripts.fetch_sdpcli import main
            main()
        except Exception as e:
            print(f"[pysdp] SDPCLI auto-download skipped: {e}")
            print("[pysdp] Run 'pysdp-fetch' manually to download SDPCLI binary.")


class PostInstall(_PostInstallMixin, install):
    def run(self):
        install.run(self)
        self._post_install()


class PostDevelop(_PostInstallMixin, develop):
    def run(self):
        develop.run(self)
        self._post_install()


setup(
    cmdclass={
        "install": PostInstall,
        "develop": PostDevelop,
    },
)
