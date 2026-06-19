# Phase 14 Demo Repos — Verification & Briefs

All 12 repos indexed and verified 2026-06-19. Each entry is the "what you're getting into"
brief for agents querying that project — drawn from `get_project_home` output.

Query verification: 5 representative queries run against requests, pytest, and django.
All returned highly relevant results (similarity 0.70–0.83). Semantic search is working.

Write tools (index_project, index_changes, enrich_summaries) return 403 for demo projects
for all non-admin users — enforced by `check_project_access` in storage.py:
`is_demo → return operation == "read"`. Covered by `test_demo_project_write_raises_403`
in tests/test_auth.py.

---

## psf/requests (1,097 nodes)
Small, focused HTTP client — 296 production functions in `src.requests` balanced by 481
test functions. Core is `Session` (58 callers) and `Request` (64 callers); `Response.json`
(28 callers) is the most-called production API. `HTTPAdapter.send` is the transport seam
where custom retry and SSL behavior lives.

## pallets/flask (1,608 nodes)
Micro web framework where HTTP entry points are visible in the index via `@app.route`
decorators. `Scaffold.route` is the dominant chokepoint with 163 callers — it underlies
every route registration. Knowledge gaps are in `setupmethod` and `ProxyMixin._get_current_object`,
the internal machinery that makes request context proxies (`g`, `request`) work.

## pytest-dev/pytest (8,053 nodes)
Testing framework with 2,199 internal functions spanning plugins, collection, runners, and
assertion rewriting. `LineMatcher.fnmatch_lines` is the dominant chokepoint with 1,750
callers — it's the primary assertion mechanism in pytest's own test suite. Understanding
`Metafunc.parametrize` (312 callers) and `FixtureRequest` is essential before touching
pytest's fixture or parametrization internals.

## mwaskom/seaborn (2,923 nodes)
Statistical visualization library built around two APIs: the classic functional API
(`seaborn.distributions`, `seaborn.categorical`, `seaborn.relational`) and the newer
objects API (`seaborn._core.plot.Plot`). `Plot.add` (154 callers) and `color_palette`
(120 callers) are the primary chokepoints. The objects API is under heaviest test pressure —
889 call edges from `tests._core` to `seaborn._core`.

## sphinx-doc/sphinx (8,859 nodes)
Documentation generator with 9 major subsystems: domains (C++/Python/C/JS language
support), builders (HTML/LaTeX/EPUB output), writers, extensions, and environment.
`sphinx.util` is the universal dependency (936 edges from `sphinx.domains` alone).
`BuildEnvironment` is the central state object bridging parsing, domain resolution,
and building — touch it carefully.

## pydata/xarray (8,907 nodes)
N-dimensional labeled array library for scientific computing. `DataArray` (1,181 callers)
and `Dataset` (1,075 callers) are the two load-bearing data structures that every subsystem
depends on. `xarray.core` → `xarray.structure` (223 edges) is the dominant internal flow;
`xarray.backends` handles IO format integration (NetCDF, Zarr, HDF5).

## pylint-dev/pylint (9,611 nodes)
Python linter with 1,138 functions across checkers and 237 in extensions. The production
chokepoint is `safe_infer` (137 callers) — the AST type inference utility that almost every
checker depends on; the top raw caller count is a test fixture. `pylint.testutils` is the
key infrastructure for writing checker tests; the `pylint.checkers` → `pylint.lint` edge
(88 calls) is where checkers wire into the run loop.

## scikit-learn/scikit-learn (12,596 nodes)
ML library with estimators (linear_model, ensemble, metrics), utilities, and tests. The top
chokepoints are test utilities — `raises` (1,220 callers), `assert_allclose` (949) — so
estimator coverage is excellent but the test scaffolding is the real load-bearing layer.
The `fit`/`predict`/`score` interface is the consistent seam across all estimators;
`sklearn.utils` (1,863 fns) is the largest subsystem and the source of most cross-estimator
utilities.

## matplotlib/matplotlib (11,955 nodes)
Plotting library where 10,053 of 11,955 functions live in `lib.matplotlib` — `mpl_toolkits`
and `galleries` are thin wrappers. `FigureBase.add_subplot` (366 callers) and
`transforms.BboxBase` (233+163 callers) are the central geometric primitives; `Path`
(274 callers) underpins all line and patch rendering. The `galleries/examples` subsystem
(716 fns, 595 edges to lib.matplotlib) serves as a living integration test of the API surface.

## astropy/astropy (19,420 nodes)
Astronomy library spanning 23 subsystems: coordinates, units, time, modeling, I/O
(FITS/VOTable/ASCII), visualization, and cosmology. `astropy.table` is the universal data
exchange format that 7 other subsystems route through. `Time` (477 callers) and `WCS`
(348 callers) are the core domain objects linking coordinates, images, and observation
metadata; `TableColumns.isinstance` (1,873 callers) is the most-called function in the
entire index.

## django/django (42,988 nodes)
Full-stack web framework with ~43K functions across 20+ subsystems. `django.db` (ORM,
migrations, SQL backends) and `django.contrib` (auth, admin, sessions, staticfiles) are
the largest subsystems. `QuerySet.annotate` (1,035 callers) is the most-used ORM
chokepoint; `django.template.loader_tags.BlockNode.super` (1,779 callers) is the template
inheritance seam. Before touching the ORM query layer, run `get_impact_radius("QuerySet.annotate", depth=2)`.

## sympy/sympy (38,747 nodes)
Computer algebra system with 38,747 functions across symbolic math (core, polys,
combinatorics), physics simulations, and printing/conversion. `Symbol` (1,717 callers) and
`Rational` (1,209 callers) are the fundamental symbolic primitives everything else builds
on. `sympy.polys` (6,446 fns) is the largest subsystem — polynomial algorithms underpin
most symbolic computation; query this project with mathematical operation descriptions,
not code patterns.
