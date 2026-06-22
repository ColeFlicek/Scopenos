"""
Tests for design pattern detection (pattern_detector.py).

Public interface under test: detect_patterns(node, db, project_id) → list[PatternMatch]
Pure helper under test:      _is_pass_only(body) → bool

Tests seed the DB with minimal FunctionNode/CallEdge fixtures and assert on
the PatternMatch output — not on internal storage queries or detector branches.
"""
import json
import pytest
import pytest_asyncio

from src.call_graph.parser import FunctionNode, CallEdge
from src.call_graph.storage import CallGraphDB
from src.pattern_detector import PatternMatch, detect_patterns, _is_pass_only


# ── Helpers ───────────────────────────────────────────────────────────────────

def _method(
    node_id: str,
    *,
    decorators: list | None = None,
    body: str = "pass",
) -> FunctionNode:
    """Build a minimal method FunctionNode.

    body is the function body TEXT (not the full def line). The helper wraps
    it in a proper def so stored body matches what the parser produces.
    """
    parts = node_id.split(".")
    name = parts[-1]
    full_body = f"def {name}(self):\n    {body}"
    return FunctionNode(
        id=node_id,
        name=name,
        file="/project/mod.py",
        module=".".join(parts[:2]) if len(parts) >= 2 else parts[0],
        type="method",
        signature=f"def {name}(self):",
        body=full_body,
        docstring="",
        body_hash="abc123",
        decorators=decorators or [],
    )


def _edge(caller: str, callee: str, edge_type: str = "calls") -> CallEdge:
    return CallEdge(
        caller_id=caller,
        callee_name=callee,
        edge_type=edge_type,
        file="/project/mod.py",
    )


async def _seed(
    db: CallGraphDB,
    project_id: str,
    nodes: list[FunctionNode],
    edges: list[CallEdge] | None = None,
) -> None:
    await db.upsert_project(project_id, project_id, "/project")
    await db.upsert_nodes(nodes, project_id)
    if edges:
        all_ids = await db.get_all_node_ids(project_id)
        await db.upsert_edges(edges, all_ids, project_id)


def _patterns(matches: list[PatternMatch]) -> list[str]:
    """Return 'Pattern(Role)' strings for easy assertion."""
    return [f"{m.pattern}({m.role})" for m in matches]


def _match(matches: list[PatternMatch], pattern: str) -> PatternMatch | None:
    return next((m for m in matches if m.pattern == pattern), None)


# ── _is_pass_only ─────────────────────────────────────────────────────────────

class TestIsPassOnly:

    def test_pass_only_body_is_true(self):
        assert _is_pass_only("def setup(self):\n    pass") is True

    def test_ellipsis_only_body_is_true(self):
        assert _is_pass_only("def setup(self):\n    ...") is True

    def test_docstring_then_pass_is_true(self):
        body = 'def handle(self):\n    """Handle the request."""\n    pass'
        assert _is_pass_only(body) is True

    def test_multiline_docstring_then_pass_is_true(self):
        body = 'def handle(self):\n    """Multi\n    line\n    doc.\n    """\n    pass'
        assert _is_pass_only(body) is True

    def test_raise_not_implemented_is_false(self):
        body = "def emit(self, record):\n    raise NotImplementedError"
        assert _is_pass_only(body) is False

    def test_real_logic_is_false(self):
        body = "def get_name(self):\n    return self.name"
        assert _is_pass_only(body) is False

    def test_nonempty_body_with_comment_then_logic_is_false(self):
        body = "def finish(self):\n    pass\n    # cleanup\n    self.clean()"
        assert _is_pass_only(body) is False

    def test_empty_string_is_false(self):
        assert _is_pass_only("") is False


# ── Template Method ───────────────────────────────────────────────────────────

class TestTemplateMethod:

    @pytest.mark.asyncio
    async def test_abstractmethod_hook_detected_as_abstract_hook(self, db: CallGraphDB):
        """@abstractmethod on a method in a class with subclasses → AbstractHook."""
        nodes = [
            _method("mod.Base.hook", decorators=["abstractmethod"]),
            _method("mod.Concrete.hook", body="return 42"),
        ]
        edges = [_edge("mod.Concrete", "mod.Base", edge_type="inherits")]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.Base.hook", "name": "hook", "type": "method", "decorators": '["abstractmethod"]'}
        matches = await detect_patterns(node, db, "proj")

        assert any(m.pattern == "Template Method" and m.role == "AbstractHook" for m in matches)

    @pytest.mark.asyncio
    async def test_raise_not_implemented_hook_detected(self, db: CallGraphDB):
        """raise NotImplementedError body → AbstractHook at same confidence as @abstractmethod."""
        nodes = [
            _method("mod.Base.emit", body="raise NotImplementedError"),
            _method("mod.Concrete.emit", body="print('hello')"),
        ]
        edges = [_edge("mod.Concrete", "mod.Base", edge_type="inherits")]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.Base.emit", "name": "emit", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        tm = _match(matches, "Template Method")
        assert tm is not None
        assert tm.role == "AbstractHook"
        assert tm.confidence == pytest.approx(0.85)

    @pytest.mark.asyncio
    async def test_missing_subclass_impl_surfaces_in_missing(self, db: CallGraphDB):
        """Subclass that doesn't override the abstract hook appears in missing."""
        nodes = [
            _method("mod.Base.hook", decorators=["abstractmethod"]),
            _method("mod.ConcreteA.hook", body="return 1"),
            # ConcreteB inherits but does NOT implement hook
        ]
        edges = [
            _edge("mod.ConcreteA", "mod.Base", edge_type="inherits"),
            _edge("mod.ConcreteB", "mod.Base", edge_type="inherits"),
        ]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.Base.hook", "name": "hook", "type": "method", "decorators": '["abstractmethod"]'}
        matches = await detect_patterns(node, db, "proj")

        tm = _match(matches, "Template Method")
        assert tm is not None
        assert any("ConcreteB" in m for m in tm.missing)

    @pytest.mark.asyncio
    async def test_pass_only_hook_called_from_sibling_detected(self, db: CallGraphDB):
        """pass-only method called from a sibling → AbstractHook at confidence 0.75."""
        nodes = [
            _method("mod.Base.__init__", body="self.setup()\nself.handle()"),
            _method("mod.Base.setup", body="pass"),
            _method("mod.Base.handle", body="pass"),
            _method("mod.Concrete.setup", body="self.x = 1"),
            _method("mod.Concrete.handle", body="print('hi')"),
        ]
        edges = [
            _edge("mod.Base.__init__", "setup"),
            _edge("mod.Base.__init__", "handle"),
            _edge("mod.Concrete", "mod.Base", edge_type="inherits"),
        ]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.Base.handle", "name": "handle", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        tm = _match(matches, "Template Method")
        assert tm is not None
        assert tm.role == "AbstractHook"
        assert tm.confidence == pytest.approx(0.75)

    @pytest.mark.asyncio
    async def test_pass_only_not_called_from_sibling_not_detected(self, db: CallGraphDB):
        """pass-only method with NO sibling caller → no Template Method match (avoids false positives)."""
        nodes = [
            _method("mod.Base.noop", body="pass"),
            _method("mod.Concrete.noop", body="pass"),
        ]
        edges = [_edge("mod.Concrete", "mod.Base", edge_type="inherits")]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.Base.noop", "name": "noop", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        assert not any(m.pattern == "Template Method" for m in matches)

    @pytest.mark.asyncio
    async def test_method_calling_abstract_hook_is_template_method_role(self, db: CallGraphDB):
        """Concrete method that calls an abstract sibling → TemplateMethod role."""
        nodes = [
            _method("mod.Base.run", body="self.setup()\nself.process()"),
            _method("mod.Base.setup", decorators=["abstractmethod"]),
            _method("mod.Base.process", decorators=["abstractmethod"]),
        ]
        edges = [
            _edge("mod.Base.run", "mod.Base.setup"),
            _edge("mod.Base.run", "mod.Base.process"),
        ]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.Base.run", "name": "run", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        tm = _match(matches, "Template Method")
        assert tm is not None
        assert tm.role == "TemplateMethod"
        assert "mod.Base.setup" in tm.participants["AbstractHooks"]
        assert "mod.Base.process" in tm.participants["AbstractHooks"]

    @pytest.mark.asyncio
    async def test_method_calling_pass_only_hook_is_template_method_role(self, db: CallGraphDB):
        """Template method calling pass-only sibling hooks → TemplateMethod role."""
        nodes = [
            _method("mod.Base.__init__", body="self.setup()\nself.handle()"),
            _method("mod.Base.setup",    body="pass"),
            _method("mod.Base.handle",   body="pass"),
        ]
        edges = [
            _edge("mod.Base.__init__", "setup"),
            _edge("mod.Base.__init__", "handle"),
        ]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.Base.__init__", "name": "__init__", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        tm = _match(matches, "Template Method")
        assert tm is not None
        assert tm.role == "TemplateMethod"


# ── Factory Method ────────────────────────────────────────────────────────────

class TestFactoryMethod:

    @pytest.mark.asyncio
    async def test_abstract_create_is_abstract_creator(self, db: CallGraphDB):
        """Abstract create_X method → AbstractCreator; concrete subclasses listed."""
        nodes = [
            _method("mod.Base.create_product", decorators=["abstractmethod"]),
            _method("mod.ConcreteA.create_product", body="return A()"),
            _method("mod.ConcreteB.create_product", body="return B()"),
        ]
        edges = [
            _edge("mod.ConcreteA", "mod.Base", edge_type="inherits"),
            _edge("mod.ConcreteB", "mod.Base", edge_type="inherits"),
        ]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.Base.create_product", "name": "create_product", "type": "method", "decorators": '["abstractmethod"]'}
        matches = await detect_patterns(node, db, "proj")

        fm = _match(matches, "Factory Method")
        assert fm is not None
        assert fm.role == "AbstractCreator"
        assert "mod.ConcreteA" in fm.participants["ConcreteCreators"]
        assert "mod.ConcreteB" in fm.participants["ConcreteCreators"]

    @pytest.mark.asyncio
    async def test_concrete_override_of_abstract_create_is_concrete_creator(self, db: CallGraphDB):
        """Concrete override of an abstract factory method → ConcreteCreator."""
        nodes = [
            _method("mod.Base.create_product", decorators=["abstractmethod"]),
            _method("mod.Concrete.create_product", body="return Product()"),
        ]
        edges = [_edge("mod.Concrete", "mod.Base", edge_type="inherits")]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.Concrete.create_product", "name": "create_product", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        fm = _match(matches, "Factory Method")
        assert fm is not None
        assert fm.role == "ConcreteCreator"
        assert fm.participants["AbstractCreator"] == "mod.Base"

    @pytest.mark.asyncio
    async def test_missing_concrete_impls_surface_in_missing(self, db: CallGraphDB):
        """Subclass that doesn't implement the abstract factory method → missing."""
        nodes = [
            _method("mod.Base.create_product", decorators=["abstractmethod"]),
            _method("mod.ConcreteA.create_product", body="return A()"),
            # ConcreteB inherits but has no create_product
        ]
        edges = [
            _edge("mod.ConcreteA", "mod.Base", edge_type="inherits"),
            _edge("mod.ConcreteB", "mod.Base", edge_type="inherits"),
        ]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.Base.create_product", "name": "create_product", "type": "method", "decorators": '["abstractmethod"]'}
        matches = await detect_patterns(node, db, "proj")

        fm = _match(matches, "Factory Method")
        assert fm is not None
        assert any("ConcreteB" in m for m in fm.missing)

    @pytest.mark.asyncio
    async def test_make_and_build_verbs_trigger_detection(self, db: CallGraphDB):
        """make_* and build_* are also factory verbs."""
        for verb in ("make_widget", "build_widget"):
            nodes = [
                _method(f"mod.Base.{verb}", decorators=["abstractmethod"]),
                _method(f"mod.Concrete.{verb}", body="return Widget()"),
            ]
            edges = [_edge("mod.Concrete", "mod.Base", edge_type="inherits")]
            await _seed(db, f"proj_{verb}", nodes, edges)

            node = {"id": f"mod.Base.{verb}", "name": verb, "type": "method", "decorators": '["abstractmethod"]'}
            matches = await detect_patterns(node, db, f"proj_{verb}")

            assert _match(matches, "Factory Method") is not None, f"Expected Factory Method for {verb}"

    @pytest.mark.asyncio
    async def test_create_method_with_no_subclasses_not_detected(self, db: CallGraphDB):
        """Non-abstract create method with no subclasses → no Factory Method match."""
        nodes = [_method("mod.Factory.create", body="return Obj()")]
        await _seed(db, "proj", nodes)

        node = {"id": "mod.Factory.create", "name": "create", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        assert not any(m.pattern == "Factory Method" for m in matches)


# ── Visitor — named dispatch ──────────────────────────────────────────────────

class TestVisitorNamedDispatch:

    @pytest.mark.asyncio
    async def test_dispatch_method_across_two_classes_detected(self, db: CallGraphDB):
        """_print_Sin across ≥2 visitor classes → ConcreteVisitor."""
        nodes = [
            _method("mod.LatexPrinter._print_Sin", body="return r'\\sin'"),
            _method("mod.LatexPrinter._print_Cos", body="return r'\\cos'"),
            _method("mod.StrPrinter._print_Sin",   body="return 'sin'"),
            _method("mod.StrPrinter._print_Cos",   body="return 'cos'"),
        ]
        await _seed(db, "proj", nodes)

        node = {"id": "mod.LatexPrinter._print_Sin", "name": "_print_Sin", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        v = _match(matches, "Visitor")
        assert v is not None
        assert v.role == "ConcreteVisitor"

    @pytest.mark.asyncio
    async def test_single_visitor_class_not_detected(self, db: CallGraphDB):
        """_print_X in only one class → no Visitor match (≥2 classes required)."""
        nodes = [
            _method("mod.LatexPrinter._print_Sin", body="return r'\\sin'"),
            _method("mod.LatexPrinter._print_Cos", body="return r'\\cos'"),
        ]
        await _seed(db, "proj", nodes)

        node = {"id": "mod.LatexPrinter._print_Sin", "name": "_print_Sin", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        assert not any(m.pattern == "Visitor" for m in matches)

    @pytest.mark.asyncio
    async def test_missing_handler_surfaces_in_missing(self, db: CallGraphDB):
        """StrPrinter missing _print_Cos → it appears in missing on StrPrinter's match."""
        nodes = [
            _method("mod.LatexPrinter._print_Sin", body="return r'\\sin'"),
            _method("mod.LatexPrinter._print_Cos", body="return r'\\cos'"),
            _method("mod.StrPrinter._print_Sin",   body="return 'sin'"),
            # StrPrinter is missing _print_Cos
        ]
        await _seed(db, "proj", nodes)

        node = {"id": "mod.StrPrinter._print_Sin", "name": "_print_Sin", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        v = _match(matches, "Visitor")
        assert v is not None
        assert any("Cos" in m for m in v.missing)

    @pytest.mark.asyncio
    async def test_non_allowlist_verb_not_detected(self, db: CallGraphDB):
        """_get_X shape with verb not in allowlist → no Visitor match."""
        nodes = [
            _method("mod.ClassA._get_Sin", body="return 1"),
            _method("mod.ClassB._get_Sin", body="return 2"),
        ]
        await _seed(db, "proj", nodes)

        node = {"id": "mod.ClassA._get_Sin", "name": "_get_Sin", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        assert not any(m.pattern == "Visitor" for m in matches)


# ── Observer ──────────────────────────────────────────────────────────────────

class TestObserver:

    @pytest.mark.asyncio
    async def test_class_with_all_three_subject_methods_detected(self, db: CallGraphDB):
        """notify + attach + detach → Subject at highest confidence."""
        nodes = [
            _method("mod.EventBus.attach",  body="self._listeners.append(l)"),
            _method("mod.EventBus.detach",  body="self._listeners.remove(l)"),
            _method("mod.EventBus.notify",  body="for l in self._listeners: l.update()"),
        ]
        await _seed(db, "proj", nodes)

        node = {"id": "mod.EventBus.notify", "name": "notify", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        obs = _match(matches, "Observer")
        assert obs is not None
        assert obs.role == "Subject"
        assert obs.confidence > 0.9

    @pytest.mark.asyncio
    async def test_missing_detach_surfaces_in_missing(self, db: CallGraphDB):
        """notify + attach but no detach → Subject detected but detach listed in missing."""
        nodes = [
            _method("mod.Bus.attach", body="self._listeners.append(l)"),
            _method("mod.Bus.notify", body="for l in self._listeners: l.update()"),
        ]
        await _seed(db, "proj", nodes)

        node = {"id": "mod.Bus.notify", "name": "notify", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        obs = _match(matches, "Observer")
        assert obs is not None
        assert any("detach" in m for m in obs.missing)

    @pytest.mark.asyncio
    async def test_notify_alone_not_detected(self, db: CallGraphDB):
        """Only notify with no attach/detach → no Observer match (insufficient signal)."""
        nodes = [_method("mod.Bus.notify", body="pass")]
        await _seed(db, "proj", nodes)

        node = {"id": "mod.Bus.notify", "name": "notify", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        assert not any(m.pattern == "Observer" for m in matches)


# ── Chain of Responsibility ───────────────────────────────────────────────────

class TestChainOfResponsibility:

    @pytest.mark.asyncio
    async def test_handle_delegating_to_next_detected(self, db: CallGraphDB):
        """Method calling self._next.handle() → ConcreteHandler."""
        nodes = [_method("mod.LogHandler.handle", body="if self.can_handle():\n    return\nself._next.handle(req)")]
        edges = [_edge("mod.LogHandler.handle", "self._next.handle")]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.LogHandler.handle", "name": "handle", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        assert any(m.pattern == "Chain of Responsibility" for m in matches)

    @pytest.mark.asyncio
    async def test_successor_field_variant_detected(self, db: CallGraphDB):
        """self.successor.handle() also triggers CoR detection."""
        nodes = [_method("mod.Auth.handle", body="if ok: return\nself.successor.handle(req)")]
        edges = [_edge("mod.Auth.handle", "self.successor.handle")]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.Auth.handle", "name": "handle", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        assert any(m.pattern == "Chain of Responsibility" for m in matches)

    @pytest.mark.asyncio
    async def test_delegation_to_unrelated_field_not_detected(self, db: CallGraphDB):
        """self.helper.process() — helper is not a successor field → no CoR match."""
        nodes = [_method("mod.Service.process", body="self.helper.process()")]
        edges = [_edge("mod.Service.process", "self.helper.process")]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.Service.process", "name": "process", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        assert not any(m.pattern == "Chain of Responsibility" for m in matches)


# ── Composite ─────────────────────────────────────────────────────────────────

class TestComposite:

    @pytest.mark.asyncio
    async def test_method_delegating_to_children_detected(self, db: CallGraphDB):
        """Method calling child.render() → Composite role."""
        nodes = [_method("mod.Panel.render", body="for child in self._children:\n    child.render()")]
        edges = [_edge("mod.Panel.render", "child.render")]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.Panel.render", "name": "render", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        assert any(m.pattern == "Composite" and m.role == "Composite" for m in matches)

    @pytest.mark.asyncio
    async def test_element_child_name_variant_detected(self, db: CallGraphDB):
        """element.draw() callee also triggers Composite (element is in child vocab)."""
        nodes = [_method("mod.Group.draw", body="for el in self.elements:\n    el.draw()")]
        edges = [_edge("mod.Group.draw", "element.draw")]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.Group.draw", "name": "draw", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        assert any(m.pattern == "Composite" for m in matches)

    @pytest.mark.asyncio
    async def test_delegation_to_component_field_not_composite(self, db: CallGraphDB):
        """self._component.render() — _component is Decorator vocabulary, not Composite."""
        nodes = [_method("mod.LoggingDecorator.render", body="return self._component.render()")]
        edges = [_edge("mod.LoggingDecorator.render", "self._component.render")]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.LoggingDecorator.render", "name": "render", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        assert not any(m.pattern == "Composite" for m in matches)

    @pytest.mark.asyncio
    async def test_no_child_delegation_not_detected(self, db: CallGraphDB):
        """A render method with no child delegation → no Composite match."""
        nodes = [_method("mod.Text.render", body="return self._text")]
        await _seed(db, "proj", nodes)

        node = {"id": "mod.Text.render", "name": "render", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        assert not any(m.pattern == "Composite" for m in matches)


# ── Abstract Factory ──────────────────────────────────────────────────────────

class TestAbstractFactory:

    @pytest.mark.asyncio
    async def test_class_with_two_abstract_create_methods_is_abstract_factory(self, db: CallGraphDB):
        """≥2 abstract create_* methods on same class → AbstractFactory role."""
        nodes = [
            _method("mod.UIFactory.create_button", decorators=["abstractmethod"]),
            _method("mod.UIFactory.create_dialog", decorators=["abstractmethod"]),
            _method("mod.WinFactory.create_button", body="return WinButton()"),
            _method("mod.WinFactory.create_dialog", body="return WinDialog()"),
        ]
        edges = [_edge("mod.WinFactory", "mod.UIFactory", edge_type="inherits")]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.UIFactory.create_button", "name": "create_button", "type": "method", "decorators": '["abstractmethod"]'}
        matches = await detect_patterns(node, db, "proj")

        af = _match(matches, "Abstract Factory")
        assert af is not None
        assert af.role == "AbstractFactory"
        assert "create_button" in af.participants["AbstractMethods"]
        assert "create_dialog" in af.participants["AbstractMethods"]

    @pytest.mark.asyncio
    async def test_concrete_factory_implementing_all_methods_detected(self, db: CallGraphDB):
        """Concrete subclass implementing all abstract factory methods → ConcreteFactory."""
        nodes = [
            _method("mod.UIFactory.create_button", decorators=["abstractmethod"]),
            _method("mod.UIFactory.create_dialog", decorators=["abstractmethod"]),
            _method("mod.WinFactory.create_button", body="return WinButton()"),
            _method("mod.WinFactory.create_dialog", body="return WinDialog()"),
        ]
        edges = [_edge("mod.WinFactory", "mod.UIFactory", edge_type="inherits")]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.WinFactory.create_button", "name": "create_button", "type": "method", "decorators": "[]"}
        matches = await detect_patterns(node, db, "proj")

        af = _match(matches, "Abstract Factory")
        assert af is not None
        assert af.role == "ConcreteFactory"
        assert af.participants["AbstractFactory"] == "mod.UIFactory"

    @pytest.mark.asyncio
    async def test_missing_concrete_impl_surfaces_in_missing(self, db: CallGraphDB):
        """ConcreteFactory missing one of the abstract methods → surfaced in missing."""
        nodes = [
            _method("mod.UIFactory.create_button", decorators=["abstractmethod"]),
            _method("mod.UIFactory.create_dialog", decorators=["abstractmethod"]),
            _method("mod.WinFactory.create_button", body="return WinButton()"),
            # WinFactory does NOT implement create_dialog
        ]
        edges = [_edge("mod.WinFactory", "mod.UIFactory", edge_type="inherits")]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.UIFactory.create_button", "name": "create_button", "type": "method", "decorators": '["abstractmethod"]'}
        matches = await detect_patterns(node, db, "proj")

        af = _match(matches, "Abstract Factory")
        assert af is not None
        assert any("create_dialog" in m for m in af.missing)

    @pytest.mark.asyncio
    async def test_single_abstract_create_is_factory_method_not_abstract_factory(self, db: CallGraphDB):
        """Only one abstract create_* → Factory Method, not Abstract Factory."""
        nodes = [
            _method("mod.Creator.create_product", decorators=["abstractmethod"]),
            _method("mod.Concrete.create_product", body="return Product()"),
        ]
        edges = [_edge("mod.Concrete", "mod.Creator", edge_type="inherits")]
        await _seed(db, "proj", nodes, edges)

        node = {"id": "mod.Creator.create_product", "name": "create_product", "type": "method", "decorators": '["abstractmethod"]'}
        matches = await detect_patterns(node, db, "proj")

        assert _match(matches, "Factory Method") is not None
        assert not any(m.pattern == "Abstract Factory" for m in matches)
