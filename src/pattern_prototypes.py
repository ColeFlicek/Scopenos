"""
Pattern prototype vectors for semantic design pattern detection.

Each role in a GoF pattern has 10 English descriptions written from the
perspective of the code — what the function does, how it relates to other
parts of the pattern, what makes it distinct from similar roles. The centroid
of their embeddings forms a prototype vector that can be compared against
actual function embeddings via cosine similarity.

Prototypes are computed once (240 embed API calls) and stored persistently
in the pattern_prototypes table. On subsequent runs they are loaded from DB.
Re-embedding is triggered automatically when the description text changes
(detected via description_hash).

Usage:
    sim = await prototype_similarity("Visitor.ConcreteVisitor", func_embedding, store)
    # sim: float in [-1, 1], typically [0.5, 1.0] for real matches

This module is self-contained. Structural detectors in pattern_detector.py
import cosine_similarity and prototype_similarity independently.
"""
from __future__ import annotations

import hashlib
import json
import math


# ── Descriptions ──────────────────────────────────────────────────────────────
# 10 per role. Written at varying levels of abstraction (intent, mechanics,
# example domain) so the centroid covers the full semantic neighbourhood.

ROLE_DESCRIPTIONS: dict[str, list[str]] = {

    # ── Visitor ───────────────────────────────────────────────────────────────

    "Visitor.ConcreteVisitor": [
        "a class that implements operations on elements of an object structure by visiting each type separately",
        "a method named visit_TypeName that defines type-specific behaviour without modifying the element class",
        "a printer or renderer with separate methods for each node type such as visit_Add or visit_Mul",
        "a concrete visitor overriding abstract visit methods to perform operations on a heterogeneous tree",
        "a class that separates algorithms from the objects they operate on through typed dispatch methods",
        "a code generator walking an AST and producing output differently for each node kind",
        "a formatter implementing visit_Sin, visit_Cos, visit_Pow style methods for expression nodes",
        "a type-specific handler knowing how to process one kind of element in a collection of mixed types",
        "a class with a matrix of visit methods: one per element type in a hierarchy",
        "an operation encapsulated outside the element classes, dispatched by element type at runtime",
    ],

    "Visitor.AbstractVisitor": [
        "an abstract base defining visit methods that must be implemented for every element type",
        "a visitor interface with abstract visit_TypeName methods that concrete visitors must override",
        "a base class declaring the visitor protocol with one abstract method per element in the hierarchy",
        "the abstract side of double dispatch: defines the interface without implementing any operation",
        "a class with abstract visit methods forming the contract all concrete visitors must fulfil",
        "a visitor base raising NotImplementedError or using abstractmethod for each element type",
        "the abstract visitor axis of the visitor matrix: columns represent element types",
        "an ABC forcing subclasses to handle every type in the element class hierarchy",
        "a pure interface for visiting nodes, requiring type-specific behaviour in every concrete visitor",
        "the abstract template that ensures visit coverage is complete across all element types",
    ],

    "Visitor.ConcreteElement": [
        "a class implementing accept(visitor) that calls visitor.visit_Self to trigger double dispatch",
        "an element that accepts a visitor object and delegates the operation back to the visitor",
        "a node in an object structure whose accept method invokes the matching visitor method",
        "a concrete element passing itself to the visitor so the visitor can perform the right operation",
        "a data class whose accept method completes the double dispatch by calling visitor.visit_ClassName",
        "an AST node or expression type implementing accept to allow visitors to operate on it",
        "a concrete participant in the Visitor pattern that knows how to call back into a visitor",
        "an element type whose accept method routes the visitor to the right visit_X implementation",
        "a class with an accept method that enables external operations without modifying the class",
        "the element role in Visitor: stores data, delegates behaviour to accept-ed visitor objects",
    ],

    "Visitor.AbstractElement": [
        "an abstract base class declaring accept(visitor) as an abstract method for all element types",
        "the element interface in a Visitor pattern requiring subclasses to implement accept",
        "an abstract node type that defines the accept protocol for visitor dispatch",
        "a base element raising NotImplementedError in accept, forcing concrete elements to override",
        "the root of an element hierarchy defining the accept method that enables visitor dispatch",
        "an abstract class whose accept method is the hook for all visitor operations",
        "the interface that makes elements visitable: declares accept but leaves implementation to subclasses",
        "an ABC requiring all element subclasses to implement accept(visitor) for double dispatch",
        "the abstract element axis of the Visitor pattern: enforces that all types are visitable",
        "a base class that ensures every element in the hierarchy can be visited by any concrete visitor",
    ],

    # ── Template Method ───────────────────────────────────────────────────────

    "TemplateMethod.AbstractHook": [
        "an abstract method intended to be overridden by subclasses to customise an algorithm step",
        "a hook method that subclasses must implement to plug behaviour into a template algorithm",
        "an empty or abstract method called by a concrete template method that defines the algorithm skeleton",
        "a protected extension point in a base class that subclasses fill in with specific behaviour",
        "a method that raises NotImplementedError or is declared abstract so subclasses provide the logic",
        "a pass-only method whose purpose is to be overridden in derived classes",
        "one of the primitive operations called by a template method that subclasses are expected to override",
        "an abstract step in an algorithm skeleton that concrete subclasses must define",
        "the customisation point in a Template Method pattern where subclass-specific logic goes",
        "a method intentionally left unimplemented in the base class so subclasses can vary the behaviour",
    ],

    "TemplateMethod.TemplateMethod": [
        "a concrete method that defines an algorithm skeleton by calling abstract hook methods in sequence",
        "the invariant part of an algorithm that calls overridable primitive operations on self",
        "a method in a base class that orchestrates calls to abstract or pass-only sibling methods",
        "the template that fixes the order of steps while delegating each step to an abstract hook",
        "a method calling setup, process, and finish hooks that subclasses are expected to override",
        "an algorithm skeleton method that calls abstract primitives defined in subclasses",
        "the invariant orchestrator in a Template Method pattern: calls hooks, never overridden",
        "a base class method that controls flow and delegates variation points to subclass overrides",
        "a method whose body consists primarily of calls to abstract or hook methods on self",
        "the fixed structure in a Template Method: concrete logic around calls to abstract extension points",
    ],

    # ── Factory Method ────────────────────────────────────────────────────────

    "FactoryMethod.AbstractCreator": [
        "an abstract factory method that subclasses must override to return the appropriate product type",
        "a create method declared abstract in a base class, leaving the object creation to subclasses",
        "the creator role in Factory Method: defines the interface for object creation without instantiating",
        "an abstract method named create_X or make_X that concrete subclasses implement to return products",
        "a base class that defers object construction to subclasses through an abstract factory method",
        "a method raising NotImplementedError so that each subclass can return a different product type",
        "the abstract side of Factory Method: declares what to create but not how",
        "an abstract creator whose subclasses each override the factory method to produce their product",
        "a method that defines the product interface but delegates instantiation to concrete creators",
        "an abstract factory method forming the extension point for different product families",
    ],

    "FactoryMethod.ConcreteCreator": [
        "a concrete implementation of an abstract factory method that returns a specific product type",
        "a subclass overriding create_X to instantiate and return the product it is responsible for",
        "the concrete creator in Factory Method: knows exactly which class to instantiate",
        "a class that implements the abstract factory method by returning a concrete product object",
        "a factory subclass whose create method returns a specific implementation of the product interface",
        "an override of an abstract create_X method that calls a concrete constructor",
        "a creator class that fulfils the factory method contract by constructing its particular product",
        "the concrete side of Factory Method: encapsulates the construction of one specific product type",
        "a subclass providing the actual object creation logic deferred by the abstract creator",
        "a class implementing a factory method inherited from an abstract base with a concrete product",
    ],

    # ── Observer ──────────────────────────────────────────────────────────────

    "Observer.Subject": [
        "a class that maintains a list of observers and notifies them when its state changes",
        "an event source with attach, detach, and notify methods to manage subscribed listeners",
        "a publisher that broadcasts state changes to all registered observer objects",
        "the subject in Observer: holds observer references and calls update on each when state changes",
        "a class implementing subscribe, unsubscribe, and emit to manage event listeners",
        "an observable that fires notifications to all registered handlers when something changes",
        "a mutable object with a registry of dependents that are notified on every state mutation",
        "a subject class whose notify or emit method iterates over attached observers calling their update",
        "the event emitter role in Observer: manages observer lifecycle and drives notification dispatch",
        "a class with register_listener, remove_listener, and fire methods forming the subject interface",
    ],

    "Observer.Observer": [
        "a class implementing an update method called by a subject when its state changes",
        "a listener that subscribes to a subject and reacts to change notifications",
        "the observer role: receives notifications from a subject and updates itself in response",
        "a handler class with an on_event or update method invoked by an observable subject",
        "a dependent object that registers with a subject and gets called when state changes",
        "a subscriber whose update or handle method is triggered by the observed subject",
        "a class that implements the observer interface to receive change events from a publisher",
        "a concrete observer that adapts to state changes by implementing the update callback",
        "a listener registered with an event source whose receive method is called on each event",
        "a class that reacts to notifications from a subject without polling for changes",
    ],

    # ── Singleton ─────────────────────────────────────────────────────────────

    "Singleton.SingletonAccessor": [
        "a class method or static method that returns the single shared instance of a class",
        "a get_instance method that creates the instance on first call and returns it on subsequent calls",
        "the access point for a Singleton: guarantees only one object of the class is ever created",
        "a factory method that checks for an existing instance before constructing a new one",
        "a class-level accessor that lazily initialises and caches the one permitted instance",
        "getInstance or get_default method that enforces the single-instance invariant",
        "a method that returns a cached class-level reference to the sole instance of the class",
        "the entry point to a Singleton that controls instantiation and returns the shared object",
        "a class method implementing the single-instance guard using a class-level _instance variable",
        "a static accessor that returns the shared singleton, creating it if it does not yet exist",
    ],

    # ── Strategy ──────────────────────────────────────────────────────────────

    "Strategy.AbstractStrategy": [
        "an abstract class defining an algorithm interface that concrete strategies must implement",
        "an interface with one or a few abstract methods representing interchangeable algorithm variants",
        "the strategy base in Strategy pattern: declares execute or run without providing an implementation",
        "an abstract algorithm that clients depend on, with concrete subclasses providing the behaviour",
        "an ABC with a small number of abstract methods that define how an algorithm is performed",
        "the abstract strategy: defines what an algorithm does so context can use any concrete variant",
        "a base class raising NotImplementedError in its core method so each subclass provides an algorithm",
        "an interface that different algorithm implementations must conform to for the context to use them",
        "a strategy interface that decouples the algorithm from the context that uses it",
        "an abstract class whose subclasses each provide a different implementation of the same algorithm",
    ],

    "Strategy.ConcreteStrategy": [
        "a concrete algorithm implementation conforming to an abstract strategy interface",
        "a class overriding an abstract execute or run method with a specific algorithm variant",
        "the concrete strategy: encapsulates one specific algorithm and implements the strategy interface",
        "a subclass providing an interchangeable implementation of an algorithm defined abstractly",
        "a strategy implementation that a context can select at runtime to vary its behaviour",
        "a class implementing the strategy interface with one specific algorithm the context can use",
        "a concrete variant of an algorithm that plugs into a context via the strategy interface",
        "an implementation of an abstract strategy method providing a specific sorting, encoding, or formatting approach",
        "a concrete class whose algorithm method can replace any other concrete strategy at runtime",
        "a swappable algorithm implementation that conforms to the strategy contract",
    ],

    # ── Decorator / Proxy ─────────────────────────────────────────────────────

    "Decorator.Decorator": [
        "a class that wraps another object implementing the same interface to add behaviour transparently",
        "a wrapper that delegates core operations to a wrapped component while adding extra logic",
        "a class implementing the same interface as its component and forwarding calls with modifications",
        "a transparent wrapper that adds responsibilities to an object without changing its interface",
        "a class holding a reference to a component of the same type and decorating its methods",
        "a decorator that intercepts method calls on a wrapped object to add pre or post processing",
        "a wrapper implementing the component interface by calling through to the wrapped object",
        "a class that adds behaviour by composing with another object of the same interface",
        "a transparent extension wrapping a component of the same type to augment its behaviour",
        "a class forwarding all calls to a wrapped component while optionally adding side effects",
    ],

    "Decorator.Proxy": [
        "a class that controls access to another object by standing in as a surrogate",
        "a proxy wrapping a real subject and intercepting calls to add access control or lazy loading",
        "a class that forwards method calls to a real object while adding caching, logging, or access checks",
        "a surrogate that implements the same interface as the real object and delegates calls to it",
        "a class acting as a stand-in for a remote, expensive, or protected real subject",
        "a proxy that defers creation of the real object until the method is actually called",
        "a class implementing the same interface as a real service and delegating calls with extra logic",
        "an intermediary that controls when and how the real subject is accessed",
        "a wrapper implementing the subject interface, adding cross-cutting concerns before delegation",
        "a class that mediates access to another object, potentially adding security or caching layers",
    ],

    # ── Chain of Responsibility ───────────────────────────────────────────────

    "CoR.Handler": [
        "an abstract handler in a chain that either processes a request or passes it to the next handler",
        "a base class defining a handle method and a reference to the next handler in the chain",
        "the abstract link in a Chain of Responsibility with a successor field and an abstract handle method",
        "a handler interface that concrete handlers implement to form a processing pipeline",
        "a class with a set_next method and a handle method that subclasses override to process requests",
        "an abstract handler declaring handle_request and optionally forwarding to self._next",
        "the base handler role: holds a reference to a successor and defines the dispatch protocol",
        "a class that either handles a request or delegates to the next object in the responsibility chain",
        "a handler base whose handle method checks eligibility and forwards to next if it cannot handle",
        "an abstract element in a pipeline where each link either processes or passes the request along",
    ],

    "CoR.ConcreteHandler": [
        "a concrete handler that checks if it can handle a request and processes it or calls next",
        "a class overriding an abstract handle method to process requests it is responsible for",
        "a specific handler in a chain that processes matching requests and forwards others to its successor",
        "a concrete chain link that handles one category of request and delegates the rest",
        "a handler implementation checking request conditions before processing or passing to successor",
        "a class implementing handle_request by processing if eligible, otherwise calling next.handle",
        "a concrete element in a Chain of Responsibility that may or may not act on each request",
        "a handler that either acts on a request or calls self.next_handler.handle with the same request",
        "a specific processing stage that handles requests matching its criteria and forwards the rest",
        "a concrete chain handler with its own handling logic and a fall-through path to the next handler",
    ],

    # ── Composite ─────────────────────────────────────────────────────────────

    "Composite.Component": [
        "an abstract component interface shared by leaves and composites in a tree structure",
        "the common interface for leaf nodes and composite containers in a Composite pattern",
        "an abstract class or interface defining operations that apply to both leaves and containers",
        "a component base declaring operations like render or draw that all tree nodes must implement",
        "the unified interface in Composite pattern: allows clients to treat leaves and composites uniformly",
        "an abstract class whose subclasses are either atomic leaves or containers of other components",
        "a component interface allowing the same method to be called on individual or grouped objects",
        "the shared base in Composite that makes leaf and composite nodes interchangeable to clients",
        "an abstract component whose concrete subtypes are either leaves or composites of components",
        "the interface that enables uniform treatment of individual objects and compositions of objects",
    ],

    "Composite.Leaf": [
        "a leaf node in a tree structure that has no children and implements the component interface",
        "an atomic component that performs real work and cannot contain other components",
        "a concrete implementation of the component interface with no child management methods",
        "a terminal node in a Composite tree that directly implements the operation without delegation",
        "the leaf role in Composite: implements the operation concretely, not by delegating to children",
        "an element with no sub-components that performs the actual behaviour defined by the component interface",
        "a concrete leaf class that implements render, draw, or evaluate without containing children",
        "the simplest implementation of a component: no children, real logic in the operation method",
        "a primitive component that forms the base case in recursive composite operations",
        "a node with no children that represents an indivisible element in a component tree",
    ],

    "Composite.Composite": [
        "a container component that holds child components and implements operations by delegating to children",
        "a composite node whose operation method iterates over children calling the same operation on each",
        "a class implementing the component interface by aggregating other components and forwarding calls",
        "a container that treats a group of components as a single component, delegating to each child",
        "a composite role: has a list of children and implements operation by calling it on every child",
        "a branch node in a Composite tree that delegates render or evaluate to its contained children",
        "a class with add_child and remove_child methods that implements the component operation recursively",
        "a composite container whose operation produces results by combining child component results",
        "a node that recursively delegates operations to its children, combining the results",
        "a class implementing the component interface by storing and forwarding to a collection of components",
    ],

    # ── Abstract Factory ──────────────────────────────────────────────────────

    "AbstractFactory.AbstractFactory": [
        "an abstract factory class with multiple abstract create methods for families of related products",
        "a factory interface declaring create_A, create_B, create_C methods for a product family",
        "the abstract factory role: defines methods for creating each product in a related family",
        "an interface with multiple abstract factory methods ensuring product families are consistent",
        "a class declaring abstract methods for creating every product in a coordinated product suite",
        "an abstract factory requiring subclasses to implement creation of all products in a family",
        "the abstract side of Abstract Factory: a family of create methods without implementation",
        "a factory base defining the full creation interface for a suite of related product types",
        "an abstract class whose factory methods must all be implemented by each concrete factory",
        "an interface grouping factory methods for related products so families are always created together",
    ],

    "AbstractFactory.ConcreteFactory": [
        "a concrete factory implementing all abstract create methods to produce a consistent product family",
        "a class implementing a factory interface by returning specific implementations of each product type",
        "a concrete factory that creates a family of related objects all belonging to the same variant",
        "an implementation of an abstract factory creating products that work together as a coherent family",
        "a factory class implementing create_A, create_B and create_C to produce one consistent variant",
        "a concrete factory whose methods all return products from the same product family",
        "an implementation of the factory interface ensuring all created objects belong to the same family",
        "a concrete factory that provides matching implementations of every product in the family",
        "a class overriding all abstract factory methods to return products from one consistent suite",
        "the concrete side of Abstract Factory: implements every create method for a specific product family",
    ],
}


# ── Prototype computation ──────────────────────────────────────────────────────

def _description_hash(role: str) -> str:
    text = role + "".join(ROLE_DESCRIPTIONS.get(role, []))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _centroid(vectors: list[list[float]]) -> list[float]:
    n = len(vectors)
    dim = len(vectors[0])
    c = [sum(v[i] for v in vectors) / n for i in range(dim)]
    mag = math.sqrt(sum(x * x for x in c))
    return [x / mag for x in c] if mag > 0 else c


# ── In-process cache (populated on first DB load) ─────────────────────────────

_cache: dict[str, list[float]] = {}


async def ensure_prototype(role: str, embedder, store) -> list[float] | None:
    """Return the prototype vector for role, computing and persisting it if needed.

    Flow:
      1. In-process cache hit → return immediately (zero latency after first call)
      2. DB hit with matching description_hash → populate cache, return
      3. DB miss or stale hash → embed all descriptions, average, persist, cache, return

    Args:
        embedder: EmbeddingStore instance (for embed() calls)
        store:    PrototypeStore instance (for get/upsert)
    """
    if role in _cache:
        return _cache[role]

    descriptions = ROLE_DESCRIPTIONS.get(role)
    if not descriptions:
        return None

    current_hash = _description_hash(role)

    # Try loading from DB first
    row = await store.get_prototype(role)
    if row and row.get("description_hash") == current_hash:
        vec = json.loads(row["vector"])
        _cache[role] = vec
        return vec

    # Compute: embed all descriptions, average, normalize
    import asyncio
    vectors = await asyncio.gather(*[embedder.embed(d) for d in descriptions])
    centroid = _centroid(vectors)
    await store.upsert_prototype(role, centroid, current_hash)
    _cache[role] = centroid
    return centroid


async def prototype_similarity(
    role: str,
    function_embedding: list[float],
    embedder,
    store,
) -> float:
    """Cosine similarity between a function embedding and the prototype for role.

    Returns 0.0 if the prototype does not exist or embedder is unavailable.
    """
    proto = await ensure_prototype(role, embedder, store)
    if proto is None:
        return 0.0
    return cosine_similarity(function_embedding, proto)


async def scan_all_prototypes(
    function_embedding: list[float],
    embedder,
    store,
    threshold: float = 0.75,
) -> list[dict]:
    """Return all roles whose prototype similarity exceeds threshold.

    Used to surface semantic candidates when structural detection has no signal
    (dynamic dispatch, unconventional naming, closures).
    """
    import asyncio

    roles = list(ROLE_DESCRIPTIONS.keys())
    sims = await asyncio.gather(
        *[prototype_similarity(role, function_embedding, embedder, store) for role in roles],
        return_exceptions=True,
    )
    results = []
    for role, sim in zip(roles, sims):
        if isinstance(sim, float) and sim >= threshold:
            pattern, _, role_name = role.partition(".")
            results.append({
                "pattern": pattern,
                "role": role_name,
                "confidence": round(0.55 + (sim - threshold) * 0.4, 2),
                "prototype_similarity": round(sim, 3),
                "source": "prototype",
            })
    return sorted(results, key=lambda r: r["prototype_similarity"], reverse=True)
