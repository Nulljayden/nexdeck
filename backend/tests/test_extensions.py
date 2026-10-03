"""Extensions: integrations written as YAML files and imported at runtime."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.adapters import all_adapters, get_adapter
from app.adapters.base import AdapterError, Context
from app.services import extensions

from .conftest import CSRF, setup_admin

EXAMPLES = Path(__file__).resolve().parents[2] / "extensions"
BASE = "http://svc.example.com"

SIMPLE = """
id: simple
name: Simple
fields:
  - { name: api_key, label: API key, type: password, required: true }
auth:
  headers: { X-Api-Key: "{{ api_key }}" }
test:
  path: /api/version
  success: "Simple {{ version }} answers."
widgets:
  - id: load
    name: Load
    renderer: gauge
    path: /api/status
    value: data.load
    unit: "%"
    warn_above: 70
    rows:
      - { label: Host, value: "{{ data.host | upper }}" }
  - id: things
    name: Things
    renderer: list
    path: /api/things
    params: { limit: 5 }
    items: things
    title: "{{ name }}"
    subtitle: "{{ size | bytes }}"
    value: size
    status: "{{ state | map(up=ok, down=bad) }}"
    link: "{{ connection.url }}/thing/{{ id }}"
    actions:
      - { id: refresh, label: Refresh, method: POST, path: /api/refresh, body: { key: "{{ api_key }}" }, message: "Queued {{ queued }}." }
"""


@pytest.fixture
def ctx() -> Context:
    return Context(httpx.AsyncClient(), integration_id=1, widget_id=1, cache={})


@pytest.fixture
def loaded(data_dir: Path) -> extensions.ExtensionAdapter:
    adapter = extensions.install(SIMPLE)
    yield adapter
    extensions.LOADED.clear()
    extensions.BROKEN.clear()


def test_an_extension_joins_the_catalogue(loaded: extensions.ExtensionAdapter) -> None:
    assert get_adapter("ext-simple") is loaded
    assert loaded in all_adapters()
    names = [field.name for field in loaded.fields]
    assert names == ["url", "api_key", "insecure"]
    assert loaded.fields[1].secret, "a password field is always stored encrypted"
    assert loaded.to_dict()["widgets"][0]["kind"] == "ext-simple.load"


@respx.mock
async def test_test_fetch_and_action_ask_the_service(loaded: extensions.ExtensionAdapter, ctx: Context) -> None:
    config = {"url": BASE, "api_key": "k1"}
    version = respx.get(f"{BASE}/api/version").mock(return_value=httpx.Response(200, json={"version": "2.1"}))
    assert await loaded.test(config, ctx) == "Simple 2.1 answers."
    assert version.calls.last.request.headers["X-Api-Key"] == "k1"

    respx.get(f"{BASE}/api/status").mock(return_value=httpx.Response(200, json={"data": {"load": 81.5, "host": "nas"}}))
    gauge = await loaded.fetch("load", config, {}, ctx)
    assert gauge.primary == {"label": "", "value": 81.5, "unit": "%"}
    assert gauge.status == "warn" and gauge.metrics == {"value": 81.5}
    assert gauge.meta["gauge"]["share"] == 81.5
    assert gauge.secondary == [{"label": "Host", "value": "NAS", "unit": ""}]

    things = respx.get(f"{BASE}/api/things").mock(return_value=httpx.Response(200, json={"things": [
        {"id": 7, "name": "One", "size": 2048, "state": "up"},
        {"id": 8, "name": "Two", "state": "down"},
    ]}))
    data = await loaded.fetch("things", config, {}, ctx)
    assert dict(things.calls.last.request.url.params) == {"limit": "5"}
    assert data.items[0] == {"title": "One", "subtitle": "2.0 KB", "value": 2048, "status": "ok", "url": f"{BASE}/thing/7"}
    assert data.items[1]["status"] == "bad" and data.items[1]["subtitle"] == ""
    assert [action.id for action in data.actions] == ["refresh"]

    refresh = respx.post(f"{BASE}/api/refresh").mock(return_value=httpx.Response(200, json={"queued": 3}))
    assert await loaded.action("things", "refresh", {}, config, {}, ctx) == "Queued 3."
    assert refresh.calls.last.request.content == b'{"key":"k1"}'


@respx.mock
async def test_a_missing_value_says_where(loaded: extensions.ExtensionAdapter, ctx: Context) -> None:
    respx.get(f"{BASE}/api/status").mock(return_value=httpx.Response(200, json={"other": 1}))
    with pytest.raises(AdapterError, match="no field named 'data'"):
        await loaded.fetch("load", {"url": BASE, "api_key": "k"}, {}, ctx)


async def test_a_path_never_leaves_the_connection(data_dir: Path, ctx: Context) -> None:
    adapter = extensions.ExtensionAdapter(extensions.parse(
        "id: leak\nname: Leak\nwidgets:\n  - { id: v, name: V, renderer: value, path: '{{ where }}', value: x }\n"))
    with pytest.raises(AdapterError, match="whole address"):
        await adapter.fetch("v", {"url": BASE, "where": "https://elsewhere.example.com/"}, {}, ctx)


@pytest.mark.parametrize(("text", "reason"), [
    ("id: x\nname: X\nwidgets: []", "id"),
    ("id: fine\nname: X\nwidgets: []", "widgets"),
    ("id: fine\nname: X\nwidgets:\n  - { id: v, name: V, renderer: value }", "needs value"),
    ("id: fine\nname: X\nwidgets:\n  - { id: v, name: V, renderer: value, value: x, pararms: {} }", "pararms"),
    ("id: fine\nname: X\nfields: [{ name: url, label: U }]\nwidgets:\n  - { id: v, name: V, renderer: value, value: x }", "added to every"),
    ("- just a list", "does not describe"),
    ("id: [unclosed", "not valid YAML"),
])
def test_a_broken_file_is_refused_with_a_reason(text: str, reason: str) -> None:
    with pytest.raises(extensions.ExtensionError, match=reason):
        extensions.parse(text)


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.yaml")), ids=lambda path: path.name)
def test_every_example_reads_and_draws_its_demo(path: Path) -> None:
    adapter = extensions.ExtensionAdapter(extensions.parse(path.read_text(encoding="utf-8")))
    for widget in adapter.widgets:
        data = adapter.demo(widget.kind, {}, 1)
        assert data.primary or data.items, f"{path.name}: {widget.kind} draws nothing in demo mode"
        assert not data.error


def test_the_examples_compute_what_they_promise() -> None:
    robot = extensions.ExtensionAdapter(extensions.parse((EXAMPLES / "uptimerobot.yaml").read_text()))
    stats = robot.demo("summary", {}, 1)
    assert [(row["label"], row["value"]) for row in [stats.primary, *stats.secondary]] == [
        ("Up", 3), ("Down", 1), ("Paused", 1), ("Monitors", 5)]
    monitors = robot.demo("monitors", {}, 1)
    assert [(item["value"], item["status"]) for item in monitors.items] == [("up", "ok"), ("up", "ok"), ("down", "bad")]

    panel = extensions.ExtensionAdapter(extensions.parse((EXAMPLES / "pterodactyl.yaml").read_text()))
    server = panel.demo("server", {}, 1)
    assert [row["value"] for row in [server.primary, *server.secondary]] == ["running", 23.4, "2.0 GB", "5.0 GB", "1 d 2 h"]
    assert [action.id for action in server.actions] == ["start", "restart", "stop"]

    arr = extensions.ExtensionAdapter(extensions.parse((EXAMPLES / "whisparr.yaml").read_text()))
    health = arr.demo("health", {}, 1)
    assert health.primary["value"] == 1 and health.status == "warn"
    assert health.secondary[1]["value"].startswith("Indexers unavailable")


def test_the_settings_page_imports_lists_and_removes(client: TestClient) -> None:
    setup_admin(client)
    try:
        assert client.get("/api/v1/extensions").json()["extensions"] == []
        refused = client.post("/api/v1/extensions", json={"text": "id: x"}, headers=CSRF)
        assert refused.status_code == 400

        made = client.post("/api/v1/extensions", json={"text": SIMPLE}, headers=CSRF)
        assert made.status_code == 201, made.text
        assert made.json()["kind"] == "ext-simple"
        assert any(one["kind"] == "ext-simple" for one in client.get("/api/v1/adapters").json())

        connection = client.post("/api/v1/integrations", headers=CSRF, json={
            "kind": "ext-simple", "name": "Mine", "config": {"url": BASE, "api_key": "secret-key"}})
        assert connection.status_code == 201, connection.text
        listing = client.get("/api/v1/extensions").json()
        assert listing["extensions"][0]["connections"] == 1
        assert client.get("/api/v1/extensions/simple/source").text == SIMPLE

        (Path(listing["folder"]) / "broken.yaml").write_text("id: [", encoding="utf-8")
        reloaded = client.post("/api/v1/extensions/reload", headers=CSRF).json()
        assert reloaded["broken"][0]["file"] == "broken.yaml"

        assert client.delete("/api/v1/extensions/simple", headers=CSRF).status_code == 204
        # The connection outlives its extension and says what happened, rather than breaking the list.
        rows = client.get("/api/v1/integrations").json()
        assert rows[0]["label"].endswith("(extension missing)")
    finally:
        extensions.LOADED.clear()
        extensions.BROKEN.clear()
