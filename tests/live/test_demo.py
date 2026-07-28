"""Contract tests for the localhost-only checkpoint demo page."""

from __future__ import annotations

from http.client import HTTPConnection
import socket

from hccast_wired.live.demo import DEMO_HTML, DemoPageServer


def test_demo_page_is_self_contained_and_contains_visible_motion_instruments() -> None:
    text = DEMO_HTML.decode("utf-8")

    assert 'id="clock"' in text
    assert 'id="counter"' in text
    assert 'id="sweep"' in text
    assert 'id="contrast"' in text
    assert "requestAnimationFrame" in text
    assert "https://www.youtube.com/results?search_query=Big+Buck+Bunny" in text
    assert "<script src=" not in text
    assert "<link rel=" not in text


def test_server_serves_only_the_demo_and_releases_the_port() -> None:
    server = DemoPageServer(port=0).start()
    host, port = server.address
    assert host == "127.0.0.1"
    connection = HTTPConnection(host, port, timeout=1)
    connection.request("GET", "/")
    response = connection.getresponse()
    assert response.status == 200
    assert response.read() == DEMO_HTML
    connection.close()

    server.close()
    server.close()

    with socket.socket() as probe:
        probe.settimeout(0.2)
        assert probe.connect_ex((host, port)) != 0


def test_running_server_health_is_clear_and_stopped_server_is_reported() -> None:
    server = DemoPageServer(port=0).start()

    assert server.poll_failure() is None
    server.close()
    assert server.poll_failure() == "demo-server-exited"


def test_close_before_start_releases_the_bound_socket() -> None:
    server = DemoPageServer(port=0)
    host, port = server.address

    server.close()

    with socket.socket() as probe:
        probe.settimeout(0.2)
        assert probe.connect_ex((host, port)) != 0


def test_server_rejects_unknown_paths() -> None:
    with DemoPageServer(port=0) as server:
        host, port = server.address
        connection = HTTPConnection(host, port, timeout=1)
        connection.request("GET", "/favicon.ico")
        assert connection.getresponse().status == 404
        connection.close()
