"""Design pattern detection over call graph, SCIP abstract-method data, and embeddings.

Each detector is async, targets one pattern, and returns [] when the pattern is absent.
All detectors run concurrently via detect_patterns(). Results are only included in
get_function_context output when non-empty — no token cost for pattern-free functions.

Supported patterns (high-confidence set, Phase 1):
  Visitor, Template Method, Factory Method, Observer, Singleton, Strategy, Decorator/Proxy
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class PatternMatch:
    pattern: str
    role: str
    confidence: float
    participants: dict
    missing: list[str] = field(default_factory=list)
    action: str | None = None


# ── Storage protocol ─────────────────────────────────────────────────────────

class PatternStorageProtocol(Protocol):
    """Structural protocol capturing the 7 storage methods used by pattern detectors.

    Any object satisfying these methods can serve as the `db` argument to
    detect_patterns(). CallGraphDB satisfies this; so can test doubles.
    """
    async def get_class_methods(self, class_id: str, project_id: str | None = None) -> list[dict]: ...
    async def find_base_classes(self, class_id: str, project_id: str | None = None) -> list[str]: ...
    async def find_subclasses(self, class_id: str, project_id: str | None = None) -> list[str]: ...
    async def find_sibling_callers(self, node_id: str, class_id: str, project_id: str | None = None) -> list[str]: ...
    async def find_self_delegating_callees(self, caller_id: str, method_name: str, project_id: str | None = None) -> list[str]: ...
    async def get_node_body(self, node_id: str, project_id: str | None = None) -> str: ...
    async def get_node_abstractness(self, node_id: str, project_id: str | None = None) -> bool: ...
    async def find_dispatch_handlers(self, project_id: str, verb: str) -> list[dict]: ...


# ── Shared helpers ────────────────────────────────────────────────────────────

def _decorators(node: dict) -> list[str]:
    raw = node.get("decorators", "[]")
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return []


_RAISE_NIE_RE = re.compile(r"^\s*raise\s+NotImplementedError\b", re.MULTILINE)


def _is_abstract(node: dict) -> bool:
    """Return True if this method is abstract — either by @abstractmethod decorator
    or by a body that raises NotImplementedError as a statement.

    Uses a line-anchored regex rather than a substring check to avoid false
    positives when the text 'raise NotImplementedError' appears inside a string
    literal or a docstring prose description.
    """
    if "abstractmethod" in _decorators(node):
        return True
    body = node.get("body") or ""
    return bool(_RAISE_NIE_RE.search(body))


def _is_pass_only(body: str) -> bool:
    """Return True if the function body contains only `pass` or `...` (no real logic).

    Detects Python's informal abstract-hook idiom:
        def handle(self):
            pass   # subclasses must override

    Algorithm: skip the def line, strip docstrings and comments, then check
    that the only remaining token is 'pass' or '...'.
    """
    if not body:
        return False
    lines = body.split("\n")
    # Locate end of def signature (the colon line)
    sig_end = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("def ") or s.startswith("async def "):
            # Walk forward until we hit the closing colon
            for j in range(i, min(i + 6, len(lines))):
                if lines[j].rstrip().endswith(":"):
                    sig_end = j + 1
                    break
            break

    code: list[str] = []
    in_doc = False
    doc_delim: str | None = None
    for line in lines[sig_end:]:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if not in_doc:
            for delim in ('"""', "'''"):
                if s.startswith(delim):
                    doc_delim = delim
                    rest = s[3:]
                    if delim in rest:
                        doc_delim = None  # closed on same line
                    else:
                        in_doc = True
                    break
            else:
                code.append(s)
        else:
            if doc_delim and doc_delim in s:
                in_doc = False

    return not code or (len(code) == 1 and code[0] in ("pass", "..."))


def _method_name(node_id: str) -> str:
    return node_id.split(".")[-1]


def _class_id(node: dict) -> str | None:
    """Derive enclosing class id from node id. Returns None for top-level functions."""
    if node.get("type") not in ("method",):
        return None
    parts = node["id"].split(".")
    return ".".join(parts[:-1]) if len(parts) >= 2 else None


# ── Visitor ───────────────────────────────────────────────────────────────────

_VISITOR_VERBS = frozenset({
    "print", "visit", "render", "handle", "encode", "decode", "write", "emit",
})
_DISPATCH_RE = re.compile(r"^_?([a-z][a-z0-9]*)_([A-Za-z]\w*)$")
_VISIT_RE    = re.compile(r"^visit_([A-Za-z]\w*)$")


async def _detect_visitor(node: dict, db, project_id: str | None) -> list[PatternMatch]:
    name = _method_name(node["id"])
    class_id = _class_id(node)

    # ── Named-dispatch variant (_verb_TypeName) ───────────────────────────────
    dm = _DISPATCH_RE.match(name)
    if dm and dm.group(1) in _VISITOR_VERBS:
        verb = dm.group(1)
        handlers = await db.find_dispatch_handlers(project_id or "", verb)
        by_class: dict[str, set[str]] = {}
        pfx = f"_{verb}_"
        pfx_bare = f"{verb}_"
        for h in handlers:
            cls = ".".join(h["id"].split(".")[:-1])
            hname = h["name"].lstrip("_")
            if hname.startswith(pfx_bare):
                by_class.setdefault(cls, set()).add(hname[len(pfx_bare):])
        if len(by_class) >= 2 and class_id and class_id in by_class:
            all_elements = set().union(*by_class.values())
            missing = sorted(all_elements - by_class[class_id])
            return [PatternMatch(
                pattern="Visitor",
                role="ConcreteVisitor",
                confidence=0.92,
                participants={
                    "ConcreteVisitor": class_id,
                    "Elements": sorted(all_elements),
                    "SiblingVisitors": [c for c in by_class if c != class_id],
                },
                missing=[f"{pfx}{e}" for e in missing],
                action=(
                    f"add missing `{pfx}*` handlers to `{class_id.split('.')[-1]}`"
                    if missing else
                    "add handler to all visitor classes when adding a new element type"
                ),
            )]

    # ── Classic variant: visit_X → ConcreteVisitor or AbstractVisitor ─────────
    vm = _VISIT_RE.match(name)
    if vm and class_id:
        abstract = _is_abstract(node)
        bases = await db.find_base_classes(class_id, project_id)

        abstract_visitor_id = None
        all_elements: set[str] = set()
        for base_id in bases:
            base_methods = await db.get_class_methods(base_id, project_id)
            abstract_visits = [
                m for m in base_methods
                if _VISIT_RE.match(_method_name(m["id"])) and _is_abstract(m)
            ]
            if abstract_visits:
                abstract_visitor_id = base_id
                all_elements = {
                    _VISIT_RE.match(_method_name(m["id"])).group(1)
                    for m in abstract_visits
                }
                break

        if abstract_visitor_id:
            siblings = await db.find_subclasses(abstract_visitor_id, project_id)
            by_class2: dict[str, set[str]] = {}
            for sid in siblings:
                sib_methods = await db.get_class_methods(sid, project_id)
                by_class2[sid] = {
                    _VISIT_RE.match(_method_name(m["id"])).group(1)
                    for m in sib_methods
                    if _VISIT_RE.match(_method_name(m["id"])) and not _is_abstract(m)
                }
            missing = sorted(all_elements - by_class2.get(class_id, set()))
            return [PatternMatch(
                pattern="Visitor",
                role="AbstractVisitor" if abstract else "ConcreteVisitor",
                confidence=0.90,
                participants={
                    "AbstractVisitor": abstract_visitor_id,
                    "ConcreteVisitor": class_id,
                    "Elements": sorted(all_elements),
                    "SiblingVisitors": [c for c in siblings if c != class_id],
                },
                missing=[f"visit_{e}" for e in missing],
                action=(
                    f"implement missing `visit_*` handlers in `{class_id.split('.')[-1]}`"
                    if missing else None
                ),
            )]

    # ── accept() → Element role ───────────────────────────────────────────────
    if name == "accept" and class_id:
        abstract = _is_abstract(node)
        subs = await db.find_subclasses(class_id, project_id) if abstract else []
        missing_accept = []
        for sub_id in subs:
            sub_methods = await db.get_class_methods(sub_id, project_id)
            if not any(_method_name(m["id"]) == "accept" for m in sub_methods):
                missing_accept.append(sub_id.split(".")[-1])
        return [PatternMatch(
            pattern="Visitor",
            role="AbstractElement" if abstract else "ConcreteElement",
            confidence=0.75,
            participants={"Element": class_id, "Subclasses": subs},
            missing=missing_accept,
            action="add `accept(visitor)` to subclasses" if missing_accept else None,
        )]

    return []


# ── Template Method ───────────────────────────────────────────────────────────

async def _detect_template_method(node: dict, db, project_id: str | None) -> list[PatternMatch]:
    class_id = _class_id(node)
    if not class_id:
        return []
    name = _method_name(node["id"])
    body = node.get("body") or ""
    class_prefix = class_id + "."

    # ── Case A: formal abstract hook (@abstractmethod / raise NotImplementedError) ──
    if _is_abstract(node):
        subs = await db.find_subclasses(class_id, project_id)
        missing_impl = []
        for sub_id in subs:
            sub_methods = await db.get_class_methods(sub_id, project_id)
            if not any(
                _method_name(m["id"]) == name and not _is_abstract(m)
                for m in sub_methods
            ):
                missing_impl.append(sub_id.split(".")[-1])
        return [PatternMatch(
            pattern="Template Method",
            role="AbstractHook",
            confidence=0.85,
            participants={"AbstractClass": class_id, "ConcreteSubclasses": subs},
            missing=[f"{c}.{name}" for c in missing_impl],
            action=f"implement `{name}` in subclasses that haven't overridden it" if missing_impl else None,
        )]

    # ── Case B: pass-only hook called from a sibling (Python informal abstract) ─
    # A pass-only method is only a template hook if something in the same class
    # actually calls it. This eliminates simple stubs and no-op methods.
    # Uses find_sibling_callers (not get_callers) because _resolve_callee may
    # have stored the call as a bare name when multiple classes share the method.
    if _is_pass_only(body):
        sibling_callers = await db.find_sibling_callers(node["id"], class_id, project_id)
        if sibling_callers:
            subs = await db.find_subclasses(class_id, project_id)
            missing_impl = []
            for sub_id in subs:
                sub_methods = await db.get_class_methods(sub_id, project_id)
                # A subclass "implements" the hook if it overrides it with non-pass body
                if not any(
                    _method_name(m["id"]) == name and not _is_pass_only(m.get("body") or "")
                    for m in sub_methods
                ):
                    missing_impl.append(sub_id.split(".")[-1])
            return [PatternMatch(
                pattern="Template Method",
                role="AbstractHook",
                confidence=0.75,
                participants={
                    "AbstractClass": class_id,
                    "CalledFrom": sibling_callers[:3],
                    "ConcreteSubclasses": subs,
                },
                missing=[f"{c}.{name}" for c in missing_impl],
                action=f"override `{name}` in subclasses" if missing_impl else None,
            )]

    # ── Case C: this method calls abstract/pass-only siblings → it's the template ─
    callees = await db.get_callees(node["id"], project_id)
    # Fetch all class methods with body once (avoids N separate body lookups)
    class_methods = await db.get_class_methods(class_id, project_id)
    class_body_map = {m["id"]: m for m in class_methods}

    abstract_hooks: list[str] = []
    for callee in callees:
        cid = callee.get("id", "")
        if not cid.startswith(class_prefix):
            continue
        callee_node = class_body_map.get(cid)
        if callee_node and (_is_abstract(callee_node) or _is_pass_only(callee_node.get("body") or "")):
            abstract_hooks.append(cid)
        elif not callee_node and await db.get_node_abstractness(cid, project_id):
            # Callee is in the class but wasn't in get_class_methods (e.g. inherited)
            abstract_hooks.append(cid)

    if not abstract_hooks:
        return []

    subs = await db.find_subclasses(class_id, project_id)
    hook_names = [h.split(".")[-1] for h in abstract_hooks]
    missing_impl = []
    for sub_id in subs:
        sub_methods = await db.get_class_methods(sub_id, project_id)
        sub_names = {_method_name(m["id"]) for m in sub_methods}
        gap = [h for h in hook_names if h not in sub_names]
        if gap:
            missing_impl.append(f"{sub_id.split('.')[-1]}: {gap}")
    return [PatternMatch(
        pattern="Template Method",
        role="TemplateMethod",
        confidence=0.88,
        participants={
            "AbstractClass": class_id,
            "TemplateMethod": node["id"],
            "AbstractHooks": abstract_hooks,
        },
        missing=missing_impl,
        action="implement missing abstract hooks in incomplete subclasses" if missing_impl else None,
    )]


# ── Factory Method ────────────────────────────────────────────────────────────

_FACTORY_RE = re.compile(
    r"^_?(?:create|make|build|factory|produce|spawn|manufacture|new_instance|create_instance|get_or_create)(?:_|$)",
    re.I,
)


async def _detect_factory_method(node: dict, db, project_id: str | None) -> list[PatternMatch]:
    class_id = _class_id(node)
    if not class_id:
        return []
    name = _method_name(node["id"])
    if not _FACTORY_RE.match(name):
        return []

    abstract = _is_abstract(node)
    bases = await db.find_base_classes(class_id, project_id)

    # Check if this overrides an abstract factory method in a base class
    abstract_creator: str | None = None
    for base_id in bases:
        base_methods = await db.get_class_methods(base_id, project_id)
        if any(_method_name(m["id"]) == name and _is_abstract(m) for m in base_methods):
            abstract_creator = base_id
            break

    if abstract:
        subs = await db.find_subclasses(class_id, project_id)
        have, missing = [], []
        for sub_id in subs:
            sub_methods = await db.get_class_methods(sub_id, project_id)
            (have if any(_method_name(m["id"]) == name and not _is_abstract(m) for m in sub_methods) else missing).append(sub_id)
        return [PatternMatch(
            pattern="Factory Method",
            role="AbstractCreator",
            confidence=0.90,
            participants={"AbstractCreator": class_id, "ConcreteCreators": have},
            missing=[s.split(".")[-1] for s in missing],
            action=f"implement `{name}` in concrete subclasses" if missing else None,
        )]

    if abstract_creator:
        return [PatternMatch(
            pattern="Factory Method",
            role="ConcreteCreator",
            confidence=0.88,
            participants={"AbstractCreator": abstract_creator, "ConcreteCreator": class_id},
        )]

    # Non-abstract, no abstract base — only emit if subclasses actually override it
    subs = await db.find_subclasses(class_id, project_id)
    if not subs:
        return []
    overriding = []
    for sub_id in subs:
        sub_methods = await db.get_class_methods(sub_id, project_id)
        if any(_method_name(m["id"]) == name for m in sub_methods):
            overriding.append(sub_id)
    if not overriding:
        return []
    missing = [s.split(".")[-1] for s in subs if s not in overriding]
    return [PatternMatch(
        pattern="Factory Method",
        role="Creator",
        confidence=0.72,
        participants={"Creator": class_id, "ConcreteCreators": overriding},
        missing=missing,
        action=f"override `{name}` in remaining subclasses" if missing else None,
    )]


# ── Observer ──────────────────────────────────────────────────────────────────

_SUBJECT_NOTIFY = frozenset({
    "notify", "notify_observers", "notify_all", "emit", "fire", "trigger", "dispatch", "publish",
})
_SUBJECT_ATTACH = frozenset({
    "attach", "subscribe", "register", "add_listener", "add_observer", "on",
})
_SUBJECT_DETACH = frozenset({
    "detach", "unsubscribe", "deregister", "remove_listener", "remove_observer", "off",
})
_OBSERVER_UPDATE = frozenset({
    "on_update", "on_change", "on_event", "on_notify", "receive", "notify_of",
})


async def _detect_observer(node: dict, db, project_id: str | None) -> list[PatternMatch]:
    class_id = _class_id(node)
    if not class_id:
        return []
    name = _method_name(node["id"])

    is_subject = name in (_SUBJECT_NOTIFY | _SUBJECT_ATTACH | _SUBJECT_DETACH)
    is_observer = name in _OBSERVER_UPDATE
    if not is_subject and not is_observer:
        return []

    class_methods = await db.get_class_methods(class_id, project_id)
    method_names = {_method_name(m["id"]) for m in class_methods}

    if is_subject:
        has_notify = bool(method_names & _SUBJECT_NOTIFY)
        has_attach = bool(method_names & _SUBJECT_ATTACH)
        has_detach = bool(method_names & _SUBJECT_DETACH)
        signal_count = sum([has_notify, has_attach, has_detach])
        if signal_count < 2:
            return []
        missing = (
            (["attach/subscribe method"] if not has_attach else [])
            + (["detach/unsubscribe method"] if not has_detach else [])
            + (["notify/emit method"] if not has_notify else [])
        )
        return [PatternMatch(
            pattern="Observer",
            role="Subject",
            confidence=0.75 + 0.08 * signal_count,
            participants={"Subject": class_id},
            missing=missing,
            action="add missing Subject interface methods" if missing else None,
        )]

    # Observer role — low confidence alone, but useful in context
    return [PatternMatch(
        pattern="Observer",
        role="Observer",
        confidence=0.65,
        participants={"Observer": class_id},
    )]


# ── Singleton ─────────────────────────────────────────────────────────────────

_SINGLETON_ACCESS = frozenset({
    "get_instance", "getInstance", "instance", "shared_instance",
    "get_singleton", "singleton", "get_shared_instance",
})


async def _detect_singleton(node: dict, db, project_id: str | None) -> list[PatternMatch]:
    class_id = _class_id(node)
    if not class_id:
        return []
    name = _method_name(node["id"])
    if name not in _SINGLETON_ACCESS and name not in ("__new__", "__init__"):
        return []

    class_methods = await db.get_class_methods(class_id, project_id)
    method_names = {_method_name(m["id"]) for m in class_methods}
    if not (method_names & _SINGLETON_ACCESS):
        return []

    # Look for class-level _instance field node
    field_found = False
    for variant in (f"{class_id}._instance", f"{class_id}.__instance", f"{class_id}._singleton"):
        if await db.find_node_by_name(variant, project_id):
            field_found = True
            break

    missing = [] if field_found else ["class-level `_instance` field not found in index"]
    return [PatternMatch(
        pattern="Singleton",
        role="SingletonAccessor" if name in _SINGLETON_ACCESS else "SingletonGuard",
        confidence=0.80,
        participants={"Singleton": class_id, "AccessorMethod": node["id"]},
        missing=missing,
        action="ensure `_instance` class variable and thread-safe guard are present" if missing else None,
    )]


# ── Strategy ──────────────────────────────────────────────────────────────────

_STRATEGY_VERBS = frozenset({
    "execute", "run", "apply", "sort", "compress", "encode", "process",
    "compute", "calculate", "perform", "evaluate", "compare", "select",
    "format", "transform", "serialize", "validate", "render", "emit",
})


async def _detect_strategy(node: dict, db, project_id: str | None) -> list[PatternMatch]:
    class_id = _class_id(node)
    if not class_id:
        return []
    name = _method_name(node["id"])
    abstract = _is_abstract(node)

    if not abstract and name not in _STRATEGY_VERBS:
        return []

    if abstract and name in _STRATEGY_VERBS:
        # Candidate AbstractStrategy: abstract class with ≤3 abstract methods
        class_methods = await db.get_class_methods(class_id, project_id)
        abstract_methods = [m for m in class_methods if _is_abstract(m)]
        if len(abstract_methods) > 3:
            return []
        subs = await db.find_subclasses(class_id, project_id)
        missing = []
        for sub_id in subs:
            sub_methods = await db.get_class_methods(sub_id, project_id)
            if not any(_method_name(m["id"]) == name for m in sub_methods):
                missing.append(sub_id.split(".")[-1])
        return [PatternMatch(
            pattern="Strategy",
            role="AbstractStrategy",
            confidence=0.78,
            participants={"AbstractStrategy": class_id, "ConcreteStrategies": subs},
            missing=missing,
            action=f"implement `{name}` in concrete strategy subclasses" if missing else None,
        )]

    if not abstract and name in _STRATEGY_VERBS:
        # Check if this overrides an abstract strategy method
        bases = await db.find_base_classes(class_id, project_id)
        for base_id in bases:
            base_methods = await db.get_class_methods(base_id, project_id)
            if any(_method_name(m["id"]) == name and _is_abstract(m) for m in base_methods):
                return [PatternMatch(
                    pattern="Strategy",
                    role="ConcreteStrategy",
                    confidence=0.82,
                    participants={"AbstractStrategy": base_id, "ConcreteStrategy": class_id},
                )]

    return []


# ── Chain of Responsibility ───────────────────────────────────────────────────

_COR_SUCCESSOR_NAMES = frozenset({
    "_next", "next", "_successor", "successor",
    "next_handler", "_next_handler", "next_link", "_chain",
})

_COMPOSITE_CHILD_NAMES = frozenset({
    "child", "children", "item", "items", "element", "elements",
    "node", "nodes", "component", "components", "part", "parts",
    "member", "members", "leaf", "subtree",
})


async def _detect_cor(node: dict, db, project_id: str | None) -> list[PatternMatch]:
    name = _method_name(node["id"])
    class_id = _class_id(node)
    if not class_id:
        return []

    delegating = await db.find_self_delegating_callees(node["id"], name, project_id)

    # Keep only callees that reference a known successor field name
    # e.g. "self._next.handle" → parts contain "_next"
    successor_hits = [
        d for d in delegating
        if any(part in _COR_SUCCESSOR_NAMES for part in d.replace("self.", "").split("."))
    ]
    if not successor_hits:
        return []

    abstract = _is_abstract(node) or _is_pass_only(node.get("body") or "")
    bases = await db.find_base_classes(class_id, project_id)
    role = "Handler" if (abstract or bases) else "ConcreteHandler"

    return [PatternMatch(
        pattern="Chain of Responsibility",
        role=role,
        confidence=0.85,
        participants={
            "Handler": class_id,
            "SuccessorCall": successor_hits,
        },
    )]


# ── Composite ─────────────────────────────────────────────────────────────────

async def _detect_composite(node: dict, db, project_id: str | None) -> list[PatternMatch]:
    name = _method_name(node["id"])
    class_id = _class_id(node)
    if not class_id:
        return []

    delegating = await db.find_self_delegating_callees(node["id"], name, project_id)

    # Child-style field names → Composite
    child_hits = [
        d for d in delegating
        if any(part in _COMPOSITE_CHILD_NAMES for part in d.replace("self.", "").split("."))
    ]
    if not child_hits:
        return []

    # Must be more than a single hard-wired delegate (that's Decorator/Proxy)
    # A Composite iterates — the loop produces one edge in the graph, but the
    # field name itself distinguishes it: "child", "element", etc. vs "_component"
    bases = await db.find_base_classes(class_id, project_id)
    subs = await db.find_subclasses(class_id, project_id)

    role = "Composite"
    return [PatternMatch(
        pattern="Composite",
        role=role,
        confidence=0.80,
        participants={
            "Composite": class_id,
            "ChildDelegation": child_hits,
            **({"SharedInterface": bases[0]} if bases else {}),
        },
    )]


# ── Abstract Factory ──────────────────────────────────────────────────────────

async def _detect_abstract_factory(node: dict, db, project_id: str | None) -> list[PatternMatch]:
    class_id = _class_id(node)
    if not class_id:
        return []
    name = _method_name(node["id"])
    if not _FACTORY_RE.match(name):
        return []

    abstract = _is_abstract(node)

    if abstract:
        # Count abstract factory methods on this class — ≥2 required for Abstract Factory
        class_methods = await db.get_class_methods(class_id, project_id)
        abstract_factory_methods = [
            m for m in class_methods
            if _FACTORY_RE.match(_method_name(m["id"])) and _is_abstract(m)
        ]
        if len(abstract_factory_methods) < 2:
            return []

        subs = await db.find_subclasses(class_id, project_id)
        missing: list[str] = []
        for sub_id in subs:
            sub_methods = await db.get_class_methods(sub_id, project_id)
            sub_names = {_method_name(m["id"]) for m in sub_methods}
            unimplemented = [
                _method_name(m["id"]) for m in abstract_factory_methods
                if _method_name(m["id"]) not in sub_names
            ]
            if unimplemented:
                missing.append(f"{sub_id.split('.')[-1]}: {unimplemented}")
        return [PatternMatch(
            pattern="Abstract Factory",
            role="AbstractFactory",
            confidence=0.88,
            participants={
                "AbstractFactory": class_id,
                "AbstractMethods": [_method_name(m["id"]) for m in abstract_factory_methods],
                "ConcreteFactories": subs,
            },
            missing=missing,
            action="implement all factory methods in each concrete factory" if missing else None,
        )]

    # Concrete method — check if base class is an Abstract Factory (≥2 abstract create_*)
    bases = await db.find_base_classes(class_id, project_id)
    for base_id in bases:
        base_methods = await db.get_class_methods(base_id, project_id)
        base_abstract_factory = [
            m for m in base_methods
            if _FACTORY_RE.match(_method_name(m["id"])) and _is_abstract(m)
        ]
        if len(base_abstract_factory) >= 2:
            return [PatternMatch(
                pattern="Abstract Factory",
                role="ConcreteFactory",
                confidence=0.85,
                participants={
                    "AbstractFactory": base_id,
                    "ConcreteFactory": class_id,
                },
            )]

    return []


# ── Decorator / Proxy ─────────────────────────────────────────────────────────

async def _detect_decorator_pattern(node: dict, db, project_id: str | None) -> list[PatternMatch]:
    class_id = _class_id(node)
    if not class_id:
        return []
    name = _method_name(node["id"])

    callees = await db.get_callees(node["id"], project_id)
    delegated_classes: set[str] = set()
    for callee in callees:
        cid = callee.get("id", "")
        if cid.endswith(f".{name}") and not cid.startswith(class_id + "."):
            delegated_classes.add(".".join(cid.split(".")[:-1]))

    if not delegated_classes:
        return []

    # Confirm Decorator (vs Proxy) by shared base interface
    my_bases = set(await db.find_base_classes(class_id, project_id))
    shared_interface: str | None = None
    for dclass in delegated_classes:
        their_bases = set(await db.find_base_classes(dclass, project_id))
        shared = my_bases & their_bases
        if shared:
            shared_interface = next(iter(shared))
            break

    pattern = "Decorator" if shared_interface else "Proxy"
    participants: dict = {
        pattern: class_id,
        "Component": list(delegated_classes),
    }
    if shared_interface:
        participants["SharedInterface"] = shared_interface

    return [PatternMatch(
        pattern=pattern,
        role=pattern,
        confidence=0.82 if shared_interface else 0.65,
        participants=participants,
    )]


# ── Public API ────────────────────────────────────────────────────────────────

async def detect_patterns(
    node: dict,
    db: PatternStorageProtocol,
    project_id: str | None,
    embedder=None,
) -> list[PatternMatch]:
    """Run all pattern detectors concurrently. Returns only non-empty matches.

    Safe to call on any node — detectors return [] when the pattern is absent.
    Never raises; individual detector exceptions are swallowed so pattern
    detection never blocks the main get_function_context response.

    When embedder is provided, each structural match gains a prototype_similarity
    score and semantic-only candidates (no structural signal) are appended at
    lower confidence.
    """
    # Enrich the target node with body text if not already present.
    # body is excluded from _NODE_COLS to keep LLM responses lean, but
    # pattern detectors need it to identify raise-NotImplementedError hooks.
    if "body" not in node or node.get("body") is None:
        node = {**node, "body": await db.get_node_body(node["id"], project_id)}

    results = await asyncio.gather(
        _detect_visitor(node, db, project_id),
        _detect_template_method(node, db, project_id),
        _detect_factory_method(node, db, project_id),
        _detect_observer(node, db, project_id),
        _detect_singleton(node, db, project_id),
        _detect_strategy(node, db, project_id),
        _detect_decorator_pattern(node, db, project_id),
        _detect_cor(node, db, project_id),
        _detect_composite(node, db, project_id),
        _detect_abstract_factory(node, db, project_id),
        return_exceptions=True,
    )
    matches: list[PatternMatch] = []
    for r in results:
        if isinstance(r, list):
            matches.extend(r)

    if embedder is None:
        return matches

    # ── Prototype enrichment ─────────────────────────────────────────────────
    from .pattern_prototypes import (
        prototype_similarity as _proto_sim,
        scan_all_prototypes as _scan_proto,
        ROLE_DESCRIPTIONS,
    )

    try:
        func_embedding = await embedder.embed(
            f"{node.get('name', '')} {node.get('signature', '')} {node.get('summary', '')}"
        )
    except Exception:
        return matches

    # Attach prototype_similarity to each structural match
    structural_role_keys = set()
    for m in matches:
        role_key = f"{m.pattern.replace(' ', '')}.{m.role}"
        # Normalise key to match ROLE_DESCRIPTIONS keys (e.g. TemplateMethod.AbstractHook)
        if role_key not in ROLE_DESCRIPTIONS:
            # Try common aliases
            role_key = (
                role_key
                .replace("Factory Method", "FactoryMethod")
                .replace("Template Method", "TemplateMethod")
                .replace("Abstract Factory", "AbstractFactory")
                .replace("Chain of Responsibility", "CoR")
            )
        structural_role_keys.add(role_key)
        try:
            sim = await _proto_sim(role_key, func_embedding, embedder, embedder)
            if sim > 0:
                m.participants["prototype_similarity"] = round(sim, 3)
        except Exception:
            pass

    # Surface semantic-only candidates where no structural match fired
    try:
        semantic_candidates = await _scan_proto(func_embedding, embedder, embedder)
        for cand in semantic_candidates:
            cand_key = f"{cand['pattern']}.{cand['role']}"
            if cand_key not in structural_role_keys:
                matches.append(PatternMatch(
                    pattern=cand["pattern"],
                    role=cand["role"],
                    confidence=cand["confidence"],
                    participants={"prototype_similarity": cand["prototype_similarity"], "source": "prototype"},
                ))
    except Exception:
        pass

    return matches


def serialize_patterns(matches: list[PatternMatch]) -> list[dict]:
    out = []
    for m in matches:
        d: dict = {
            "pattern": m.pattern,
            "role": m.role,
            "confidence": round(m.confidence, 2),
            "participants": m.participants,
        }
        if m.missing:
            d["missing"] = m.missing
        if m.action:
            d["action"] = m.action
        out.append(d)
    return out
