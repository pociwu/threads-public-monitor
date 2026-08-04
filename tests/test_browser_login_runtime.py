from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_login_image_uses_native_vnc_x_server_and_window_manager() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "tigervnc-standalone-server" in dockerfile
    assert "fluxbox" in dockerfile
    assert "x11-utils" in dockerfile
    assert "x11vnc" not in dockerfile


def test_login_entrypoint_waits_for_native_vnc_x_server() -> None:
    entrypoint = (ROOT / "scripts/browser-login-entrypoint.sh").read_text(encoding="utf-8")
    assert "Xtigervnc :99" in entrypoint
    assert "-rfbport 5900" in entrypoint
    assert "-SecurityTypes None" in entrypoint
    assert "xdpyinfo -display :99" in entrypoint
    assert "fluxbox" in entrypoint
    assert "x11vnc" not in entrypoint
    assert "--ozone-platform=x11" in entrypoint


def test_login_script_removes_stale_chromium_profile_locks_after_worker_stops() -> None:
    script = (ROOT / "scripts/login.sh").read_text(encoding="utf-8")

    worker_stop = script.index("docker compose stop worker")
    first_lock_cleanup = script.index("\ncleanup_profile_locks\n", worker_stop)
    browser_start = script.index("docker compose --profile login up -d browser-login")
    browser_stop = script.index("docker compose --profile login stop browser-login")
    second_lock_cleanup = script.index("\ncleanup_profile_locks\n", browser_stop)
    worker_start = script.index("docker compose start worker")

    assert worker_stop < first_lock_cleanup < browser_start
    assert browser_stop < second_lock_cleanup < worker_start
    assert script.count("cleanup_profile_locks") == 3
    assert "browser-profile/SingletonCookie" in script
    assert "browser-profile/SingletonSocket" in script


def test_worker_entrypoint_removes_only_stale_chromium_profile_locks_before_start() -> None:
    entrypoint = (ROOT / "scripts/worker-entrypoint.sh").read_text(encoding="utf-8")

    lock_cleanup = entrypoint.index("rm -f --")
    worker_start = entrypoint.index("exec python -m app.worker")

    assert lock_cleanup < worker_start
    assert "/browser-profile/SingletonLock" in entrypoint
    assert "/browser-profile/SingletonCookie" in entrypoint
    assert "/browser-profile/SingletonSocket" in entrypoint
    assert "Singleton*" not in entrypoint


def test_compose_starts_worker_through_lock_cleanup_entrypoint() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert 'command: ["/app/scripts/worker-entrypoint.sh"]' in compose
