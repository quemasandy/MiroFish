#!/usr/bin/env python3
"""Export a public DeepWiki codebase wiki into local Markdown files.

DeepWiki embeds the full wiki structure and each page's Markdown content inside
the initial HTML response. This script reconstructs that data stream and writes
it to a local documentation folder.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List
from urllib.parse import quote
from urllib.request import Request, urlopen


NEXT_PUSH_RE = re.compile(r"self\.__next_f\.push\((.*?)\)</script>", re.S)
CHUNK_RE = re.compile(r"^([0-9a-z]+):(.*)$")
PAGE_RE = re.compile(
    r'"page_plan":\{"id":"([^"]+)","title":"([^"]+)"\},"content":"\$([0-9a-z]+)"'
)
REPO_NAME_RE = re.compile(r'"repo_name":"([^"]+)"')
COMMIT_HASH_RE = re.compile(r'"commit_hash":"([^"]+)"')
GENERATED_AT_RE = re.compile(r'"generated_at":"([^"]+)"')
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]*)\)")
SOURCE_REF_RE = re.compile(r"^(.+?):(\d+)(?:-(\d+))?$")


@dataclass(frozen=True)
class Page:
    page_id: str
    title: str
    chunk_id: str

    @property
    def filename(self) -> str:
        return f"{self.page_id.replace('.', '-')}-{slugify(self.title)}.md"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a public DeepWiki wiki into local Markdown files."
    )
    parser.add_argument(
        "url",
        help="DeepWiki URL, for example https://deepwiki.com/666ghj/MiroFish",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="docs/deepwiki",
        help="Output directory for the generated Markdown files.",
    )
    return parser.parse_args()


def fetch_html(url: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; MiroFish DeepWiki exporter/1.0)"
        },
    )
    with urlopen(request) as response:
        return response.read().decode("utf-8")


def extract_push_payloads(html: str) -> List[str]:
    payloads: List[str] = []
    for match in NEXT_PUSH_RE.findall(html):
        data = json.loads(match)
        if len(data) >= 2 and isinstance(data[1], str):
            payloads.append(data[1])
    if not payloads:
        raise ValueError("No DeepWiki flight payloads were found in the HTML.")
    return payloads


def parse_flight_stream(payloads: Iterable[str]) -> tuple[Dict[str, str], Dict[str, str]]:
    chunks: Dict[str, str] = {}
    text_chunks: Dict[str, str] = {}
    pending_text_chunk: str | None = None

    for payload in payloads:
        if pending_text_chunk is not None:
            text_chunks[pending_text_chunk] = payload
            pending_text_chunk = None
            continue

        for line in payload.splitlines():
            if not line:
                continue
            match = CHUNK_RE.match(line)
            if not match:
                continue
            chunk_id, value = match.groups()
            chunks[chunk_id] = value
            if value.startswith("T") and value.endswith(","):
                if pending_text_chunk is not None:
                    raise ValueError("Encountered nested text chunk markers.")
                pending_text_chunk = chunk_id

    if pending_text_chunk is not None:
        raise ValueError("DeepWiki flight stream ended mid-text chunk.")

    return chunks, text_chunks


def extract_pages(chunks: Dict[str, str]) -> List[Page]:
    seen: set[str] = set()
    pages: List[Page] = []
    for value in chunks.values():
        if "page_plan" not in value:
            continue
        for page_id, title, chunk_id in PAGE_RE.findall(value):
            key = f"{page_id}:{chunk_id}"
            if key in seen:
                continue
            seen.add(key)
            pages.append(Page(page_id=page_id, title=title, chunk_id=chunk_id))
    if not pages:
        raise ValueError("No page metadata was found in the DeepWiki payloads.")
    return sorted(pages, key=lambda page: tuple(int(part) for part in page.page_id.split(".")))


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return slug.strip("-") or "page"


def github_blob_base(repo_name: str | None, commit_hash: str | None) -> str | None:
    if not repo_name:
        return None
    if commit_hash:
        return f"https://github.com/{repo_name}/blob/{commit_hash}"
    return f"https://github.com/{repo_name}"


def quote_github_path(path: str) -> str:
    return quote(path, safe="/")


def source_reference_url(label: str, blob_base: str | None) -> str | None:
    if not blob_base:
        return None
    match = SOURCE_REF_RE.match(label)
    if not match:
        return None
    source_path, start_line, end_line = match.groups()
    url = f"{blob_base}/{quote_github_path(source_path)}#L{start_line}"
    if end_line:
        url += f"-L{end_line}"
    return url


def rewrite_links(content: str, page_targets: Dict[str, str], blob_base: str | None) -> str:
    def replace(match: re.Match[str]) -> str:
        label, href = match.groups()
        if href.startswith("#"):
            page_id = href[1:]
            target = page_targets.get(page_id)
            if target:
                return f"[{label}]({target})"
            return match.group(0)

        if not href:
            target = source_reference_url(label, blob_base)
            if target:
                return f"[{label}]({target})"
            return match.group(0)

        if re.match(r"^(?:[a-z]+:)?//", href) or href.startswith("mailto:"):
            return match.group(0)

        if blob_base:
            anchor = ""
            path = href
            if "#" in href:
                path, anchor = href.split("#", 1)
                anchor = f"#{anchor}"
            return f"[{label}]({blob_base}/{quote_github_path(path)}{anchor})"

        return match.group(0)

    return MD_LINK_RE.sub(replace, content)


def write_pages(
    output_dir: Path,
    pages: List[Page],
    text_chunks: Dict[str, str],
    blob_base: str | None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    page_targets = {page.page_id: page.filename for page in pages}

    for page in pages:
        content = text_chunks.get(page.chunk_id)
        if content is None:
            raise ValueError(f"Missing text chunk {page.chunk_id} for page {page.page_id}.")
        rewritten = rewrite_links(content, page_targets, blob_base).rstrip() + "\n"
        (output_dir / page.filename).write_text(rewritten, encoding="utf-8")


def write_index(
    output_dir: Path,
    pages: List[Page],
    source_url: str,
    repo_name: str | None,
    commit_hash: str | None,
    generated_at: str | None,
) -> None:
    exported_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    lines = [
        "# DeepWiki Export",
        "",
        f"- Source: [{source_url}]({source_url})",
    ]
    if repo_name:
        lines.append(f"- Repository: `{repo_name}`")
    if commit_hash:
        lines.append(f"- Indexed commit: `{commit_hash}`")
    if generated_at:
        lines.append(f"- DeepWiki generated at: `{generated_at}`")
    lines.extend(
        [
            f"- Exported at: `{exported_at}`",
            "",
            "## Pages",
            "",
        ]
    )
    lines.extend(f"- [{page.page_id} {page.title}]({page.filename})" for page in pages)
    lines.append("")
    (output_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    html = fetch_html(args.url)
    payloads = extract_push_payloads(html)
    joined_payloads = "\n".join(payloads)
    chunks, text_chunks = parse_flight_stream(payloads)
    pages = extract_pages(chunks)

    repo_name_match = REPO_NAME_RE.search(joined_payloads)
    commit_hash_match = COMMIT_HASH_RE.search(joined_payloads)
    generated_at_match = GENERATED_AT_RE.search(joined_payloads)
    repo_name = repo_name_match.group(1) if repo_name_match else None
    commit_hash = commit_hash_match.group(1) if commit_hash_match else None
    generated_at = generated_at_match.group(1) if generated_at_match else None
    blob_base = github_blob_base(repo_name, commit_hash)

    output_dir = Path(args.output)
    write_pages(output_dir, pages, text_chunks, blob_base)
    write_index(output_dir, pages, args.url, repo_name, commit_hash, generated_at)

    print(
        f"Exported {len(pages)} pages from {args.url} to {output_dir}",
        file=sys.stdout,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
