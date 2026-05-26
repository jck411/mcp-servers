import re
import shlex
import subprocess
from pathlib import Path


def test_promote_archive_works_with_nounset(tmp_path: Path):
    script = Path("deploy/backup.sh").read_text()
    match = re.search(r"\npromote_archive\(\) \{\n.*?\n\}", script, re.S)
    assert match

    archive = tmp_path / "knowledge.test.tar.gz"
    archive.write_bytes(b"backup")
    Path(f"{archive}.sha256").write_text("sum\n")
    Path(f"{archive}.list").write_text("manifest.json\n")
    (tmp_path / "weekly").mkdir()

    harness = "\n".join([
        "set -euo pipefail",
        f"BACKUP_ROOT={shlex.quote(str(tmp_path))}",
        "log() { :; }",
        match.group(0).strip(),
        f"promote_archive {shlex.quote(str(archive))} weekly",
        f"test -s {shlex.quote(str(tmp_path / 'weekly' / archive.name))}",
    ])
    subprocess.run(["bash", "-c", harness], check=True)


def test_backup_runs_repo_owned_maintenance_module():
    script = Path("deploy/backup.sh").read_text()

    assert "-m servers.knowledge.maintenance" in script
    assert "deploy/maintain.py" not in script
    assert "deploy-maintain" not in script
