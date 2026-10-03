# Extensions

An **extension** adds a service to nexdeck without touching its code. It is
one YAML file that says:

- which connection fields the service needs (an API key, a user, an ID),
- how requests sign in (a header, a bearer token, basic auth, a query parameter),
- which cards it offers, which address each card asks, and where in the JSON
  answer its numbers and rows are.

Import it under **System > Extensions** (administrators only). From then on
it is an integration like any other: add a connection under **System >
Integrations**, then put its cards on a board from the widget library.

Nothing in the file is executed. It is read with a safe YAML loader, checked
field by field, and a mistake is refused with the line that is wrong. Every
request stays underneath the connection's URL; a path can never point at
another host.

The files live in `/data/extensions/`. A file copied there by hand appears
after **Reload folder**. Working examples are in
[`extensions/`](../extensions) at the top of this repository.

## A whole file

```yaml
id: whisparr                   # lowercase letters, digits and dashes; unique
name: Whisparr                 # shown in the catalogue
version: 1.0.0
description: Whisparr's download queue and its health warnings.
author: you
icon: whisparr                 # a name from dashboard-icons (github.com/homarr-labs/dashboard-icons)
category: downloads            # basics, feeds, generic, hosts, nas, downloads, media, network, monitoring, other
docs_url: https://wiki.servarr.com/whisparr
url_placeholder: http://whisparr:6969
guide:                         # steps shown in the connection sheet
  - "The API key is under Settings > General > Security."

fields:                        # "url" and "Ignore TLS errors" are added by themselves
  - name: api_key
    label: API key
    type: password             # text, password, url, number, bool, select, textarea
    required: true

auth:                          # sent with every request
  headers:
    X-Api-Key: "{{ api_key }}"
  # bearer: "{{ token }}"                          -> Authorization: Bearer ...
  # basic: { username: "{{ user }}", password: "{{ password }}" }
  # params: { apikey: "{{ api_key }}" }            -> ?apikey=...

test:                          # what "Test connection" asks
  path: /api/v3/system/status
  success: "Whisparr {{ version }} answers."

widgets:
  - id: queue
    name: Queue
    renderer: list
    path: /api/v3/queue
    params: { pageSize: 20 }
    refresh: 30                # seconds
    items: records             # where the array is
    title: "{{ title }}"
    subtitle: "{{ status }} · {{ size | bytes }}"
    value: "{{ timeleft }}"
```

## Requests

Every widget, the `test` and every action is a request:

| Key | Meaning |
|---|---|
| `path` | Appended to the connection's URL. May use `{{ field }}`. |
| `method` | `GET` (default), `POST`, `PUT`, `PATCH` or `DELETE`. |
| `params` | Query parameters. |
| `headers` | Extra headers for this request only. |
| `body` | A JSON body. Strings inside it may use `{{ field }}`. |
| `form` | A form body instead of JSON (`application/x-www-form-urlencoded`). |

In the request part, `{{ name }}` is a connection field.

## Cards

| `renderer` | What it draws | Keys it reads |
|---|---|---|
| `value` | One big number or word, with a sparkline for numbers. | `value`, `label`, `unit`, `decimals`, `warn_above`, `bad_above`, `warn_below`, `bad_below`, `rows` (chips underneath) |
| `gauge` | A dial. | as `value`, plus `max` (a number or a path); with `unit: "%"` no `max` is needed |
| `stats` | Several labelled rows; percentages become bars. | `rows`: a list of `{ label, value, unit, decimals }` |
| `list` | One row per element of an array. | `items` (the array), `title`, `subtitle`, `value`, `unit`, `status`, `link`, `limit` |

Every card also takes `name`, `description`, `refresh` (seconds, default 60),
`size: [columns, rows]`, `link` (where the card leads), `actions` and `demo`.

### Reading the answer

`value`, `items` and a gauge's `max` may be a bare path (`data.load`) or a
template. Everything else is a template:

- `{{ data.items[0].name }}` reads a value. Dots for fields, `[0]` for an
  element, `[*]` for every element, `$` for the whole answer.
- `{{ connection.url }}` reads a connection field from inside a card, for links:
  `link: "{{ connection.url }}/movie/{{ id }}"`.
- In a `list`, paths are relative to each element.
- A template that is nothing but one `{{ ... }}` keeps the value's type, so a
  number stays a number and gets a sparkline.

### Filters

Written after a bar, one after another: `{{ records | where(status, failed) | count }}`.

| Filter | Does |
|---|---|
| `count` | Number of elements of a list. |
| `sum`, `min`, `max`, `avg` | Over a list of numbers (`{{ disks[*].size \| sum }}`). |
| `first`, `last` | One element of a list. |
| `where(field)`, `where(field, value)` | The elements whose field is set, or equals a value. |
| `map(a=x, b=y)` | Replace one value with another: `map(2=up, 9=down)`. |
| `round(n)`, `int` | Round to `n` places, or to a whole number. |
| `divide(n)`, `multiply(n)`, `add(n)`, `subtract(n)` | Arithmetic. |
| `bytes` | `1610612736` → `1.5 GB`. |
| `duration` | Seconds → `1 d 2 h`, `3 h 5 min`, `12 min`. |
| `ago` | A time → `5 min ago`. Takes ISO dates and Unix seconds or milliseconds. |
| `date`, `datetime` | A time → `2026-10-03` or `2026-10-03 14:05`. |
| `upper`, `lower` | Case. |
| `join(sep)` | A list as text. |
| `default(x)` | `x` when the value is empty. |

### Row status

A list row is coloured when `status` comes out as `ok`, `warn` or `bad`:

```yaml
status: "{{ state | map(running=ok, degraded=warn, failed=bad) }}"
```

### Actions

Buttons at the bottom of a card. Each is a request plus a label:

```yaml
actions:
  - id: restart
    label: Restart
    icon: rotate-ccw           # a lucide.dev icon name
    confirm: true              # ask once before running
    danger: true               # draw it red
    path: /api/client/servers/{{ server_id }}/power
    method: POST
    body: { signal: restart }
    message: Restart sent.     # what the toast says; may read the answer
```

Who may press a button follows the board's sharing like every other action,
and every press is written to the log.

### Demo data

`demo` is an answer to pretend the service gave. nexdeck draws it when demo
mode is on for the connection or the installation, so a card can be tried
before anything real is connected:

```yaml
demo:
  records:
    - { title: Example, status: downloading, size: 1610612736, timeleft: "00:12:00" }
```

Without one, the card shows made-up numbers in its own shape.

## Updating and removing

Importing a file whose `id` is already there replaces the old version; the
connections and cards made with it keep working. Removing an extension keeps
its connections, whose cards then say that the extension is missing until it
is imported again.
