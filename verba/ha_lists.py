"""Verba: manage Home Assistant to-do lists and the shopping list.

Loaded from the verba directory by verba_loader.py, which expects a
module-level `verba` instance of ToolVerba. One Verba = one tool: this file
is the `ha_lists` tool for Hydra. The LLM picks an `action`
(add/edit/delete/show) and a `list_type` (shopping/todo); dispatch happens
here.

The Home Assistant connection (base URL + long-lived token) comes from the
`homeassistant` integration installed in Tater; this Verba defines no
settings of its own.
"""

import asyncio
import logging

import aiohttp

from tateros import integration_store as integration_store_module
from verba_base import ToolVerba
from verba_result import action_failure, action_success

logger = logging.getLogger("ha_lists")

_HA_TIMEOUT = aiohttp.ClientTimeout(total=15)
_TODO_ENTITY_PREFIX = "todo."
_SHOPPING_LIST_ENTITY = "todo.shopping_list"
_ACTIONS = {"add", "edit", "delete", "show"}
_LIST_TYPES = {"shopping", "todo"}
_SHOPPING_KEYWORDS = ("shop", "grocer")


def _ha_config() -> tuple[str, str]:
    """Resolve the shared HA connection from Tater's homeassistant integration."""
    try:
        fn = integration_store_module.integration_function(
            "homeassistant", "load_homeassistant_config"
        )
    except Exception:
        logger.exception("homeassistant integration lookup failed")
        return "", ""
    if fn is None:
        return "", ""
    try:
        cfg = fn(required=False)
    except Exception:
        logger.exception("load_homeassistant_config failed")
        return "", ""
    if not isinstance(cfg, dict):
        return "", ""
    base = str(cfg.get("base") or "").strip().rstrip("/")
    token = str(cfg.get("token") or "").strip()
    return base, token


def _unreachable() -> dict:
    return action_failure(
        code="ha_unreachable",
        message=(
            "Home Assistant is unreachable or the homeassistant integration "
            "is not configured."
        ),
        say_hint="I couldn't reach Home Assistant.",
    )


def _bearer_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _match_list(lists: list[dict], wanted: str) -> tuple[dict | None, bool]:
    """Fuzzy-match a list name. Returns (matched, ambiguous)."""
    wanted_l = str(wanted or "").strip().lower()
    if not wanted_l:
        return None, False
    exact = [
        entry
        for entry in lists
        if entry["friendly_name"].lower() == wanted_l
        or entry["entity_id"].lower() == wanted_l
        or entry["entity_id"].lower() == f"{_TODO_ENTITY_PREFIX}{wanted_l}"
    ]
    if exact:
        return exact[0], False
    partial = [
        entry
        for entry in lists
        if wanted_l in entry["friendly_name"].lower()
        or wanted_l in entry["entity_id"].lower()
    ]
    if len(partial) == 1:
        return partial[0], False
    if len(partial) > 1:
        return None, True
    return None, False


def _looks_shopping(text: str) -> bool:
    return any(keyword in text for keyword in _SHOPPING_KEYWORDS)


def _match_list_from_text(lists: list[dict], text: str) -> tuple[dict | None, bool]:
    """Match a list whose name appears in the user's utterance. Returns
    (matched, ambiguous) — ambiguous means several list names were mentioned."""
    hits = [
        entry
        for entry in lists
        if entry["friendly_name"].lower() in text
        or entry["entity_id"].lower() in text
    ]
    if len(hits) == 1:
        return hits[0], False
    if len(hits) > 1:
        return None, True
    return None, False


class HaListsVerba(ToolVerba):
    name = "ha_lists"
    verba_name = "Home Assistant Lists & Tasks"
    pretty_name = "Home Assistant Lists & Tasks"
    version = "0.2.2"
    # The canonical example the planner imitates for argument shape — the
    # named-list case is what it gets wrong on its own, so anchor it here.
    usage = '{"function": "ha_lists", "arguments": {"action": "add", "list_type": "todo", "item": "pickles", "list_name": "Costco"}}'
    platforms = ["webui", "discord", "voice_core"]
    notifier = False
    verba_dec = "Manages Home Assistant to-do items and shopping lists."
    description = (
        "Adds, updates, removes, and reads back items on Home Assistant "
        "to-do lists and the shopping list."
    )
    # NOTE: the planning LLM only sees the first ~77 characters of
    # when_to_use (tool_purpose truncates at 80) plus the single `usage`
    # example below. Those two strings carry the steering, so the lead
    # sentence there must cover: show, shopping list, named lists.
    when_to_use = (
        "Add, edit, delete, or show items on the shopping list or any named "
        "list. Use for requests like 'add pickles to my Costco list' or "
        "'what's on my errands list' — store and errand lists are todo "
        "lists with list_name."
    )
    how_to_use = (
        "Set action to add, edit, or delete and list_type to shopping or "
        "todo, plus item; use action=show to read the items on a list "
        "back to the user (item not needed)."
    )
    common_needs = ["action", "item"]
    example_calls = [
        "Add milk to the shopping list",
        "Add pickles to my Costco list",
        "Add call the plumber to my errands list for tomorrow",
        "Mark milk as done on the shopping list",
        "What's on my Costco list?",
        "Remove milk from the shopping list",
    ]
    missing_info_prompts = [
        "What would you like to add or change, and on which list?"
    ]
    settings_category = None
    required_settings = {}  # HA connection comes from the homeassistant integration

    argument_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["add", "edit", "delete", "show"],
                "description": (
                    "What to do: add/edit/delete an item, or show to read "
                    "the items on a list."
                ),
            },
            "list_type": {
                "type": "string",
                "enum": ["shopping", "todo"],
                "description": (
                    "'shopping' only for the plain shopping/grocery list; "
                    "'todo' for ANY named list, including store lists "
                    "(e.g. a Costco list) and errand/task lists. Always set "
                    "this."
                ),
            },
            "item": {
                "type": "string",
                "description": (
                    "The item or task name, e.g. 'milk' or 'Call the "
                    "plumber'. Required for add/edit/delete; omit for show."
                ),
            },
            "quantity": {
                "type": "string",
                "description": (
                    "add on the shopping list only: optional free-text "
                    "quantity such as '2' or 'a dozen'."
                ),
            },
            "list_name": {
                "type": "string",
                "description": (
                    "todo lists only: which named to-do list, e.g. 'Costco' "
                    "or 'errands' (fuzzy matched). Omit for the shopping "
                    "list."
                ),
            },
            "rename": {
                "type": "string",
                "description": "edit only: new name for the item.",
            },
            "status": {
                "type": "string",
                "enum": ["needs_action", "completed"],
                "description": "edit only: set to 'completed' to mark an item done.",
            },
            "due_date": {
                "type": "string",
                "description": (
                    "add/edit on to-do lists only: due date as an ISO date "
                    "(YYYY-MM-DD)."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "add/edit on to-do lists only: extra detail about the task."
                ),
            },
        },
        "required": ["action", "list_type"],
    }

    async def handle_webui(self, args, llm_client, request_text=""):
        return await self._run(args, request_text)

    async def handle_discord(self, args, llm_client, request_text=""):
        return await self._run(args, request_text)

    async def handle_voice_core(self, args, llm_client, request_text=""):
        return await self._run(args, request_text)

    async def _run(self, args, request_text="") -> dict:
        args = args if isinstance(args, dict) else {}
        action = str(args.get("action") or "").strip().lower()
        raw_item = str(args.get("item") or "").strip()
        list_type = str(args.get("list_type") or "").strip().lower()
        list_name = str(args.get("list_name") or "").strip()
        quantity = str(args.get("quantity") or "").strip()
        rename = str(args.get("rename") or "").strip()
        status = str(args.get("status") or "").strip().lower()
        due_date = str(args.get("due_date") or "").strip()
        description = str(args.get("description") or "").strip()

        if action not in _ACTIONS:
            return action_failure(
                code="unknown_action",
                message=(
                    f"Unknown action '{action}'. Valid actions: add, edit, "
                    "delete, show."
                ),
                needs=["action"],
                say_hint=(
                    "Ask whether the item should be added, updated, or "
                    "removed, or whether they want to hear what's on the list."
                ),
            )
        if action != "show" and not raw_item:
            return action_failure(
                code="missing_item",
                message="No item or task name was provided.",
                say_hint="Ask the user which item or task they mean.",
            )
        if status not in ("", "needs_action", "completed"):
            status = ""
        utterance = str(request_text or "").strip()
        list_type = self._resolve_list_type(list_type, list_name, utterance)
        if list_type not in _LIST_TYPES:
            return action_failure(
                code="missing_list_type",
                message=(
                    "It is unclear whether the item belongs on the shopping "
                    "list or a to-do list."
                ),
                needs=["list_type"],
                say_hint=(
                    "Ask whether the item belongs on the shopping list or a to-do list."
                ),
            )

        base, token = _ha_config()
        if not base or not token:
            return _unreachable()

        try:
            async with aiohttp.ClientSession(
                timeout=_HA_TIMEOUT, headers=_bearer_headers(token)
            ) as session:
                entity_id, list_label = "", ""
                if (
                    list_type == "todo"
                    and not list_name
                    and utterance
                    and _looks_shopping(utterance)
                ):
                    # The planner copies the usage example and sometimes
                    # sends list_type=todo even when the user said
                    # "shopping list"; trust the spoken words — they mean
                    # the plain shopping list.
                    list_type = "shopping"
                if list_type == "shopping":
                    async with session.get(
                        f"{base}/api/states/{_SHOPPING_LIST_ENTITY}"
                    ) as resp:
                        if resp.status == 404:
                            return action_failure(
                                code="shopping_list_missing",
                                message=(
                                    "The Home Assistant shopping list entity "
                                    "is missing."
                                ),
                                say_hint=(
                                    "The shopping list isn't enabled in Home Assistant."
                                ),
                            )
                        resp.raise_for_status()
                    entity_id, list_label = _SHOPPING_LIST_ENTITY, "shopping list"
                else:
                    lists = await self._todo_lists(session, base)
                    if not lists:
                        return action_failure(
                            code="no_todo_lists",
                            message="No to-do lists are available in Home Assistant.",
                            say_hint=(
                                "Tell the user no to-do lists were found in "
                                "Home Assistant."
                            ),
                        )
                    chosen, ambiguous = self._choose_list(lists, list_name, utterance)
                    if chosen is None:
                        available = ", ".join(
                            entry["friendly_name"] for entry in lists
                        )
                        if ambiguous:
                            wanted = list_name or "your request"
                            return action_failure(
                                code="ambiguous_list",
                                message=f"'{wanted}' matched several to-do lists.",
                                needs=["list_name"],
                                say_hint=(
                                    "Ask which to-do list to use. "
                                    f"Available lists: {available}."
                                ),
                            )
                        return action_failure(
                            code="list_not_found",
                            message=f"The to-do list '{list_name}' was not found.",
                            needs=["list_name"],
                            say_hint=(
                                f"Tell the user the list '{list_name}' wasn't "
                                f"found. Available lists: {available}."
                            ),
                        )
                    entity_id = chosen["entity_id"]
                    list_label = chosen["friendly_name"]

                if action == "show":
                    return await self._show(session, base, entity_id, list_label)
                if action == "add":
                    return await self._add(
                        session,
                        base,
                        entity_id,
                        list_label,
                        raw_item,
                        quantity,
                        due_date,
                        description,
                    )
                if action == "edit":
                    return await self._edit(
                        session,
                        base,
                        entity_id,
                        list_label,
                        raw_item,
                        rename,
                        status,
                        due_date,
                        description,
                    )
                return await self._delete(
                    session, base, entity_id, list_label, raw_item
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Home Assistant unreachable: %s", exc)
            return _unreachable()

    def _resolve_list_type(
        self, list_type: str, list_name: str, request_text: str = ""
    ) -> str:
        wanted = str(list_name or "").strip().lower()
        shopping_named = wanted and not _looks_shopping(wanted)
        if list_type == "shopping" and shopping_named:
            # "Add pickles to my Costco list": a named list that is not a
            # shopping synonym is a to-do list, even though it sounds like
            # shopping. Store lists are todo.* entities in Home Assistant.
            return "todo"
        if list_type in _LIST_TYPES:
            return list_type
        if wanted:
            return "shopping" if _looks_shopping(wanted) else "todo"
        if request_text and _looks_shopping(request_text):
            return "shopping"
        return ""

    def _choose_list(
        self, lists: list[dict], list_name: str, request_text: str = ""
    ) -> tuple[dict | None, bool]:
        """Pick a list by fuzzy name match, or apply defaults when unnamed."""
        if list_name:
            return _match_list(lists, list_name)
        text = str(request_text or "").strip().lower()
        if text:
            matched, ambiguous = _match_list_from_text(lists, text)
            if matched or ambiguous:
                return matched, ambiguous
        if len(lists) == 1:
            return lists[0], False
        return None, len(lists) > 1

    async def _add(
        self,
        session,
        base,
        entity_id: str,
        list_label: str,
        raw_item: str,
        quantity: str,
        due_date: str,
        description: str,
    ) -> dict:
        item = f"{raw_item} ({quantity})" if quantity else raw_item
        extras = {}
        if entity_id != _SHOPPING_LIST_ENTITY:
            # due_date/description are to-do-only; the shopping list does
            # not support them.
            if due_date:
                extras["due_date"] = due_date
            if description:
                extras["description"] = description
        if entity_id == _SHOPPING_LIST_ENTITY:
            active = await self._item_summaries(
                session, base, entity_id, "needs_action"
            )
            if any(name.lower() == item.lower() for name in active):
                return action_success(
                    facts={"action": "add", "item": item, "duplicate": True},
                    say_hint=f"{item} is already on your {list_label}.",
                    summary_for_user=f"{item} is already on your {list_label}.",
                )
        payload = {"entity_id": entity_id, "item": item}
        payload.update(extras)
        status, detail = await self._post_service(
            session, base, "todo/add_item", payload
        )
        dropped = False
        if status >= 400 and extras:
            # Entities feature-gate due_date/description; retry with the
            # item alone.
            logger.info(
                "add_item with extras rejected (%s); retrying without extras", status
            )
            dropped = True
            status, detail = await self._post_service(
                session, base, "todo/add_item", {"entity_id": entity_id, "item": item}
            )
        if status >= 400:
            logger.warning("todo.add_item failed (%s): %s", status, detail[:300])
            return action_failure(
                code="ha_error",
                message=f"Home Assistant rejected the add_item call: {detail[:200]}",
                say_hint=(
                    f"Tell the user {item} could not be added to the {list_label}."
                ),
            )
        if dropped:
            say_hint = (
                f"Added {item} to your {list_label}, but the due date or "
                "details couldn't be set."
            )
        else:
            say_hint = f"Added {item} to your {list_label}."
        logger.info("Added %r to %s", item, entity_id)
        return action_success(
            facts={"action": "add", "item": item, "list": list_label},
            say_hint=say_hint,
            summary_for_user=say_hint,
        )

    async def _edit(
        self,
        session,
        base,
        entity_id: str,
        list_label: str,
        raw_item: str,
        rename: str,
        status: str,
        due_date: str,
        description: str,
    ) -> dict:
        changes = {}
        if rename:
            changes["rename"] = rename
        if status:
            changes["status"] = status
        extras = {}
        if due_date:
            extras["due_date"] = due_date
        if description:
            extras["description"] = description
        changes.update(extras)
        if not changes:
            return action_failure(
                code="nothing_to_update",
                message="No changes were specified for the item.",
                say_hint="Ask the user what should change about the item.",
            )

        known = await self._known_item_names(session, base, entity_id)
        if raw_item.lower() not in {name.lower() for name in known}:
            return action_failure(
                code="item_not_found",
                message=f"'{raw_item}' is not on {list_label}.",
                say_hint=f"Tell the user {raw_item} isn't on the {list_label}.",
            )

        payload = {"entity_id": entity_id, "item": raw_item}
        payload.update(changes)
        status_code, detail = await self._post_service(
            session, base, "todo/update_item", payload
        )
        dropped = False
        if status_code >= 400 and extras:
            # Entities feature-gate due_date/description; retry with the
            # supported changes alone.
            logger.info(
                "todo.update_item with extras rejected (%s); retrying", status_code
            )
            dropped = True
            retry_payload = {"entity_id": entity_id, "item": raw_item}
            for key in ("rename", "status"):
                if key in changes:
                    retry_payload[key] = changes[key]
            status_code, detail = await self._post_service(
                session, base, "todo/update_item", retry_payload
            )
        if status_code >= 400:
            logger.warning(
                "todo.update_item failed (%s): %s", status_code, detail[:300]
            )
            return action_failure(
                code="ha_error",
                message=f"Home Assistant rejected the update_item call: {detail[:200]}",
                say_hint=(
                    f"Tell the user {raw_item} could not be updated on the "
                    f"{list_label}."
                ),
            )

        if dropped:
            say_hint = (
                f"Updated {raw_item} on your {list_label}, but the due date "
                "or details couldn't be set."
            )
        else:
            say_hint = f"Updated {raw_item} on your {list_label}."
        logger.info("Updated %r on %s", raw_item, entity_id)
        return action_success(
            facts={"action": "edit", "item": raw_item, "list": list_label},
            say_hint=say_hint,
            summary_for_user=say_hint,
        )

    async def _delete(
        self, session, base, entity_id: str, list_label: str, raw_item: str
    ) -> dict:
        known = await self._known_item_names(session, base, entity_id)
        if raw_item.lower() not in {name.lower() for name in known}:
            return action_failure(
                code="item_not_found",
                message=f"'{raw_item}' is not on {list_label}.",
                say_hint=f"Tell the user {raw_item} isn't on the {list_label}.",
            )
        status, detail = await self._post_service(
            session, base, "todo/remove_item", {"entity_id": entity_id, "item": raw_item}
        )
        if status >= 400:
            logger.warning("todo.remove_item failed (%s): %s", status, detail[:300])
            return action_failure(
                code="ha_error",
                message=f"Home Assistant rejected the remove_item call: {detail[:200]}",
                say_hint=(
                    f"Tell the user {raw_item} could not be removed from the "
                    f"{list_label}."
                ),
            )
        logger.info("Removed %r from %s", raw_item, entity_id)
        return action_success(
            facts={"action": "delete", "item": raw_item, "list": list_label},
            say_hint=f"Removed {raw_item} from your {list_label}.",
            summary_for_user=f"Removed {raw_item} from your {list_label}.",
        )

    async def _show(self, session, base, entity_id: str, list_label: str) -> dict:
        items = await self._fetch_summaries(
            session, base, entity_id, "needs_action"
        )
        if items is None:
            logger.warning("show could not read items from %s", entity_id)
            return action_failure(
                code="ha_error",
                message=f"Home Assistant failed to return the items on {list_label}.",
                say_hint=(
                    f"Tell the user you couldn't retrieve the {list_label}."
                ),
            )
        if not items:
            say_hint = f"Your {list_label} is empty."
        else:
            say_hint = f"Your {list_label} has: {', '.join(items)}."
        logger.info("Showed %d items from %s", len(items), entity_id)
        return action_success(
            facts={"action": "show", "list": list_label, "items": items},
            say_hint=say_hint,
            summary_for_user=say_hint,
        )

    async def _todo_lists(self, session, base) -> list[dict]:
        async with session.get(f"{base}/api/states") as resp:
            resp.raise_for_status()
            states = await resp.json(content_type=None)
        lists = []
        for state in states if isinstance(states, list) else []:
            if not isinstance(state, dict):
                continue
            entity_id = str(state.get("entity_id") or "")
            if not entity_id.startswith(_TODO_ENTITY_PREFIX):
                continue
            if entity_id == _SHOPPING_LIST_ENTITY:
                continue
            attrs = (
                state.get("attributes")
                if isinstance(state.get("attributes"), dict)
                else {}
            )
            friendly = str(attrs.get("friendly_name") or "").strip()
            if not friendly:
                friendly = (
                    entity_id[len(_TODO_ENTITY_PREFIX):].replace("_", " ").strip()
                    or entity_id
                )
            lists.append({"entity_id": entity_id, "friendly_name": friendly})
        lists.sort(key=lambda entry: entry["friendly_name"].lower())
        return lists

    async def _known_item_names(self, session, base, entity_id: str) -> list[str]:
        names = await self._item_summaries(session, base, entity_id, "needs_action")
        names += await self._item_summaries(session, base, entity_id, "completed")
        return names

    async def _item_summaries(
        self, session, base, entity_id: str, status: str
    ) -> list[str]:
        """Item summaries with the given status; empty list on failure."""
        names = await self._fetch_summaries(session, base, entity_id, status)
        return names if names is not None else []

    async def _fetch_summaries(
        self, session, base, entity_id: str, status: str
    ) -> list[str] | None:
        """Item summaries with the given status; None when HA can't answer."""
        url = f"{base}/api/services/todo/get_items?return_response"
        async with session.post(
            url, json={"entity_id": entity_id, "status": status}
        ) as resp:
            if resp.status >= 400:
                logger.warning(
                    "todo.get_items failed (%s) for %s", resp.status, entity_id
                )
                return None
            payload = await resp.json(content_type=None)
        response_data = (
            payload.get("service_response") if isinstance(payload, dict) else None
        )
        items = (
            response_data.get("items") if isinstance(response_data, dict) else response_data
        )
        if not isinstance(items, list):
            return None
        names = []
        for entry in items:
            if isinstance(entry, dict):
                names.append(str(entry.get("summary") or "").strip())
        return [name for name in names if name]

    async def _post_service(
        self, session, base, service: str, payload: dict
    ) -> tuple[int, str]:
        async with session.post(
            f"{base}/api/services/{service}", json=payload
        ) as resp:
            if resp.status >= 400:
                return resp.status, await resp.text()
            return resp.status, ""


verba = HaListsVerba()
