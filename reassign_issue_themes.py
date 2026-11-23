"""Refresh one risk theme and reassign issue themes only.

Usage:
    python scripts/reassign_issue_themes.py
    python scripts/reassign_issue_themes.py --theme "Another risk theme"
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from tempfile import gettempdir

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import boto3

from src import bedrock_client, cluster_themes, config, db


DEFAULT_THEME = "Infrastructure / procurement delays"


def load_risk_theme(label):
    with open(config.THEME_SEED_PATH, encoding="utf-8") as taxonomy_file:
        taxonomy = json.load(taxonomy_file)
    theme = next((item for item in taxonomy
                  if item.get("label") == label and item.get("kind") == "risk"), None)
    if theme is None:
        raise ValueError(f"Risk theme not found in taxonomy: {label}")
    return theme


def reassign_issues(theme):
    if config.MOCK_MODE:
        raise RuntimeError("Refusing to mix mock and live embeddings; set MOCK_MODE=false.")
    if boto3.Session().get_credentials() is None:
        raise RuntimeError("AWS credentials are unavailable for the Bedrock embedding call.")

    print(f"Database: {config.PG_HOST}:{config.PG_PORT}/{config.PG_DB}")
    print(f"Theme: {theme['label']}")
    if input(f"Type {config.PG_DB} to back up and reassign issue themes: ").strip() != config.PG_DB:
        print("Cancelled; database was not changed.")
        return False

    embedding = bedrock_client.embed(f"{theme['label']}. {theme['description']}")
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("LOCK TABLE issues, themes IN SHARE ROW EXCLUSIVE MODE")
            cur.execute(
                "SELECT theme_id, description, embedding::text FROM themes WHERE label = %s",
                (theme["label"],),
            )
            old_theme = cur.fetchone()
            if old_theme is None:
                raise ValueError(f"Theme is not seeded in the database: {theme['label']}")

            cur.execute("SELECT issue_id, theme_id FROM issues ORDER BY issue_id")
            old_assignments = cur.fetchall()
            backup = {
                "created_at": datetime.now().isoformat(),
                "database": {
                    "host": config.PG_HOST,
                    "port": config.PG_PORT,
                    "name": config.PG_DB,
                },
                "theme": {
                    "theme_id": old_theme[0],
                    "label": theme["label"],
                    "description": old_theme[1],
                    "embedding": old_theme[2],
                },
                "issue_theme_ids": [
                    {"issue_id": issue_id, "theme_id": theme_id}
                    for issue_id, theme_id in old_assignments
                ],
            }
            backup_path = Path(gettempdir()) / (
                f"mrt_issue_theme_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
            )
            backup_path.write_text(json.dumps(backup, indent=2), encoding="utf-8")

            cur.execute(
                "UPDATE themes SET description = %s, embedding = %s WHERE theme_id = %s",
                (theme["description"], embedding, old_theme[0]),
            )
            matched = cluster_themes.assign_themes(cur, "issues")
            cur.execute("SELECT issue_id, theme_id FROM issues ORDER BY issue_id")
            new_assignments = dict(cur.fetchall())
            old_assignment_map = dict(old_assignments)
            changed = sum(
                old_assignment_map[issue_id] != theme_id
                for issue_id, theme_id in new_assignments.items()
            )
            cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE theme_id IS NULL) FROM issues")
            issue_count, unassigned_count = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"Backup: {backup_path}")
    print(f"Issues reassigned: {issue_count}; assignments changed: {changed}")
    print(f"Issues meeting the assignment threshold: {len(matched)}")
    print(f"Issues left unassigned: {unassigned_count}")
    print("Only the selected theme and issue assignments were updated.")
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--theme", default=DEFAULT_THEME,
        help=f"Risk theme label to refresh (default: {DEFAULT_THEME})",
    )
    args = parser.parse_args(argv)
    try:
        reassign_issues(load_risk_theme(args.theme))
    except Exception as exc:
        print(f"Reassignment stopped; no changes committed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())