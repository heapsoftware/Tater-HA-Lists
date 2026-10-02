#!/usr/bin/env python3
"""Standalone tests for the ha_lists Verba (spec.md section 8).

Loads the Verba from this repo's verba/ directory and the Tater framework
from the Tater clone outside this repo (sibling Tater/ folder by default,
override with TATER_PATH). HA is faked with a local aiohttp server — no
network, no real HA, no pytest.

Run:  python3 scripts/test_ha_lists.py   (exits non-zero on any failure)
"""

import asyncio
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# The Tater clone lives outside this repo (sibling folder by default;
# override with TATER_PATH).
TATER_ROOT = Path(os.environ.get("TATER_PATH") or (PROJECT_ROOT.parent / "Tater"))
if not TATER_ROOT.exists():
    print(f"Tater clone not found at {TATER_ROOT} (set TATER_PATH)")
    sys.exit(1)
sys.path.insert(0, str(TATER_ROOT))

from aiohttp import web  # noqa: E402

from tateros import integration_store as integration_store  # noqa: E402
from verba_kernel import verba_supports_platform  # noqa: E402
from verba_loader import load_verbas_from_directory  # noqa: E402

TOKEN = "test-token"
SHOPPING = "todo.shopping_list"


class FakeHA:
    """Minimal fake of the HA REST endpoints the verba uses."""

    def __init__(self):
        self.shopping_enabled = True
        # entity_id -> list of item dicts (summary/status/...)
        self.items = {
            SHOPPING: [],
            "todo.errands": [],
            "todo.chores": [],
            "todo.costco": [],
        }
        self.todo_lists = [
            ("todo.errands", "Errands"),
            ("todo.chores", "Chores"),
            ("todo.costco", "Costco"),
        ]
        self.reject_extras = set()  # entities that reject due_date/description
        self.break_get_items = False  # when true, get_items returns 500
        self.add_calls = []
        self.update_calls = []
        self.remove_calls = []
        self.app = web.Application()
        self.app.router.add_get("/api/states", self._states)
        self.app.router.add_get("/api/states/{entity_id}", self._state)
        self.app.router.add_post("/api/services/todo/get_items", self._get_items)
        self.app.router.add_post("/api/services/todo/add_item", self._add_item)
        self.app.router.add_post("/api/services/todo/update_item", self._update_item)
        self.app.router.add_post("/api/services/todo/remove_item", self._remove_item)
        self._runner = None
        self.base_url = ""

    def _auth_ok(self, request):
        return request.headers.get("Authorization") == f"Bearer {TOKEN}"

    async def _states(self, request):
        if not self._auth_ok(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        out = []
        if self.shopping_enabled:
            out.append({
                "entity_id": SHOPPING,
                "state": "idle",
                "attributes": {"friendly_name": "Shopping List"},
            })
        for entity_id, friendly in self.todo_lists:
            out.append({
                "entity_id": entity_id,
                "state": "idle",
                "attributes": {"friendly_name": friendly},
            })
        return web.json_response(out)

    async def _state(self, request):
        if not self._auth_ok(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        entity_id = request.match_info["entity_id"]
        if entity_id == SHOPPING and self.shopping_enabled:
            return web.json_response({"entity_id": entity_id, "state": "idle"})
        return web.json_response({"error": "not found"}, status=404)

    def _entity_exists(self, entity_id):
        if entity_id == SHOPPING:
            return self.shopping_enabled
        return entity_id in self.items

    async def _get_items(self, request):
        if not self._auth_ok(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        if self.break_get_items:
            return web.json_response({"error": "boom"}, status=500)
        if "return_response" not in request.query:
            return web.json_response({"error": "return_response required"}, status=400)
        data = await request.json()
        entity_id = data.get("entity_id")
        if not self._entity_exists(entity_id):
            return web.json_response({"error": "unknown entity"}, status=400)
        wanted = data.get("status", "needs_action")
        items = [
            dict(item)
            for item in self.items.get(entity_id, [])
            if item.get("status") == wanted
        ]
        return web.json_response({"changed_states": [], "service_response": {"items": items}})

    async def _add_item(self, request):
        if not self._auth_ok(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        data = await request.json()
        entity_id = data.get("entity_id") or ""
        if not self._entity_exists(entity_id):
            return web.json_response({"error": "unknown entity"}, status=400)
        if (data.get("due_date") or data.get("description")) and entity_id in self.reject_extras:
            return web.json_response({"error": "update_field_not_supported"}, status=400)
        self.add_calls.append(dict(data))
        self.items[entity_id].append({"summary": data.get("item"), "status": "needs_action"})
        return web.json_response([])

    async def _update_item(self, request):
        if not self._auth_ok(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        data = await request.json()
        entity_id = data.get("entity_id") or ""
        if not self._entity_exists(entity_id):
            return web.json_response({"error": "unknown entity"}, status=400)
        if (data.get("due_date") or data.get("description")) and entity_id in self.reject_extras:
            return web.json_response({"error": "update_field_not_supported"}, status=400)
        matches = [
            item
            for item in self.items.get(entity_id, [])
            if str(item.get("summary", "")).lower() == str(data.get("item", "")).lower()
        ]
        if not matches:
            return web.json_response({"error": "item not found"}, status=400)
        self.update_calls.append(dict(data))
        for item in matches:
            if data.get("rename"):
                item["summary"] = data["rename"]
            if data.get("status"):
                item["status"] = data["status"]
        return web.json_response([])

    async def _remove_item(self, request):
        if not self._auth_ok(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        data = await request.json()
        entity_id = data.get("entity_id") or ""
        if not self._entity_exists(entity_id):
            return web.json_response({"error": "unknown entity"}, status=400)
        kept = [
            item
            for item in self.items.get(entity_id, [])
            if str(item.get("summary", "")).lower() != str(data.get("item", "")).lower()
        ]
        if len(kept) == len(self.items.get(entity_id, [])):
            return web.json_response({"error": "item not found"}, status=400)
        self.remove_calls.append(dict(data))
        self.items[entity_id] = kept
        return web.json_response([])

    async def start(self):
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = None
        try:
            port = int(self._runner.addresses[0].rsplit(":", 1)[1])
        except Exception:
            for sock in site._server.sockets:
                port = sock.getsockname()[1]
                break
        self.base_url = f"http://127.0.0.1:{port}"

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()


class Checks:
    def __init__(self):
        self.failures = []
        self.count = 0

    def check(self, name, condition, detail=""):
        self.count += 1
        if condition:
            print(f"PASS: {name}")
        else:
            self.failures.append(name)
            print(f"FAIL: {name} :: {detail}")

    def finish(self):
        print(f"\n{self.count - len(self.failures)}/{self.count} checks passed")
        return 1 if self.failures else 0


def install_fake_ha_config(base_url):
    integration_store.integration_function = lambda *a, **k: (
        lambda **kw: {"base": base_url, "token": TOKEN}
    )


def install_missing_ha_config():
    integration_store.integration_function = lambda *a, **k: None


def summaries(ha, entity_id):
    return {str(item.get("summary", "")).lower() for item in ha.items.get(entity_id, [])}


async def main() -> int:
    checks = Checks()
    ha = FakeHA()
    await ha.start()
    original = integration_store.integration_function
    try:
        registry = load_verbas_from_directory(str(PROJECT_ROOT / "verba"))
        tool = registry.get("ha_lists")
        checks.check("ha_lists loads from verba/ directory", tool is not None, str(list(registry)))
        if tool is None:
            return checks.finish()

        checks.check(
            "required_settings empty",
            tool.required_settings == {},
        )
        checks.check(
            "voice_core platform supported",
            verba_supports_platform(tool, "voice_core"),
        )
        checks.check(
            "usage parses with correct function id",
            json.loads(tool.usage).get("function") == "ha_lists",
        )
        schema = tool.argument_schema
        checks.check(
            "schema requires action/list_type (item optional for show)",
            set(schema.get("required", [])) == {"action", "list_type"},
            str(schema.get("required")),
        )
        checks.check(
            "action enum covers add/edit/delete/show",
            schema["properties"]["action"]["enum"] == ["add", "edit", "delete", "show"],
        )
        # Hydra only shows the planning LLM the first ~77 chars of
        # when_to_use (tool_purpose truncates at 80) plus the `usage`
        # example — those two strings must carry the steering.
        lead = tool.when_to_use[:77]
        checks.check(
            "when_to_use lead carries show + shopping + named lists",
            "show" in lead and "shopping list" in lead and "named list" in lead,
            lead,
        )
        usage_args = json.loads(tool.usage).get("arguments", {})
        checks.check(
            "usage example anchors the named-list routing",
            usage_args.get("list_type") == "todo"
            and usage_args.get("list_name") == "Costco",
            tool.usage,
        )

        install_fake_ha_config(ha.base_url)

        # --- add: shopping list ---
        result = await tool.handle_webui(
            {"action": "add", "list_type": "shopping", "item": "milk"}, None
        )
        checks.check(
            "shopping add succeeds",
            result.get("ok") is True and "Added milk" in result.get("say_hint", ""),
            str(result)[:200],
        )
        checks.check("exactly one add call", len(ha.add_calls) == 1)
        checks.check("item stored on shopping list", "milk" in summaries(ha, SHOPPING))

        # --- add: duplicate check is case-insensitive ---
        result = await tool.handle_webui(
            {"action": "add", "list_type": "shopping", "item": "MILK"}, None
        )
        checks.check(
            "duplicate detected, not re-added",
            result.get("ok") is True
            and "already" in result.get("say_hint", "").lower()
            and len(ha.add_calls) == 1,
            str(result)[:200],
        )

        # --- add: quantity folding ---
        result = await tool.handle_webui(
            {"action": "add", "list_type": "shopping", "item": "eggs", "quantity": "a dozen"}, None
        )
        checks.check(
            "quantity folded into item name",
            result.get("ok") is True
            and ha.add_calls[-1].get("item") == "eggs (a dozen)",
            str(ha.add_calls[-1]),
        )

        # --- add: missing shopping list entity ---
        ha.shopping_enabled = False
        result = await tool.handle_webui(
            {"action": "add", "list_type": "shopping", "item": "milk"}, None
        )
        checks.check(
            "missing shopping list reported cleanly",
            result.get("ok") is False and "isn't enabled" in result.get("say_hint", ""),
            str(result)[:200],
        )
        ha.shopping_enabled = True

        # --- add: ambiguous todo list asks which ---
        result = await tool.handle_webui(
            {"action": "add", "list_type": "todo", "item": "Call the plumber"}, None
        )
        checks.check(
            "ambiguous list asks which to use",
            result.get("ok") is False
            and "Errands" in result.get("say_hint", "")
            and "list_name" in (result.get("needs") or []),
            str(result)[:300],
        )

        # --- add: fuzzy list match + due date/description ---
        result = await tool.handle_webui(
            {
                "action": "add",
                "list_type": "todo",
                "item": "Call the plumber",
                "list_name": "errand",
                "due_date": "2026-10-05",
                "description": "Ask about the quote",
            },
            None,
        )
        checks.check(
            "fuzzy list match adds task",
            result.get("ok") is True and result.get("facts", {}).get("list") == "Errands",
            str(result)[:200],
        )
        last = ha.add_calls[-1]
        checks.check(
            "due_date and description sent",
            last.get("due_date") == "2026-10-05" and last.get("description") == "Ask about the quote",
            str(last),
        )

        # --- add: feature-gated extras retried without them ---
        ha.reject_extras.add("todo.chores")
        result = await tool.handle_webui(
            {
                "action": "add",
                "list_type": "todo",
                "item": "Fix the gate",
                "list_name": "chores",
                "due_date": "2026-10-06",
            },
            None,
        )
        checks.check(
            "rejected extras retried, still succeeds",
            result.get("ok") is True and "couldn't" in result.get("say_hint", ""),
            str(result)[:300],
        )
        checks.check(
            "retry sent item only",
            ha.add_calls[-1].get("item") == "Fix the gate" and not ha.add_calls[-1].get("due_date"),
            str(ha.add_calls[-1]),
        )

        # --- edit: mark shopping item complete ---
        result = await tool.handle_webui(
            {"action": "edit", "list_type": "shopping", "item": "milk", "status": "completed"}, None
        )
        checks.check(
            "edit marks item complete",
            result.get("ok") is True
            and ha.update_calls[-1].get("status") == "completed",
            str(result)[:200] + str(ha.update_calls[-1]),
        )
        milk_item = next(i for i in ha.items[SHOPPING] if i["summary"] == "milk")
        checks.check("edit applied to stored item", milk_item["status"] == "completed")

        # --- edit: rename on a todo list ---
        result = await tool.handle_webui(
            {
                "action": "edit",
                "list_type": "todo",
                "list_name": "errands",
                "item": "Call the plumber",
                "rename": "Call the plumber about the quote",
            },
            None,
        )
        checks.check(
            "edit renames todo item",
            result.get("ok") is True
            and ha.update_calls[-1].get("rename") == "Call the plumber about the quote",
            str(result)[:200],
        )

        # --- edit: feature-gated extras retried ---
        result = await tool.handle_webui(
            {
                "action": "edit",
                "list_type": "todo",
                "list_name": "chores",
                "item": "Fix the gate",
                "due_date": "2026-10-06",
            },
            None,
        )
        checks.check(
            "edit drops rejected extras and succeeds",
            result.get("ok") is True and "couldn't" in result.get("say_hint", ""),
            str(result)[:300],
        )
        checks.check(
            "edit retry sent no extras",
            not ha.update_calls[-1].get("due_date"),
            str(ha.update_calls[-1]),
        )

        # --- edit: nothing to update / item not found ---
        result = await tool.handle_webui(
            {"action": "edit", "list_type": "shopping", "item": "milk"}, None
        )
        checks.check(
            "edit without changes fails cleanly",
            result.get("ok") is False and result.get("error", {}).get("code") == "nothing_to_update",
            str(result)[:200],
        )
        result = await tool.handle_webui(
            {"action": "edit", "list_type": "shopping", "item": "carrot", "rename": "x"}, None
        )
        checks.check(
            "editing a missing item fails cleanly",
            result.get("ok") is False and result.get("error", {}).get("code") == "item_not_found",
            str(result)[:200],
        )

        # --- delete ---
        result = await tool.handle_webui(
            {"action": "delete", "list_type": "shopping", "item": "eggs (a dozen)"}, None
        )
        checks.check(
            "delete removes shopping item",
            result.get("ok") is True
            and len(ha.remove_calls) == 1
            and "eggs (a dozen)" not in summaries(ha, SHOPPING),
            str(result)[:200],
        )
        result = await tool.handle_webui(
            {"action": "delete", "list_type": "todo", "list_name": "chores", "item": "Fix the gate"}, None
        )
        checks.check(
            "delete removes todo item",
            result.get("ok") is True and "fix the gate" not in summaries(ha, "todo.chores"),
            str(result)[:200],
        )
        result = await tool.handle_webui(
            {"action": "delete", "list_type": "shopping", "item": "carrot"}, None
        )
        checks.check(
            "deleting a missing item fails cleanly",
            result.get("ok") is False and result.get("error", {}).get("code") == "item_not_found",
            str(result)[:200],
        )

        # --- show: read items back aloud ---
        ha.items[SHOPPING].append({"summary": "apples", "status": "needs_action"})
        result = await tool.handle_webui({"action": "show", "list_type": "shopping"}, None)
        checks.check(
            "show reads shopping items without an item arg",
            result.get("ok") is True
            and "apples" in result.get("say_hint", "")
            and result.get("facts", {}).get("items") == ["apples"],
            str(result)[:200],
        )
        result = await tool.handle_webui(
            {"action": "show", "list_type": "todo", "list_name": "errands"}, None
        )
        checks.check(
            "show reads todo items",
            result.get("ok") is True
            and "Call the plumber" in result.get("say_hint", ""),
            str(result)[:300],
        )
        result = await tool.handle_webui(
            {"action": "show", "list_type": "todo", "list_name": "costco"}, None
        )
        checks.check(
            "show reports an empty list",
            result.get("ok") is True and "empty" in result.get("say_hint", "").lower(),
            str(result)[:200],
        )
        result = await tool.handle_webui({"action": "show", "list_type": "todo"}, None)
        checks.check(
            "show without list name asks which list",
            result.get("ok") is False
            and "list_name" in (result.get("needs") or []),
            str(result)[:300],
        )
        ha.break_get_items = True
        result = await tool.handle_webui({"action": "show", "list_type": "shopping"}, None)
        checks.check(
            "show with failed get_items fails cleanly (not 'empty')",
            result.get("ok") is False and result.get("error", {}).get("code") == "ha_error",
            str(result)[:200],
        )
        ha.break_get_items = False

        # --- store-named lists route away from the shopping list ---
        result = await tool.handle_webui(
            {"action": "add", "list_type": "shopping", "item": "pickles", "list_name": "Costco"},
            None,
        )
        checks.check(
            "store-named list routes to the todo list",
            result.get("ok") is True and result.get("facts", {}).get("list") == "Costco",
            str(result)[:200],
        )
        checks.check(
            "pickles not on the shopping list",
            "pickles" not in summaries(ha, SHOPPING),
            str(summaries(ha, SHOPPING)),
        )
        result = await tool.handle_webui(
            {"action": "add", "list_type": "shopping", "item": "soda", "list_name": "the grocery list"},
            None,
        )
        checks.check(
            "shopping-synonym list_name stays on the shopping list",
            result.get("ok") is True and "soda" in summaries(ha, SHOPPING),
            str(result)[:200],
        )

        # --- validation ---
        result = await tool.handle_webui({"action": "fetch", "item": "x", "list_type": "shopping"}, None)
        checks.check(
            "unknown action fails cleanly",
            result.get("ok") is False and result.get("error", {}).get("code") == "unknown_action",
            str(result)[:200],
        )
        checks.check(
            "unknown action message lists valid actions",
            "add, edit, delete, show" in result.get("error", {}).get("message", ""),
            str(result)[:200],
        )
        result = await tool.handle_webui({"action": "add", "item": "x"}, None)
        checks.check(
            "missing list_type fails cleanly",
            result.get("ok") is False and result.get("error", {}).get("code") == "missing_list_type",
            str(result)[:200],
        )
        result = await tool.handle_webui({"action": "add", "list_type": "shopping", "item": "  "}, None)
        checks.check(
            "blank item fails cleanly",
            result.get("ok") is False and result.get("error", {}).get("code") == "missing_item",
            str(result)[:200],
        )
        result = await tool.handle_webui(
            {"action": "add", "item": "milk", "list_name": "the grocery list"}, None
        )
        checks.check(
            "list_type inferred from shopping-ish list_name",
            result.get("ok") is True and "milk" in summaries(ha, SHOPPING),
            str(result)[:200],
        )

        # --- voice platform dispatch ---
        result = await tool.handle_voice_core(
            {"action": "add", "list_type": "shopping", "item": "bananas"}, None
        )
        checks.check("voice_core dispatch works", result.get("ok") is True, str(result)[:200])

        # --- offline / missing integration ---
        install_fake_ha_config("http://127.0.0.1:1")
        result = await tool.handle_webui(
            {"action": "add", "list_type": "shopping", "item": "milk"}, None
        )
        checks.check(
            "unreachable HA fails cleanly",
            result.get("ok") is False
            and "couldn't reach home assistant" in result.get("say_hint", "").lower(),
            str(result)[:200],
        )
        install_missing_ha_config()
        result = await tool.handle_webui(
            {"action": "add", "list_type": "shopping", "item": "milk"}, None
        )
        checks.check(
            "missing integration fails cleanly",
            result.get("ok") is False
            and "couldn't reach home assistant" in result.get("say_hint", "").lower(),
            str(result)[:200],
        )
    finally:
        integration_store.integration_function = original
        await ha.stop()
    return checks.finish()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
