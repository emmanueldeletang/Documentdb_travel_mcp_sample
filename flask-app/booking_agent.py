"""
Booking Assistant — a stateful, action-taking agent over the DocumentDB MCP server.

Unlike the single-turn Q&A in llm.py, this agent holds a multi-turn conversation
and can WRITE to traveldb (create reservations). Every write tool call is gated:
the LLM may read freely, but any insert/update/delete is paused, surfaced to the
user as a proposed action, and only executed after explicit confirmation.

Wire it into app.py with one line (after your MCP client exists):

    from booking_agent import init_booking
    init_booking(app, mcp)        # mcp = your existing MCPClient instance
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, jsonify, render_template, request
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

# ---- write/management tool detection ---------------------------------------
# We can't hardcode the server's exact tool names, so we detect writes by keyword.
# Override with env WRITE_TOOLS / READ_TOOLS (comma-separated) to pin exact names.
_WRITE_KEYWORDS = ("insert", "update", "delete", "replace", "create", "drop",
                   "rename", "bulk", "write")
_WRITE_OVERRIDE = {t.strip() for t in os.environ.get("WRITE_TOOLS", "").split(",") if t.strip()}
_READ_OVERRIDE = {t.strip() for t in os.environ.get("READ_TOOLS", "").split(",") if t.strip()}


def is_write_tool(name: str) -> bool:
    if name in _READ_OVERRIDE:
        return False
    if name in _WRITE_OVERRIDE:
        return True
    n = name.lower()
    return any(k in n for k in _WRITE_KEYWORDS)


SYSTEM_PROMPT = """You are a Booking Assistant for the traveldb travel database.

Collections and key fields (all snake_case):
- customers   : _id ("CUST-NNNN"), name, email, country, loyalty_tier, loyalty_points
- destinations: _id ("DEST-XXX"),  city, country, region, avg_nightly_rate, rating, tags
- flights     : _id ("FL-XXXNN"),  origin, destination_id, departure, price_per_person
- reservations: _id ("RES-NNNNN"), customer_id, customer_name, destination_id,
                destination_city, flight_id, status, booking_date, check_in, check_out,
                nights, travelers, room_total, flight_total, total_price, currency

Tool call rules — ALWAYS include BOTH:
  connection_profile = "default"
  database           = "traveldb"

Booking workflow:
  1. find_documents collection="customers"    filter={"name": "<Full Name>"}
  2. find_documents collection="destinations" filter={"city": "<City>"}
  3. (optional) find_documents collection="flights" filter={"destination_id": "<DEST-xxx>"}
  4. Compute:
       room_total   = avg_nightly_rate × nights
       flight_total = price_per_person × travelers  (0 if no flight)
       total_price  = room_total + flight_total
  5. Build and propose the reservation document with these exact field names:
       _id, customer_id, customer_name, destination_id, destination_city,
       flight_id (omit if none), status="confirmed", booking_date (today ISO),
       check_in, check_out (= check_in + nights), nights, travelers,
       room_total, flight_total, total_price, currency="EUR"

Read tools (find/count/aggregate/sample) run automatically.
Write tools (insert/update/delete) REQUIRE explicit user confirmation — surface the
exact document first, never mark a booking complete before the write succeeds.
If a tool returns an error, explain it plainly and suggest a fix. Never blame permissions
unless the error text explicitly says "Unauthorized" or "not authorized".
"""


# ---- LLM client (Azure OpenAI or OpenAI), same env vars as the README ------
def make_llm():
    if os.environ.get("AZURE_OPENAI_API_KEY"):
        from openai import AzureOpenAI
        client = AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        )
        return client, os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    if os.environ.get("OPENAI_API_KEY"):
        from openai import OpenAI
        return OpenAI(), os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    return None, None  # booking needs an LLM; routes report this cleanly


def _safe_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}


class _DirectCrud:
    """Direct MongoDB CRUD executor used after user confirmation."""

    def __init__(self, uri: str, default_db: str = "traveldb") -> None:
        self._uri = uri
        self._default_db = default_db
        self._client: MongoClient | None = None
        self._lock = threading.Lock()

    def _client_or_create(self) -> MongoClient:
        if self._client is None:
            self._client = MongoClient(self._uri, serverSelectionTimeoutMS=5000)
        return self._client

    @staticmethod
    def _as_object(value: Any) -> Any:
        if isinstance(value, str):
            s = value.strip()
            if s.startswith("{") or s.startswith("["):
                try:
                    return json.loads(s)
                except ValueError:
                    return value
        return value

    @classmethod
    def _as_dict(cls, value: Any) -> dict[str, Any] | None:
        parsed = cls._as_object(value)
        return parsed if isinstance(parsed, dict) else None

    @classmethod
    def _as_list(cls, value: Any) -> list[Any] | None:
        parsed = cls._as_object(value)
        return parsed if isinstance(parsed, list) else None

    @staticmethod
    def _collection_name(args: dict[str, Any]) -> str:
        for key in ("collection", "collection_name"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise ValueError("Missing collection/collection_name in tool arguments.")

    @classmethod
    def _insert_docs(cls, args: dict[str, Any]) -> list[dict[str, Any]]:
        # Try common list payload keys
        for key in ("documents", "docs", "items", "records"):
            items = cls._as_list(args.get(key))
            if not items:
                continue
            docs: list[dict[str, Any]] = []
            for item in items:
                as_dict = cls._as_dict(item)
                if as_dict is not None:
                    docs.append(as_dict)
            if docs:
                return docs

        # Try common single doc keys
        for key in ("document", "doc", "payload", "data"):
            as_dict = cls._as_dict(args.get(key))
            if as_dict is not None:
                return [as_dict]

        # Fallback: search all keys for ANY dict or list of dicts
        for key, value in args.items():
            items = cls._as_list(value)
            if items:
                docs: list[dict[str, Any]] = []
                for item in items:
                    as_dict = cls._as_dict(item)
                    if as_dict is not None:
                        docs.append(as_dict)
                if docs:
                    return docs
            
            as_dict = cls._as_dict(value)
            if as_dict is not None:
                return [as_dict]

        available_keys = ", ".join(repr(k) for k in args.keys()) if args else "(empty)"
        raise ValueError(f"Missing document/documents payload for insert. Available keys: {available_keys}")

    @classmethod
    def _filter(cls, args: dict[str, Any]) -> dict[str, Any]:
        for key in ("filter", "query"):
            value = cls._as_dict(args.get(key))
            if value is not None:
                return value
        raise ValueError("Missing filter/query payload.")

    @classmethod
    def _update_doc(cls, args: dict[str, Any]) -> dict[str, Any]:
        for key in ("update", "update_doc", "update_document", "payload", "data"):
            value = cls._as_dict(args.get(key))
            if value is not None:
                return value
        raise ValueError("Missing update payload.")

    @staticmethod
    def _id_exists(collection, doc_id: str) -> bool:
        return collection.find_one({"_id": doc_id}, {"_id": 1}) is not None

    @classmethod
    def _next_reservation_id(cls, collection) -> str:
        max_num = 0
        max_width = 5
        for row in collection.find({"_id": {"$regex": r"^RES-[0-9]+$"}}, {"_id": 1}):
            raw_id = row.get("_id")
            if not isinstance(raw_id, str):
                continue
            match = re.match(r"^RES-([0-9]+)$", raw_id)
            if not match:
                continue
            digits = match.group(1)
            max_width = max(max_width, len(digits))
            num = int(digits)
            if num > max_num:
                max_num = num
        return f"RES-{str(max_num + 1).zfill(max_width)}"

    @classmethod
    def _next_generic_id(cls, base_id: str, used_ids: set[str]) -> str:
        # Preserve readability by adding an incrementing suffix for non-RES IDs.
        i = 1
        while True:
            candidate = f"{base_id}-{i}"
            if candidate not in used_ids:
                return candidate
            i += 1

    @classmethod
    def _resolve_insert_ids(
        cls,
        collection,
        docs: list[dict[str, Any]],
    ) -> list[str]:
        changes: list[str] = []
        used_ids: set[str] = set()

        for doc in docs:
            raw_id = doc.get("_id")
            if not isinstance(raw_id, str) or not raw_id.strip():
                continue
            doc_id = raw_id.strip()
            doc["_id"] = doc_id

            conflict = doc_id in used_ids or cls._id_exists(collection, doc_id)
            if not conflict:
                used_ids.add(doc_id)
                continue

            if doc_id.startswith("RES-"):
                new_id = cls._next_reservation_id(collection)
                while new_id in used_ids or cls._id_exists(collection, new_id):
                    new_id = cls._next_reservation_id(collection)
            else:
                new_id = cls._next_generic_id(doc_id, used_ids)
                while cls._id_exists(collection, new_id):
                    used_ids.add(new_id)
                    new_id = cls._next_generic_id(doc_id, used_ids)

            doc["_id"] = new_id
            used_ids.add(new_id)
            changes.append(f"{doc_id} -> {new_id}")

        return changes

    def execute(self, tool_name: str, args: dict[str, Any], default_db: str) -> str:
        name = (tool_name or "").lower()
        db_name = str(args.get("database") or args.get("db") or default_db or self._default_db)
        col_name = self._collection_name(args)

        with self._lock:
            client = self._client_or_create()
            collection = client[db_name][col_name]

            if "insert" in name or "create" in name:
                docs = self._insert_docs(args)

                id_changes = self._resolve_insert_ids(collection, docs)

                try:
                    if len(docs) == 1:
                        result = collection.insert_one(docs[0])
                        suffix = f"; remapped_ids={', '.join(id_changes)}" if id_changes else ""
                        return (
                            f"direct insert into {db_name}.{col_name}: inserted_id={result.inserted_id}"
                            f"{suffix}"
                        )
                    result = collection.insert_many(docs)
                    suffix = f"; remapped_ids={', '.join(id_changes)}" if id_changes else ""
                    return (
                        f"direct insert into {db_name}.{col_name}: inserted_count={len(result.inserted_ids)}"
                        f"{suffix}"
                    )
                except DuplicateKeyError as exc:
                    raise ValueError(
                        "Duplicate key detected while inserting. "
                        "Use a unique _id or send an update/replace request for existing records. "
                        f"Details: {exc}"
                    ) from exc

            if "update" in name or "replace" in name:
                filt = self._filter(args)
                is_many = bool(args.get("many")) or "many" in name
                if "replace" in name:
                    replacement = self._as_dict(args.get("replacement"))
                    if replacement is None:
                        replacement = self._as_dict(args.get("document"))
                    if replacement is None:
                        raise ValueError("Missing replacement/document payload for replace.")
                    if is_many:
                        raise ValueError("replace_many is not supported; use update with $set.")
                    out = collection.replace_one(filt, replacement, upsert=bool(args.get("upsert")))
                    return (
                        f"direct replace in {db_name}.{col_name}: "
                        f"matched={out.matched_count}, modified={out.modified_count}, upserted_id={out.upserted_id}"
                    )

                update_doc = self._update_doc(args)
                upsert = bool(args.get("upsert"))
                if is_many:
                    out = collection.update_many(filt, update_doc, upsert=upsert)
                else:
                    out = collection.update_one(filt, update_doc, upsert=upsert)
                return (
                    f"direct update in {db_name}.{col_name}: "
                    f"matched={out.matched_count}, modified={out.modified_count}, upserted_id={out.upserted_id}"
                )

            if "delete" in name or "drop" in name:
                filt = self._filter(args)
                is_many = bool(args.get("many")) or "many" in name
                if is_many:
                    out = collection.delete_many(filt)
                else:
                    out = collection.delete_one(filt)
                return f"direct delete in {db_name}.{col_name}: deleted_count={out.deleted_count}"

        raise ValueError(f"Unsupported direct CRUD tool: {tool_name}")


class _BookingHistory:
    """Persists booking conversation events into traveldb.bookinghisto."""

    def __init__(self, uri: str, db_name: str = "traveldb", collection_name: str = "bookinghisto") -> None:
        self._uri = uri
        self._db_name = db_name
        self._collection_name = collection_name
        self._client: MongoClient | None = None
        self._collection = None
        self._lock = threading.Lock()

    def _get_collection(self):
        if self._collection is not None:
            return self._collection
        self._client = MongoClient(self._uri, serverSelectionTimeoutMS=5000)
        col = self._client[self._db_name][self._collection_name]
        try:
            col.create_index("conversation_id")
        except Exception:
            pass
        try:
            col.create_index("created_at")
        except Exception:
            pass
        self._collection = col
        return col

    def store_event(
        self,
        conversation_id: str,
        event_type: str,
        request_message: str | None,
        result: dict[str, Any],
        pending_actions: list[dict[str, Any]] | None,
    ) -> None:
        with self._lock:
            col = self._get_collection()
            doc = {
                "conversation_id": conversation_id,
                "event_type": event_type,
                "request_message": request_message,
                "status": result.get("status"),
                "reply": result.get("reply"),
                "actions": result.get("actions") or pending_actions or [],
                "trace": result.get("trace") or [],
                "created_at": datetime.now(timezone.utc),
            }
            col.insert_one(doc)


def _is_direct_crud_tool(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in ("insert", "create", "update", "replace", "delete", "drop"))


class _Conversation:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.pending: list[dict] | None = None   # tool_calls awaiting confirmation
        self.trace: list[dict] = []
        self.lock = threading.Lock()


class BookingAgent:
    """Reuses the existing synchronous MCPClient (list_tools/call_tool)."""

    def __init__(self, mcp, llm, model, max_steps: int = 8) -> None:
        self.mcp = mcp
        self.llm = llm
        self.model = model
        self.max_steps = max_steps
        self._tools_cache: list[dict] | None = None
        self._store: dict[str, _Conversation] = {}
        self._store_lock = threading.Lock()
        self._direct_crud = _DirectCrud(os.environ.get("DOCUMENTDB_URI", ""), default_db="traveldb")
        self._history = _BookingHistory(
            os.environ.get("DOCUMENTDB_URI", ""),
            db_name=os.environ.get("BOOKING_HISTORY_DB", "traveldb"),
            collection_name=os.environ.get("BOOKING_HISTORY_COLLECTION", "bookinghisto"),
        )

    def _store_history(
        self,
        conversation_id: str,
        event_type: str,
        request_message: str | None,
        result: dict[str, Any],
        pending_actions: list[dict[str, Any]] | None = None,
    ) -> None:
        try:
            self._history.store_event(conversation_id, event_type, request_message, result, pending_actions)
        except Exception:
            # History persistence is best-effort and must never break booking flow.
            pass

    # ---- tool plumbing ------------------------------------------------------
    def _tools(self) -> list[dict]:
        if self._tools_cache is None:
            self._tools_cache = [
                {"type": "function", "function": {
                    "name": t["name"],
                    "description": (t.get("description") or "")[:1024],
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                }}
                for t in self.mcp.list_tools()
            ]
        return self._tools_cache

    def _exec(self, conv: _Conversation, call_id: str, name: str, args: dict) -> None:
        args = dict(args)
        args.setdefault("connection_profile", "default")
        args.setdefault("database", "traveldb")
        try:
            result = self.mcp.call_tool(name, args)
            text = result.get("text") or ""
            if result.get("is_error"):
                content = f"TOOL ERROR from {name}: {text}"
            else:
                content = text[:6000] if text else "(empty result)"
        except Exception as exc:
            content = f"TOOL ERROR from {name}: {type(exc).__name__}: {exc}"
        conv.trace.append({"tool": name, "arguments": args, "result": content[:1000]})
        conv.messages.append({"role": "tool", "tool_call_id": call_id, "content": content})

    # ---- agent loop ---------------------------------------------------------
    def _run_loop(self, conv: _Conversation) -> dict:
        if self.llm is None:
            return {"status": "error",
                    "reply": "Booking needs an LLM. Set AZURE_OPENAI_API_KEY or OPENAI_API_KEY.",
                    "trace": conv.trace}
        for _ in range(self.max_steps):
            resp = self.llm.chat.completions.create(
                model=self.model, messages=conv.messages,
                tools=self._tools(), tool_choice="auto", temperature=0,
            )
            msg = resp.choices[0].message
            conv.messages.append(msg.model_dump(exclude_none=True))

            if not msg.tool_calls:
                return {"status": "reply", "reply": msg.content or "", "trace": conv.trace}

            # Gate: if this batch contains ANY write tool, pause for confirmation.
            if any(is_write_tool(c.function.name) for c in msg.tool_calls):
                conv.pending = [c.model_dump() for c in msg.tool_calls]
                actions = [{"tool": c.function.name,
                            "arguments": _safe_args(c.function.arguments)}
                           for c in msg.tool_calls]
                return {"status": "confirm", "actions": actions, "trace": conv.trace}

            # Otherwise these are reads — execute and continue planning.
            for c in msg.tool_calls:
                self._exec(conv, c.id, c.function.name, _safe_args(c.function.arguments))

        return {"status": "reply", "reply": "(stopped: reached step limit)", "trace": conv.trace}

    # ---- public API ---------------------------------------------------------
    def _get(self, cid: str) -> _Conversation | None:
        with self._store_lock:
            return self._store.get(cid)

    def start(self, message: str) -> dict:
        cid = uuid.uuid4().hex
        conv = _Conversation()
        conv.messages = [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": message}]
        with self._store_lock:
            self._store[cid] = conv
        with conv.lock:
            out = self._run_loop(conv)
        out["conversation_id"] = cid
        self._store_history(cid, "start", message, out, conv.pending)
        return out

    def send(self, cid: str, message: str) -> dict:
        conv = self._get(cid)
        if conv is None:
            return self.start(message)
        with conv.lock:
            if conv.pending:
                actions = [{"tool": c["function"]["name"],
                            "arguments": _safe_args(c["function"]["arguments"])}
                           for c in conv.pending]
                out = {"status": "confirm", "conversation_id": cid, "actions": actions,
                       "reply": "Please confirm or cancel the pending action first.",
                       "trace": conv.trace}
                self._store_history(cid, "send-blocked-pending", message, out, conv.pending)
                return out
            conv.messages.append({"role": "user", "content": message})
            out = self._run_loop(conv)
        out["conversation_id"] = cid
        self._store_history(cid, "send", message, out, conv.pending)
        return out

    def confirm(self, cid: str, approved: bool) -> dict:
        conv = self._get(cid)
        if conv is None:
            out = {"status": "error", "reply": "Unknown conversation.", "trace": []}
            self._store_history(cid, "confirm-error", None, out, None)
            return out
        with conv.lock:
            if not conv.pending:
                out = {"status": "reply", "conversation_id": cid,
                       "reply": "Nothing to confirm.", "trace": conv.trace}
                self._store_history(cid, "confirm-empty", None, out, None)
                return out
            pending, conv.pending = conv.pending, None

            if not approved:
                for call in pending:
                    name = call["function"]["name"]
                    args = _safe_args(call["function"]["arguments"])
                    conv.messages.append({"role": "tool", "tool_call_id": call["id"],
                                          "content": "User declined this action."})
                    conv.trace.append({"tool": name, "arguments": args, "result": "declined"})
                out = self._run_loop(conv)
                out["conversation_id"] = cid
                self._store_history(cid, "confirm-declined", None, out, pending)
                return out

            # Execute confirmed writes. For CRUD actions, use direct DB operations.
            # For non-CRUD management actions, fall back to MCP tool execution.
            errors: list[str] = []
            successes: list[str] = []
            for call in pending:
                name = call["function"]["name"]
                args = _safe_args(call["function"]["arguments"])
                args_with_defaults = dict(args)
                args_with_defaults.setdefault("connection_profile", "default")
                args_with_defaults.setdefault("database", "traveldb")
                try:
                    if _is_direct_crud_tool(name):
                        text = self._direct_crud.execute(name, args_with_defaults, default_db="traveldb")
                        successes.append(f"`{name}` succeeded: {text}")
                        content = text
                    else:
                        result = self.mcp.call_tool(name, args_with_defaults)
                        text = (result.get("text") or "").strip()
                        if result.get("is_error"):
                            errors.append(f"`{name}` failed: {text}")
                            content = f"TOOL ERROR from {name}: {text}"
                        else:
                            successes.append(f"`{name}` succeeded: {text[:200]}")
                            content = text[:6000] or "(ok)"
                except Exception as exc:
                    msg = f"{type(exc).__name__}: {exc}"
                    errors.append(f"`{name}` raised: {msg}")
                    content = f"TOOL ERROR from {name}: {msg}"
                conv.trace.append({"tool": name, "arguments": args_with_defaults,
                                   "result": content[:1000]})
                conv.messages.append({"role": "tool", "tool_call_id": call["id"],
                                      "content": content})

            # Return a deterministic reply instead of letting the LLM guess from raw results.
            if errors:
                reply = "⚠️ Some operations failed:\n" + "\n".join(f"• {e}" for e in errors)
                if successes:
                    reply += "\n\n✅ These succeeded:\n" + "\n".join(f"• {s}" for s in successes)
            else:
                reply = "✅ " + "; ".join(successes) if successes else "✅ Done."

        out = {"status": "reply", "reply": reply,
               "conversation_id": cid, "trace": conv.trace}
        self._store_history(cid, "confirm-approved", None, out, pending)
        return out


# ---- Flask integration ------------------------------------------------------
def init_booking(app, mcp_client, max_steps: int = 8) -> BookingAgent:
    """Builds the agent and registers its routes. Call once from app.py."""
    llm, model = make_llm()
    agent = BookingAgent(mcp_client, llm, model, max_steps=max_steps)
    bp = Blueprint("booking", __name__)

    @bp.get("/booking")
    def booking_page():
        return render_template("booking.html")

    @bp.post("/api/book")
    def api_book():
        body = request.get_json(force=True) or {}
        message = (body.get("message") or "").strip()
        if not message:
            return jsonify({"status": "error", "reply": "Empty message."}), 400
        cid = body.get("conversation_id")
        return jsonify(agent.send(cid, message) if cid else agent.start(message))

    @bp.post("/api/book/confirm")
    def api_book_confirm():
        body = request.get_json(force=True) or {}
        cid = body.get("conversation_id")
        if not cid:
            return jsonify({"status": "error", "reply": "Missing conversation_id."}), 400
        return jsonify(agent.confirm(cid, bool(body.get("approved"))))

    app.register_blueprint(bp)
    return agent