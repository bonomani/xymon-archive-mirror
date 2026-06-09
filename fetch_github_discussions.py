#!/usr/bin/env python3
"""Fetch GitHub Discussions into the same SQLite store.

Uses the GitHub GraphQL API via the ``gh`` CLI (so auth is whatever ``gh``
already has, or a GITHUB_TOKEN env var). Each discussion becomes a thread:
opening post + comments + replies, mapped to the ``message`` schema by
``mailstore.gh_discussion_rows`` and deduped by GraphQL node ID.

    python3 fetch_github_discussions.py --repo owner/name
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import mailstore

QUERY = """
query($owner:String!, $repo:String!, $cursor:String) {
  repository(owner:$owner, name:$repo) {
    discussions(first:25, after:$cursor,
                orderBy:{field:CREATED_AT, direction:ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id title createdAt body bodyHTML
        author { login }
        category { name }
        comments(first:100) {
          nodes {
            id createdAt body bodyHTML
            author { login }
            replyTo { id }
            replies(first:100) {
              nodes {
                id createdAt body bodyHTML
                author { login }
                replyTo { id }
              }
            }
          }
        }
      }
    }
  }
}
"""


def gh_graphql(owner: str, repo: str, cursor: str | None) -> dict:
    cmd = ["gh", "api", "graphql", "-f", f"query={QUERY}",
           "-F", f"owner={owner}", "-F", f"repo={repo}"]
    if cursor:
        cmd += ["-F", f"cursor={cursor}"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def fetch(conn, owner: str, repo: str) -> tuple[int, int]:
    """Page through all discussions. Returns (threads, messages_added)."""
    cursor, threads, added = None, 0, 0
    while True:
        data = gh_graphql(owner, repo, cursor)
        d = data["data"]["repository"]["discussions"]
        for disc in d["nodes"]:
            rows = mailstore.gh_discussion_rows(disc)
            added += mailstore.insert_rows(conn, rows)
            threads += 1
            capped = disc.get("comments", {}).get("nodes", [])
            if len(capped) >= 100:
                print(f"  ! discussion '{disc['title'][:40]}' hit the "
                      "100-comment page cap; some replies may be missing")
        if not d["pageInfo"]["hasNextPage"]:
            break
        cursor = d["pageInfo"]["endCursor"]
    return threads, added


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch GitHub Discussions")
    ap.add_argument("--db", default="archive.db", type=Path)
    ap.add_argument("--repo", required=True, help="owner/name")
    args = ap.parse_args()
    if "/" not in args.repo:
        ap.error("--repo must be owner/name")
    owner, repo = args.repo.split("/", 1)

    conn = mailstore.connect(args.db)
    threads, added = fetch(conn, owner, repo)
    conn.close()
    print(f"{args.repo}: {threads} discussion(s), {added} message(s) added")


if __name__ == "__main__":
    main()
