from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_update_rollback_restores_database_inside_container() -> None:
    script = (ROOT / "update.sh").read_text(encoding="utf-8")

    assert 'cp "$backup_path" data/threads-monitor.db' not in script
    assert (
        'docker compose run --rm web sqlite3 /data/threads-monitor.db '
        '".restore \'/backups/threads-monitor-${timestamp}.db\'"'
    ) in script
    assert "trap - ERR" in script
