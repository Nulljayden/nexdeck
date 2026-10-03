"""Extensions: integrations written as YAML files instead of Python.

An extension is one file in ``<data>/extensions/``. It names a service, the
connection fields it needs, how to sign in, and the cards it offers. Each card
asks one address of the service and says where in the JSON answer its numbers
and rows are. Nothing in the file is executed: it is read with
``yaml.safe_load``, checked against the models below, and drawn by the same
renderers every built-in adapter uses.

The format is described in ``docs/extensions.md``; working examples live in
``extensions/`` at the top of the repository.

Templates
---------

Text in a card is a template. ``{{ path }}`` reads a value from the JSON
answer (``data.items[0].name``, ``records[*].size``), and filters after a bar
change it: ``{{ size | bytes }}``, ``{{ records | count }}``,
``{{ records | where(status, failed) | count }}``. A template that is nothing
but one ``{{ ... }}`` keeps the value's type, so a number stays a number and
gets a sparkline. ``connection.<field>`` reads a connection field, which is
how a row links back to the service: ``{{ connection.url }}/movie/{{ id }}``.

In the request part (path, params, headers, body, auth) the context is the
connection itself: ``{{ api_key }}``.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator
from pydantic import Field as PydanticField

from ..adapters.base import (
    Action,
    Adapter,
    AdapterError,
    Context,
    Field,
    WidgetData,
    WidgetType,
    base_url,
)
from ..config import get_settings
from .jsonpath import PathError, as_number, extract

logger = logging.getLogger("nexdeck.extensions")

#: Every extension's adapter kind starts with this, so it can never claim the
#: kind of a built-in adapter and its connections are recognisable as such.
PREFIX = "ext-"
ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$"
#: The renderers an extension may draw with. The others need shapes (posters,
#: cameras, players) that a path into a JSON answer cannot describe.
RENDERERS = ("value", "gauge", "stats", "list")
CATEGORIES = ("basics", "feeds", "generic", "hosts", "nas", "downloads", "media", "network", "monitoring", "other")
#: Generous for a text file, small enough that nobody uploads a dump by mistake.
MAX_BYTES = 256 * 1024
#: Field names the extension does not choose: they are added to every one.
RESERVED_FIELDS = ("url", "insecure")


class ExtensionError(ValueError):
    """A file that is not a usable extension; the message says why."""


# ---------------------------------------------------------------------------
# The file format
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    # ⚠️ A misspelt key ("pararms") must fail loudly. Ignored, it would be a
    # card that silently asks the wrong question.
    model_config = ConfigDict(extra="forbid")


class OptionSpec(_Strict):
    value: str
    label: str


class FieldSpec(_Strict):
    name: str = PydanticField(pattern=r"^[a-z_][a-z0-9_]{0,39}$")
    label: str = PydanticField(min_length=1, max_length=80)
    type: Literal["text", "password", "url", "number", "bool", "select", "textarea"] = "text"
    required: bool = False
    secret: bool = False
    default: Any = None
    help: str = ""
    placeholder: str = ""
    options: list[OptionSpec] = PydanticField(default_factory=list)

    @field_validator("name")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if value in RESERVED_FIELDS:
            raise ValueError(f"{value!r} is added to every extension by itself; pick another name.")
        return value

    @model_validator(mode="after")
    def _password_is_secret(self) -> FieldSpec:
        # A password field that is stored in the clear is a mistake nobody means.
        if self.type == "password":
            self.secret = True
        return self


class BasicSpec(_Strict):
    username: str
    password: str


class AuthSpec(_Strict):
    #: Sent with every request; values are templates over the connection.
    headers: dict[str, str] = PydanticField(default_factory=dict)
    params: dict[str, str] = PydanticField(default_factory=dict)
    #: Shortcut for ``Authorization: Bearer <token>``.
    bearer: str = ""
    basic: BasicSpec | None = None


Scalar = str | int | float | bool


class RequestSpec(_Strict):
    path: str = ""
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    params: dict[str, Scalar] = PydanticField(default_factory=dict)
    headers: dict[str, str] = PydanticField(default_factory=dict)
    #: A JSON body; strings anywhere inside it are templates.
    body: Any = None
    #: A form body instead of JSON.
    form: dict[str, Scalar] | None = None


class TestSpec(RequestSpec):
    #: What a passed test says; a template over the answer.
    success: str = "Connected."


class RowSpec(_Strict):
    label: str
    value: str
    unit: str = ""
    decimals: int | None = PydanticField(default=None, ge=0, le=6)


class ActionSpec(RequestSpec):
    id: str = PydanticField(pattern=r"^[a-z0-9_-]{1,40}$")
    label: str = PydanticField(min_length=1, max_length=60)
    icon: str = ""
    confirm: bool = False
    danger: bool = False
    #: What the toast says afterwards; a template over the answer.
    message: str = "Done."


class WidgetSpec(RequestSpec):
    id: str = PydanticField(pattern=r"^[a-z0-9_]{1,40}$")
    name: str = PydanticField(min_length=1, max_length=60)
    description: str = ""
    renderer: Literal["value", "gauge", "stats", "list"]
    refresh: int = PydanticField(default=60, ge=5, le=86400)
    size: tuple[int, int] | None = None

    # value and gauge: one number or text.
    #: A path into the answer, or a template. For a list: the template of each row's value.
    value: str = ""
    label: str = ""
    unit: str = ""
    decimals: int | None = PydanticField(default=None, ge=0, le=6)
    #: For a gauge: what counts as full, a number or a path.
    max: str | float | None = None
    warn_above: float | None = None
    bad_above: float | None = None
    warn_below: float | None = None
    bad_below: float | None = None

    # stats: several rows; also chips under a value or gauge.
    rows: list[RowSpec] = PydanticField(default_factory=list)

    # list: one row per element of an array.
    items: str = ""
    title: str = ""
    subtitle: str = ""
    #: A template that comes out as ok, warn or bad, per row.
    status: str = ""
    limit: int = PydanticField(default=10, ge=1, le=100)

    #: Where the card (or, for a list, each row) leads; a template.
    link: str = ""
    actions: list[ActionSpec] = PydanticField(default_factory=list)
    #: An answer to pretend the service gave in demo mode.
    demo: Any = None

    @model_validator(mode="after")
    def _complete(self) -> WidgetSpec:
        if self.renderer in ("value", "gauge") and not self.value:
            raise ValueError(f"The {self.renderer} card {self.id!r} needs value: the path of its number.")
        if self.renderer == "stats" and not self.rows:
            raise ValueError(f"The stats card {self.id!r} needs rows.")
        if self.renderer == "list" and not (self.items and self.title):
            raise ValueError(f"The list card {self.id!r} needs items (the path of the array) and title.")
        ids = [one.id for one in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Two actions of the card {self.id!r} share an id.")
        return self


class ExtensionSpec(_Strict):
    id: str = PydanticField(pattern=ID_PATTERN)
    name: str = PydanticField(min_length=1, max_length=60)
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    #: A dashboard-icons name, such as ``radarr``.
    icon: str = ""
    category: Literal["basics", "feeds", "generic", "hosts", "nas", "downloads", "media", "network", "monitoring", "other"] = "other"
    docs_url: str = ""
    #: Shown in the connection sheet: what to set up at the service first.
    guide: list[str] = PydanticField(default_factory=list)
    #: The URL field's placeholder, such as ``http://radarr:7878``.
    url_placeholder: str = "http://service:8080"
    fields: list[FieldSpec] = PydanticField(default_factory=list)
    auth: AuthSpec = PydanticField(default_factory=AuthSpec)
    test: TestSpec | None = None
    widgets: list[WidgetSpec] = PydanticField(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> ExtensionSpec:
        names = [one.name for one in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("Two fields share a name.")
        ids = [one.id for one in self.widgets]
        if len(ids) != len(set(ids)):
            raise ValueError("Two widgets share an id.")
        return self


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

#: ⚠️ Never across a closing pair: a lazy ``.*?`` still stretches from the first
#: ``{{`` to the last ``}}`` when the whole string has to match.
_EXPRESSION = re.compile(r"\{\{\s*((?:(?!\}\}).)*?)\s*\}\}")
_FILTER = re.compile(r"^([a-z_]+)(?:\((.*)\))?$")


def _arguments(text: str | None) -> list[str]:
    if not text:
        return []
    return [part.strip().strip("'\"") for part in text.split(",")]


def _lookup(path: str, scope: dict[str, Any]) -> Any:
    path = path.strip()
    if (path.startswith('"') and path.endswith('"')) or (path.startswith("'") and path.endswith("'")):
        return path[1:-1]
    if path.startswith("connection.") or path == "connection":
        connection = scope.get("connection") or {}
        return connection if path == "connection" else connection.get(path[len("connection."):])
    return extract(scope.get("data"), path)


def _human_bytes(value: Any) -> str:
    number = as_number(value)
    if number is None:
        return str(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(number) < 1024 or unit == "PB":
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024
    return str(value)


def _duration(value: Any) -> str:
    number = as_number(value)
    if number is None:
        return str(value)
    seconds = int(number)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"


def _when(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    number = as_number(value) if not isinstance(value, str) or _looks_numeric(value) else None
    if number is not None:
        # Seconds, or milliseconds when it is too large to be seconds.
        return datetime.fromtimestamp(number / 1000 if number > 1e11 else number, UTC)
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _ago(value: Any) -> str:
    moment = _when(value)
    if moment is None:
        return str(value or "")
    seconds = (datetime.now(UTC) - moment).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 90:
        text = f"{int(seconds)} s"
    elif seconds < 5400:
        text = f"{int(seconds // 60)} min"
    elif seconds < 172800:
        text = f"{int(seconds // 3600)} h"
    else:
        text = f"{int(seconds // 86400)} d"
    return f"in {text}" if future else f"{text} ago"


def _apply(value: Any, name: str, args: list[str]) -> Any:
    if name == "default":
        return value if value not in (None, "", []) else (args[0] if args else "")
    if name in ("count", "length"):
        return len(value) if isinstance(value, (list, dict, str)) else 0
    if name == "sum":
        return sum(as_number(one) or 0 for one in value) if isinstance(value, list) else as_number(value) or 0
    if name in ("min", "max", "avg"):
        numbers = [n for n in (as_number(one) for one in (value if isinstance(value, list) else [value])) if n is not None]
        if not numbers:
            return None
        return min(numbers) if name == "min" else max(numbers) if name == "max" else sum(numbers) / len(numbers)
    if name in ("first", "last"):
        if isinstance(value, list):
            return (value[0] if name == "first" else value[-1]) if value else None
        return value
    if name == "where":
        # where(field, value): the elements whose field equals value.
        # where(field): the elements whose field is truthy.
        if not isinstance(value, list):
            return []
        field_name = args[0] if args else ""
        kept = []
        for one in value:
            try:
                got = extract(one, field_name)
            except PathError:
                continue
            if len(args) < 2:
                if got:
                    kept.append(one)
            elif str(got).lower() == args[1].lower():
                kept.append(one)
        return kept
    if name == "join":
        separator = args[0] if args else ", "
        return separator.join(str(one) for one in value) if isinstance(value, list) else str(value)
    if name == "upper":
        return str(value).upper()
    if name == "lower":
        return str(value).lower()
    if name == "bytes":
        return _human_bytes(value)
    if name == "duration":
        return _duration(value)
    if name == "ago":
        return _ago(value)
    if name == "date":
        moment = _when(value)
        return moment.astimezone().strftime("%Y-%m-%d") if moment else str(value or "")
    if name == "datetime":
        moment = _when(value)
        return moment.astimezone().strftime("%Y-%m-%d %H:%M") if moment else str(value or "")
    number = as_number(value)
    if name == "int":
        return int(number) if number is not None else value
    if name == "round":
        if number is None:
            return value
        places = int(args[0]) if args else 0
        return round(number, places) if places else int(round(number))
    if name in ("divide", "multiply", "add", "subtract"):
        if number is None or not args:
            return value
        other = float(args[0])
        if name == "divide":
            return number / other if other else None
        if name == "multiply":
            return number * other
        return number + other if name == "add" else number - other
    if name == "map":
        # map(a=x, b=y): replace one word with another, such as up=ok, down=bad.
        for pair in args:
            key, _, replacement = pair.partition("=")
            if str(value).lower() == key.strip().lower():
                return replacement.strip()
        return value
    raise PathError(f"There is no filter called {name!r}.")


def evaluate(expression: str, scope: dict[str, Any]) -> Any:
    """One ``path | filter | filter(arg)`` expression."""
    pieces = [piece.strip() for piece in expression.split("|")]
    value = _lookup(pieces[0], scope)
    for piece in pieces[1:]:
        match = _FILTER.match(piece)
        if not match:
            raise PathError(f"Cannot read the filter {piece!r}.")
        value = _apply(value, match.group(1), _arguments(match.group(2)))
    return value


def render(template: Any, scope: dict[str, Any], *, strict: bool = False) -> Any:
    """Fill a template. A lone ``{{ ... }}`` keeps the value's own type.

    ``strict`` raises when a path is missing; otherwise it reads as empty, which
    is what a row of a list wants when one element lacks a field.
    """
    if not isinstance(template, str):
        return template
    whole = _EXPRESSION.fullmatch(template.strip())
    if whole:
        try:
            return evaluate(whole.group(1), scope)
        except PathError:
            if strict:
                raise
            return ""

    def one(match: re.Match[str]) -> str:
        try:
            value = evaluate(match.group(1), scope)
        except PathError:
            if strict:
                raise
            return ""
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return str(value)

    return _EXPRESSION.sub(one, template)


def value_of(spec: str, scope: dict[str, Any], *, strict: bool = True) -> Any:
    """A ``value:`` entry: a template if it has braces, else a path."""
    if "{{" in spec:
        return render(spec, scope, strict=strict)
    return render("{{ " + spec + " }}", scope, strict=strict)


def _fill(thing: Any, scope: dict[str, Any]) -> Any:
    """Templates anywhere inside a body, a dict of params or a list."""
    if isinstance(thing, str):
        return render(thing, scope)
    if isinstance(thing, dict):
        return {key: _fill(value, scope) for key, value in thing.items()}
    if isinstance(thing, list):
        return [_fill(value, scope) for value in thing]
    return thing


# ---------------------------------------------------------------------------
# The adapter an extension becomes
# ---------------------------------------------------------------------------


def _join(base: str, path: str) -> str:
    """The request address, always underneath the connection's address.

    ⚠️ The same rule as the JSON API adapter: a path never names another host,
    or a template filled from somewhere unexpected could send the connection's
    credentials elsewhere.
    """
    path = (path or "").strip().lstrip("/\\")
    if "://" in path:
        raise AdapterError("A request path in this extension names a whole address.", code="bad_path",
                           hint="Paths are written relative to the connection's URL.")
    return base.rstrip("/") + ("/" + path if path else "")


def _status(number: float, spec: WidgetSpec) -> str:
    if spec.bad_above is not None and number > spec.bad_above:
        return "bad"
    if spec.bad_below is not None and number < spec.bad_below:
        return "bad"
    if spec.warn_above is not None and number > spec.warn_above:
        return "warn"
    if spec.warn_below is not None and number < spec.warn_below:
        return "warn"
    return "ok"


def _shown(value: Any, decimals: int | None) -> tuple[Any, float | None]:
    """The value as it is drawn, and the number behind it if there is one."""
    if isinstance(value, bool):
        return ("yes" if value else "no"), None
    number = as_number(value) if not isinstance(value, str) or _looks_numeric(value) else None
    if number is None:
        if isinstance(value, (dict, list)):
            return str(value)[:60], None
        return ("" if value is None else value), None
    if decimals is None:
        return (int(number) if float(number).is_integer() else round(number, 2)), number
    return (round(number, decimals) if decimals else int(round(number))), number


def _looks_numeric(text: str) -> bool:
    return bool(re.fullmatch(r"\s*-?\d+([.,]\d+)?\s*", text))


class ExtensionAdapter(Adapter):
    """One extension file, standing in for a Python adapter."""

    def __init__(self, spec: ExtensionSpec, path: Path | None = None) -> None:
        self.spec = spec
        self.path = path
        self.kind = PREFIX + spec.id
        self.label = spec.name
        self.category = spec.category
        self.description = spec.description or f"{spec.name}, from an extension."
        self.icon = spec.icon or "puzzle"
        # Nobody has confirmed an extension but its author.
        self.beta = False
        self.docs_url = spec.docs_url
        self.guide = tuple(spec.guide)
        self.keywords = ("extension", spec.id)
        self.fields = (
            Field("url", "URL", type="url", required=True, placeholder=spec.url_placeholder),
            *(
                Field(
                    one.name, one.label, type=one.type, required=one.required, secret=one.secret,
                    default=one.default, help=one.help, placeholder=one.placeholder,
                    options=tuple((option.value, option.label) for option in one.options),
                )
                for one in spec.fields
            ),
            Field("insecure", "Ignore TLS errors", type="bool", default=False,
                  help="For a service with a self-signed certificate."),
        )
        self.widgets = tuple(self._widget_type(one) for one in spec.widgets)

    @staticmethod
    def _widget_type(spec: WidgetSpec) -> WidgetType:
        metrics: tuple[str, ...] = ()
        if spec.renderer in ("value", "gauge"):
            metrics = ("value",)
        elif spec.renderer == "stats":
            metrics = tuple(f"row{index}" for index in range(len(spec.rows)))
        default = spec.size or {"value": (2, 2), "gauge": (2, 2), "stats": (3, 2), "list": (3, 3)}[spec.renderer]
        return WidgetType(
            kind=spec.id,
            label=spec.name,
            description=spec.description or spec.name,
            renderer=spec.renderer,
            default_size=tuple(default),
            refresh_seconds=spec.refresh,
            metrics=metrics,
            bars=spec.renderer == "list" and bool(spec.value),
        )

    def _widget_spec(self, kind: str) -> WidgetSpec:
        for one in self.spec.widgets:
            if one.id == kind:
                return one
        raise AdapterError(f"This extension has no card {kind!r}.", code="unknown_widget")

    def to_dict(self) -> dict[str, Any]:
        return {**super().to_dict(), "extension": True}

    # -- requests ------------------------------------------------------------

    async def _call(self, request: RequestSpec, config: dict[str, Any], ctx: Context, *, cache: bool) -> Any:
        scope = {"data": config, "connection": config}
        auth = self.spec.auth
        headers = {"Accept": "application/json"}
        headers.update({name: str(render(value, scope)) for name, value in auth.headers.items()})
        if auth.bearer:
            headers["Authorization"] = f"Bearer {render(auth.bearer, scope)}"
        headers.update({name: str(render(value, scope)) for name, value in request.headers.items()})
        params: dict[str, Any] = {name: render(value, scope) for name, value in auth.params.items()}
        params.update({name: render(value, scope) for name, value in request.params.items()})
        basic = (str(render(auth.basic.username, scope)), str(render(auth.basic.password, scope))) if auth.basic else None
        url = _join(base_url(config), str(render(request.path, scope)))
        response = await ctx.request(
            request.method, url,
            headers=headers, params=params or None,
            json_body=_fill(request.body, scope) if request.body is not None else None,
            data=_fill(request.form, scope) if request.form is not None else None,
            verify=not config.get("insecure"), auth=basic,
            cache_seconds=5 if cache and request.method == "GET" else 0,
        )
        if response.status_code >= 400:
            raise AdapterError(
                f"The service answered with HTTP {response.status_code}.",
                code="http_error",
                hint="Check the URL and the credentials of the connection.",
            )
        if not response.content.strip():
            return None
        try:
            return response.json()
        except ValueError:
            # Plain text is an answer too: a version string, "OK", a number.
            return response.text.strip()

    async def test(self, config: dict[str, Any], ctx: Context) -> str:
        probe = self.spec.test or TestSpec(path=self.spec.widgets[0].path, method=self.spec.widgets[0].method,
                                           params=self.spec.widgets[0].params)
        answer = await self._call(probe, config, ctx, cache=False)
        return str(render(probe.success, {"data": answer, "connection": config}) or "Connected.")

    async def fetch(self, widget_kind: str, config: dict[str, Any], options: dict[str, Any], ctx: Context) -> WidgetData:
        spec = self._widget_spec(widget_kind)
        answer = await self._call(spec, config, ctx, cache=True)
        return self.shape(spec, answer, config)

    async def action(self, widget_kind: str, action_id: str, params: dict[str, Any], config: dict[str, Any],
                     options: dict[str, Any], ctx: Context) -> str:
        spec = self._widget_spec(widget_kind)
        chosen = next((one for one in spec.actions if one.id == action_id), None)
        if chosen is None:
            raise AdapterError("This card has no such action.", code="no_such_action")
        answer = await self._call(chosen, config, ctx, cache=False)
        ctx.forget_answers()
        return str(render(chosen.message, {"data": answer, "connection": config}) or "Done.")

    def demo(self, widget_kind: str, options: dict[str, Any], tick: int) -> WidgetData:
        from ..adapters import demo as fake

        spec = self._widget_spec(widget_kind)
        connection = {"url": "https://demo.invalid"}
        if spec.demo is not None:
            try:
                return self.shape(spec, spec.demo, connection)
            except AdapterError:
                pass
        # Without a sample answer: believable numbers in the card's own shape.
        seed = f"{self.kind}.{spec.id}"
        if spec.renderer in ("value", "gauge"):
            number = fake.walk(seed, tick, 20, 80)
            return WidgetData(primary={"label": spec.label or spec.name, "value": number, "unit": spec.unit},
                              metrics={"value": number},
                              meta={"gauge": {"share": number}} if spec.renderer == "gauge" else {})
        if spec.renderer == "stats":
            rows = [{"label": row.label, "value": fake.walk(f"{seed}{index}", tick, 1, 100), "unit": row.unit}
                    for index, row in enumerate(spec.rows)]
            return WidgetData(primary=rows[0], secondary=rows[1:],
                              metrics={f"row{index}": row["value"] for index, row in enumerate(rows)})
        return WidgetData(items=[{"title": f"Entry {index + 1}", "subtitle": "sample"} for index in range(4)])

    # -- shaping -------------------------------------------------------------

    def shape(self, spec: WidgetSpec, answer: Any, config: dict[str, Any]) -> WidgetData:
        """Turn the service's answer into a card, the way the file describes."""
        scope = {"data": answer, "connection": config}
        try:
            data = self._shape(spec, scope)
        except PathError as failure:
            raise AdapterError(str(failure), code="path_error",
                               hint="The extension looks for something the answer does not have.") from failure
        if spec.link and spec.renderer != "list":
            data.link = str(render(spec.link, scope)) or None
        if spec.actions:
            data.actions = [Action(id=one.id, label=one.label, icon=one.icon, confirm=one.confirm, danger=one.danger)
                            for one in spec.actions]
        return data

    def _rows(self, spec: WidgetSpec, scope: dict[str, Any], first: int = 0) -> tuple[list[dict[str, Any]], dict[str, float]]:
        rows: list[dict[str, Any]] = []
        metrics: dict[str, float] = {}
        for index, row in enumerate(spec.rows):
            shown, number = _shown(value_of(row.value, scope, strict=False), row.decimals)
            rows.append({"label": row.label, "value": shown, "unit": row.unit})
            if number is not None:
                metrics[f"row{index + first}"] = number
        return rows, metrics

    def _shape(self, spec: WidgetSpec, scope: dict[str, Any]) -> WidgetData:
        if spec.renderer in ("value", "gauge"):
            raw = value_of(spec.value, scope)
            shown, number = _shown(raw, spec.decimals)
            data = WidgetData(primary={"label": spec.label, "value": shown, "unit": spec.unit})
            if number is not None:
                data.metrics = {"value": number}
                data.status = _status(number, spec)
            chips, _ = self._rows(spec, scope)
            data.secondary = chips
            if spec.renderer == "gauge" and number is not None:
                ceiling = spec.max
                if isinstance(ceiling, str):
                    ceiling = as_number(value_of(ceiling, scope, strict=False))
                if ceiling:
                    data.meta = {"gauge": {"share": round(max(0.0, min(100.0, number / float(ceiling) * 100)), 1),
                                           "max": float(ceiling)}}
                elif spec.unit == "%":
                    data.meta = {"gauge": {"share": max(0.0, min(100.0, number))}}
            return data
        if spec.renderer == "stats":
            rows, metrics = self._rows(spec, scope)
            return WidgetData(primary=rows[0], secondary=rows[1:], metrics=metrics)
        found = value_of(spec.items, scope)
        if isinstance(found, dict):
            # An object of objects, such as {"sda": {...}, "sdb": {...}}: one row each.
            found = [{"key": key, **(value if isinstance(value, dict) else {"value": value})} for key, value in found.items()]
        if not isinstance(found, list):
            raise PathError("The items path does not point at a list.")
        items = []
        for element in found[: spec.limit]:
            row_scope = {"data": element, "connection": scope["connection"]}
            item: dict[str, Any] = {"title": str(render(spec.title, row_scope))}
            if spec.subtitle:
                item["subtitle"] = str(render(spec.subtitle, row_scope))
            if spec.value:
                shown, _ = _shown(value_of(spec.value, row_scope, strict=False), spec.decimals)
                item["value"] = f"{shown} {spec.unit}".strip() if spec.unit and shown != "" else shown
            if spec.status:
                state = str(render(spec.status, row_scope)).strip().lower()
                if state in ("ok", "warn", "bad"):
                    item["status"] = state
            if spec.link:
                item["url"] = str(render(spec.link, row_scope))
            items.append(item)
        return WidgetData(items=items, secondary=[{"label": "Total", "value": len(found)}])


class MissingExtension(Adapter):
    """Stands in for an extension that was removed while connections still use it.

    ⚠️ Without it, one deleted file would make every page that lists
    connections fail, because each of them asks the registry for the adapter.
    """

    category = "other"
    icon = "puzzle"
    beta = False

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.label = f"{kind[len(PREFIX):]} (extension missing)"
        self.description = "The extension this connection was made with is no longer installed."
        self.fields = ()
        self.widgets = ()

    def widget(self, kind: str) -> WidgetType:
        return WidgetType(kind=kind, label=kind, description="", renderer="value")

    async def test(self, config: dict[str, Any], ctx: Context) -> str:
        raise self._gone()

    async def fetch(self, widget_kind: str, config: dict[str, Any], options: dict[str, Any], ctx: Context) -> WidgetData:
        raise self._gone()

    def demo(self, widget_kind: str, options: dict[str, Any], tick: int) -> WidgetData:
        return WidgetData(status="unknown", error="The extension of this card is no longer installed.")

    def _gone(self) -> AdapterError:
        return AdapterError("The extension of this connection is no longer installed.", code="extension_missing",
                            hint="Import it again under System > Extensions.")


# ---------------------------------------------------------------------------
# Loading, importing, removing
# ---------------------------------------------------------------------------

LOADED: dict[str, ExtensionAdapter] = {}
#: Files that are in the folder and could not be read, by file name.
BROKEN: dict[str, str] = {}
_lock = threading.Lock()


def directory() -> Path:
    return get_settings().data_dir / "extensions"


def parse(text: str) -> ExtensionSpec:
    """Read and check one extension; raises :class:`ExtensionError` with a readable reason."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ExtensionError(f"The file is larger than {MAX_BYTES // 1024} KB.")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as failure:
        raise ExtensionError(f"The file is not valid YAML: {failure}") from failure
    if not isinstance(document, dict):
        raise ExtensionError("The file does not describe an extension: it needs id, name and widgets at the top.")
    try:
        return ExtensionSpec.model_validate(document)
    except ValidationError as failure:
        first = failure.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ()))
        message = str(first.get("msg", "invalid")).removeprefix("Value error, ")
        raise ExtensionError(f"{where}: {message}" if where else message) from failure


def reload() -> None:
    """Read every file in the folder again."""
    with _lock:
        LOADED.clear()
        BROKEN.clear()
        folder = directory()
        if not folder.is_dir():
            return
        for path in sorted([*folder.glob("*.yaml"), *folder.glob("*.yml")]):
            try:
                spec = parse(path.read_text(encoding="utf-8"))
            except (ExtensionError, OSError, UnicodeDecodeError) as failure:
                BROKEN[path.name] = str(failure)
                logger.warning("The extension %s could not be loaded: %s", path.name, failure)
                continue
            kind = PREFIX + spec.id
            if kind in LOADED:
                BROKEN[path.name] = f"Another file already uses the id {spec.id!r}."
                continue
            LOADED[kind] = ExtensionAdapter(spec, path)
        if LOADED:
            logger.info("Loaded %d extension(s): %s", len(LOADED), ", ".join(sorted(LOADED)))


def install(text: str) -> ExtensionAdapter:
    """Check a file and put it in the folder, replacing an older version of the same id."""
    spec = parse(text)
    folder = directory()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{spec.id}.yaml"
    # A file of the same id under another name would load twice.
    existing = LOADED.get(PREFIX + spec.id)
    if existing and existing.path and existing.path != target and existing.path.exists():
        existing.path.unlink()
    target.write_text(text, encoding="utf-8")
    reload()
    return LOADED[PREFIX + spec.id]


def remove(extension_id: str) -> None:
    adapter = LOADED.get(PREFIX + extension_id)
    if adapter is None or adapter.path is None:
        raise ExtensionError("There is no such extension.")
    adapter.path.unlink(missing_ok=True)
    reload()


def summary(adapter: ExtensionAdapter) -> dict[str, Any]:
    spec = adapter.spec
    return {
        "id": spec.id,
        "kind": adapter.kind,
        "name": spec.name,
        "version": spec.version,
        "description": spec.description,
        "author": spec.author,
        "icon": adapter.icon,
        "category": spec.category,
        "docs_url": spec.docs_url,
        "file": adapter.path.name if adapter.path else "",
        "widgets": [{"id": one.id, "name": one.name, "renderer": one.renderer} for one in spec.widgets],
    }
