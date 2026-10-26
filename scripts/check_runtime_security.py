"""Check a built image under the supported container security profile.

Run through the image's Python, with read-only root, writable /tmp, UID/GID
1000, all capabilities dropped and no-new-privileges. No service secrets needed.
"""

import importlib
import importlib.util
import json
import os
import shutil
import ssl
import stat
import tempfile
from pathlib import Path


def main() -> None:
    assert os.getuid() == 1000 and os.getgid() == 1000, "Expected UID/GID 1000"
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    assert status["NoNewPrivs"].strip() == "1", "Privilege escalation must be disabled"
    for capability in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        assert int(status[capability], 16) == 0, capability
    assert os.statvfs("/").f_flag & os.ST_RDONLY, "Root filesystem must be read-only"
    assert importlib.util.find_spec("pip") is None, "pip must not be installed"

    tools = ("mount", "umount", "nsenter", "infocmp", "systemd-homed", "getfacl", "setfacl")
    for tool in tools:
        assert shutil.which(tool) is None, f"Unexpected runtime tool: {tool}"
    for directory in ("/usr/lib/systemd", "/lib/systemd"):
        assert not (Path(directory) / "systemd-homed").exists()
    archive_tar = [p for p in Path("/usr").rglob("Tar.pm") if p.parent.name == "Archive"]
    assert not archive_tar, "Unexpected Perl Archive::Tar module"
    assert not any(
        line.strip() and not line.lstrip().startswith("#")
        for line in Path("/etc/fstab").read_text().splitlines()
    ), "Runtime must not contain fstab mount authorizations"

    setid_files = []
    for directory, _, files in os.walk("/usr", followlinks=False):
        for name in files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode) and mode & (stat.S_ISUID | stat.S_ISGID):
                setid_files.append(str(path))
    assert not setid_files, f"Unexpected setuid/setgid files: {setid_files}"

    for module in ("mm_bot_manager.__main__", "psycopg", "PIL.Image", "sqlite3", "lzma"):
        importlib.import_module(module)
    assert ssl.create_default_context().cert_store_stats()["x509_ca"] > 0
    maps = Path("/proc/self/maps").read_text()
    assert "libacl.so" not in maps and "libmount.so" not in maps
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        path = Path(directory) / "write-check"
        path.write_text("ok")
        assert path.read_text() == "ok"

    print(
        json.dumps(
            {
                "uid": os.getuid(),
                "gid": os.getgid(),
                "no_new_privileges": True,
                "capabilities": 0,
                "read_only_root": True,
                "setid_files": setid_files,
                "absent_tools": tools,
                "archive_tar_present": False,
                "app_imports_tls_ca_and_tmp": "ok",
                "libacl_libmount_loaded_on_app_import": False,
            }
        )
    )


if __name__ == "__main__":
    main()
