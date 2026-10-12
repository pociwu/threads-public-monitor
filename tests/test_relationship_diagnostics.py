import json

from app.config import Settings
from app.services.collector import ThreadsCollector


def test_no_progress_does_not_overwrite_failure_evidence(tmp_path):
    class Page:
        def evaluate(self, script):
            return {"dialogLinks": []}

        def screenshot(self, **kwargs):
            pass

    collector = ThreadsCollector(Settings(media_root=tmp_path / "media"))
    collector._save_relationship_diagnostic(
        Page(), "example", "followers", stage="member_rows_timeout"
    )
    collector._save_relationship_diagnostic(Page(), "example", "followers")
    failure = tmp_path / "debug/relationship-example-followers-failure-latest.json"
    progress = tmp_path / "debug/relationship-example-followers-latest.json"
    assert json.loads(failure.read_text(encoding="utf-8"))["failureStage"] == "member_rows_timeout"
    assert json.loads(progress.read_text(encoding="utf-8"))["failureStage"] == "scan_no_progress"
