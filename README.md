# ha_lists — Home Assistant Lists & Tasks Verba for Tater

A [Tater](https://github.com/TaterTotterson/Tater) Verba that manages Home
Assistant to-do lists and the shopping list by voice or chat: add, edit
(rename / mark complete / due date / details), remove, and read back items.

Version **0.2.1** · Tater >= 0.4.0 · Platforms: webui, discord, voice_core

## How it works

One Verba = one tool named `ha_lists`. The LLM picks an `action` and a
`list_type`, and the Verba dispatches internally:

| argument | values | notes |
|---|---|---|
| `action` | `add` / `edit` / `delete` / `show` | what to do; `show` reads a list's items back |
| `list_type` | `shopping` / `todo` | `shopping` is only the plain shopping/grocery list; `todo` is any named list (store lists like Costco, errands, chores) |
| `item` | text | the item or task name; not needed for `show` |
| `quantity` | text | shopping adds only, folded into the name ("milk (2)") |
| `list_name` | text | todo lists only, fuzzy matched against friendly names |
| `rename` | text | edit only |
| `status` | `needs_action` / `completed` | edit only; `completed` marks done |
| `due_date` | `YYYY-MM-DD` | todo lists only |
| `description` | text | todo lists only |

Example calls:

- "Add milk to the shopping list"
- "Add pickles to my Costco list"
- "Add call the plumber to my errands list for tomorrow"
- "What's on my Costco list?"
- "Mark milk as done on the shopping list"
- "Rename milk to whole milk on the shopping list"
- "Remove milk from the shopping list"

## Home Assistant connection

There are **no verba settings**. The connection (base URL + long-lived
token) comes from Tater's `homeassistant` integration — configure it once
in Tater and this Verba reuses it
(`integration_store.integration_function("homeassistant",
"load_homeassistant_config")`). If the integration is missing or HA is
unreachable, the Verba says so and fails gracefully.

Under the hood it uses the HA REST API:
`todo/add_item`, `todo/update_item`, `todo/remove_item`, and
`todo/get_items?return_response` for the duplicate check. The shopping
list entity is `todo.shopping_list`; to-do lists are discovered from
`todo.*` entities.

## Install

**From the verba management area (recommended):** add this repo's manifest as
a shop source in Tater's verba management area:

```
https://raw.githubusercontent.com/heapsoftware/Tater-HA-Lists/main/manifest.json
```

Then install **Home Assistant Lists & Tasks** from the catalog — Tater
downloads `verba/ha_lists.py`, verifies its SHA256, and reloads verbas.

**Manual:** copy `verba/ha_lists.py` into your Tater verba directory (or
point `TATER_VERBA_DIR` at this repo's `verba/` folder) and reload verbas.
Requires only what Tater already ships (`aiohttp`).

**Prerequisite:** configure the `homeassistant` integration in Tater first —
this verba has no settings of its own and reuses that connection.

## Development

The Tater clone used for development lives outside this repo (sibling
`Tater/` folder by default; override with `TATER_PATH`).

```bash
python3 scripts/test_ha_lists.py
```

Runs 45 checks against a fake Home Assistant REST server — no network,
no real HA. Returns non-zero on failure.
