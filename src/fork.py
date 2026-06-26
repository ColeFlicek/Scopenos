"""Fork delta algorithm: create isolated project snapshots at a specific git commit.

A fork copies the parent project's call graph into a new schema, then replaces
any functions whose source changed between the fork commit and HEAD with
re-parsed versions from that earlier commit.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .call_graph.parser import TreeSitterParser
from .call_graph.storage import CallGraphDB, derive_schema_name

_SUPPORTED_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx"}
_parser = TreeSitterParser()


def git_changed_files(
    repo_path: str,
    target_commit: str,
    base_commit: str = "HEAD",
) -> list[str]:
    """Return list of supported source file paths changed between target_commit and base_commit.

    Uses ``git diff --name-only {target_commit} {base_commit}`` and filters to
    files with extensions in _SUPPORTED_EXTENSIONS. Paths are relative to repo root.
    """
    result = subprocess.run(
        ["git", "-C", repo_path, "diff", "--name-only", target_commit, base_commit],
        capture_output=True,
        text=True,
        check=True,
    )
    changed: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if Path(line).suffix.lower() in _SUPPORTED_EXTENSIONS:
            changed.append(line)
    return changed


async def get_function_content_hashes(
    schema_name: str,
    file_paths: list[str],
    db: CallGraphDB,
) -> dict[str, str]:
    """Return {node_id: body_hash} for all nodes in given files in the fork schema.

    Queries ``SELECT id, body_hash FROM {schema}.nodes WHERE file = ANY($1)``
    directly (schema-qualified) so no search_path magic is needed.
    """
    return await db.get_node_hashes_in_schema(schema_name, file_paths)


def parse_functions_at_commit(
    repo_path: str,
    file_paths: list[str],
    target_commit: str,
) -> tuple[list, list]:
    """Parse source files AT target_commit using ``git show {commit}:{file}``.

    Returns (all_nodes, all_edges). Files that don't exist at target_commit are
    silently skipped (git show exits non-zero for missing paths).
    """
    all_nodes: list = []
    all_edges: list = []
    for rel_path in file_paths:
        result = subprocess.run(
            ["git", "-C", repo_path, "show", f"{target_commit}:{rel_path}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # File doesn't exist at this commit — skip it
            continue
        content = result.stdout
        # Use absolute-ish path rooted at repo for module derivation
        abs_path = str(Path(repo_path) / rel_path)
        nodes, edges = _parser.parse_file(abs_path, content, project_root=repo_path)
        all_nodes.extend(nodes)
        all_edges.extend(edges)
    return all_nodes, all_edges


async def apply_fork_delta(
    fork_schema_name: str,
    parent_project_id: str,
    repo_path: str,
    target_commit: str,
    org_db: CallGraphDB,
) -> dict:
    """Apply delta between parent HEAD and target_commit to the fork schema.

    Steps:
    1. Get changed files via git_changed_files()
    2. Get stored hashes for those files from the fork schema (which mirrors parent HEAD)
    3. Parse functions at target_commit
    4. For changed functions (hash differs): upsert into fork schema nodes table
    5. For functions in changed files not in parsed output: delete from fork schema nodes table
    6. Returns {"updated": int, "deleted": int, "unchanged": int}
    """
    import json

    changed_files = git_changed_files(repo_path, target_commit)
    if not changed_files:
        return {"updated": 0, "deleted": 0, "unchanged": 0}

    # Stored hashes reflect parent HEAD (fork schema was copied from parent)
    stored_hashes = await get_function_content_hashes(fork_schema_name, changed_files, org_db)

    # Parse what the files looked like at target_commit
    parsed_nodes, _parsed_edges = parse_functions_at_commit(repo_path, changed_files, target_commit)

    # Build a set of node IDs that exist at target_commit
    parsed_by_id = {n.id: n for n in parsed_nodes}

    updated = 0
    unchanged = 0

    # Upsert nodes whose body_hash differs (or are new at old commit)
    nodes_to_upsert = []
    for node in parsed_nodes:
        old_hash = stored_hashes.get(node.id, "")
        if node.body_hash != old_hash:
            nodes_to_upsert.append(node)
            updated += 1
        else:
            unchanged += 1

    if nodes_to_upsert:
        # Upsert directly into fork schema using schema-qualified queries
        rows = [
            (
                parent_project_id,
                n.id, n.file, n.module, n.type, n.name,
                n.signature, n.docstring, n.body, n.body_hash,
                json.dumps(n.decorators),
                1 if n.is_external else 0,
                n.start_line, n.end_line,
                n.return_type,
                1 if n.is_async else 0,
                json.dumps(n.parameter_names),
                n.enclosing_class,
                n.structural_layer,
            )
            for n in nodes_to_upsert
        ]
        await org_db.upsert_nodes_into_schema(fork_schema_name, rows, parent_project_id)

    # Delete nodes from changed files that no longer exist at target_commit
    deleted_ids = [nid for nid in stored_hashes if nid not in parsed_by_id]
    deleted = len(deleted_ids)
    if deleted_ids:
        await org_db.delete_nodes_from_schema_by_ids(fork_schema_name, deleted_ids)

    return {"updated": updated, "deleted": deleted, "unchanged": unchanged}


async def create_fork(
    parent_project_id: str,
    target_commit: str,
    fork_project_id: str,
    repo_path: str,
    org_db: CallGraphDB,
    user_id: str = "",
) -> dict:
    """Create an isolated fork of parent_project_id at target_commit.

    Steps:
    1. Derive parent and fork schema names
    2. Copy structural tables from parent to fork via fork_schema()
    3. Apply delta: re-parse changed functions at target_commit
    4. Register the fork in public.projects (with is_fork=True)
    5. Optionally grant owner access to user_id
    6. Returns {"fork_project_id": str, "schema_name": str, "delta": dict}
    """
    from datetime import datetime, timezone

    # Step 1: derive schema names
    parent_schema = await org_db.get_schema_name_for_project(parent_project_id)
    short = target_commit[:7]
    fork_schema_name = f"{parent_schema}_fork_{short}"

    # Step 2: copy structural tables from parent
    await org_db.fork_schema(parent_schema, fork_schema_name)

    # Step 3: apply delta
    # The fork schema was copied from parent, so all node rows have project_id=parent_project_id.
    # Pass parent_project_id so the upsert ON CONFLICT(project_id, id) key matches existing rows.
    delta = await apply_fork_delta(
        fork_schema_name, parent_project_id, repo_path, target_commit, org_db
    )

    # Step 4: register fork in public.projects
    now = datetime.now(timezone.utc).isoformat()
    await org_db.insert_fork_project(
        fork_project_id,
        f"{parent_project_id}@{short}",
        repo_path,
        target_commit,
        fork_schema_name,
        parent_schema,
        now,
    )

    # Step 5: optionally grant access
    if user_id:
        await org_db.grant_project_access(user_id, fork_project_id, "owner")

    return {
        "fork_project_id": fork_project_id,
        "schema_name": fork_schema_name,
        "delta": delta,
    }
