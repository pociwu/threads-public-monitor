from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_login_image_includes_x11_window_manager_and_probe() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "fluxbox" in dockerfile
    assert "x11-utils" in dockerfile


def test_login_entrypoint_waits_for_x_and_forces_vnc_repaint() -> None:
    entrypoint = (ROOT / "scripts/browser-login-entrypoint.sh").read_text(encoding="utf-8")
    assert "xdpyinfo -display :99" in entrypoint
    assert "fluxbox" in entrypoint
    assert "-noxdamage" in entrypoint
    assert "--ozone-platform=x11" in entrypoint


def test_login_script_removes_stale_chromium_profile_locks_after_worker_stops() -> None:
    script = (ROOT / "scripts/login.sh").read_text(encoding="utf-8")

    worker_stop = script.index("docker compose stop worker")
    lock_cleanup = script.index("browser-profile/SingletonLock")
    browser_start = script.index("docker compose --profile login up -d browser-login")

    assert worker_stop < lock_cleanup < browser_start
    assert "browser-profile/SingletonCookie" in script
    assert "browser-profile/SingletonSocket" in script
