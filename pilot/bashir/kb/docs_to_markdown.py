"""Convert a Claude Docs tab (the XML returned by the Docs `read` tool) into Markdown.

Usage: python3 pilot/bashir/kb/docs_to_markdown.py <read-result.json> <out.md>

The input is the raw tool result of a Docs `read` call: JSON whose `data.xml` holds the tab.
Used once to snapshot the Claude Docs knowledge base (2026-11-20); the seed of the v2 epic
"Raw knowledge import". Rendering rules are declared as data in INLINE and BLOCK.
"""

import json
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

Render = Callable[[ET.Element], str]


def inline(el: ET.Element) -> str:
    """Text content of an inline element with its children rendered."""
    inner = (el.text or "") + "".join(INLINE.get(c.tag, inline)(c) + (c.tail or "") for c in el)
    return inner


INLINE: dict[str, Render] = {
    "text": inline,
    "bold": lambda e: f"**{inline(e)}**",
    "italic": lambda e: f"*{inline(e)}*",
    "code": lambda e: f"`{inline(e)}`",
    "link": lambda e: f"[{inline(e)}]({e.get('href', '')})",
    "mention": lambda e: e.get("label") or e.get("name") or "",
    "date": lambda e: e.get("value", ""),
    "dropdown": inline,
}


def paragraph_text(p: ET.Element) -> str:
    return "".join(INLINE.get(c.tag, inline)(c) for c in p).strip()


def paragraph(p: ET.Element) -> str:
    level = p.get("heading")
    body = paragraph_text(p)
    return f"{'#' * int(level)} {body}" if level else body


def cell(c: ET.Element) -> str:
    return "<br>".join(paragraph_text(p) for p in c.iter("paragraph")).replace("|", "\\|")


def table(t: ET.Element) -> str:
    rows = [[cell(c) for c in r.findall("cell")] for r in t.findall("row")]
    header, body = rows[0], rows[1:]
    lines = [header, ["---"] * len(header), *body]
    return "\n".join(f"| {' | '.join(r)} |" for r in lines)


def list_block(lst: ET.Element, depth: int = 0) -> str:
    kind = lst.get("kind", "bullet")

    def marker(i: int, item: ET.Element) -> str:
        return {
            "ordered": f"{i}.",
            "check": f"- [{'x' if item.get('checked') == 'true' else ' '}]",
        }.get(kind, "-")

    def item_lines(i: int, item: ET.Element) -> list[str]:
        paras = [paragraph_text(p) for p in item.findall("paragraph")]
        nested = [list_block(n, depth + 1) for n in item.findall("list")]
        head = f"{'  ' * depth}{marker(i, item)} {paras[0] if paras else ''}"
        rest = [f"{'  ' * (depth + 1)}{p}" for p in paras[1:]]
        return [head, *rest, *nested]

    return "\n".join(line for i, item in enumerate(lst.findall("listItem"), 1) for line in item_lines(i, item))


def code_block(c: ET.Element) -> str:
    return f"```{c.get('language', '')}\n{''.join(c.itertext()).strip()}\n```"


BLOCK: dict[str, Render] = {
    "paragraph": paragraph,
    "table": table,
    "list": list_block,
    "codeBlock": code_block,
    "embed": lambda e: f"\\[embedded content: {e.get('caption', '')}\\]",
}


def convert(xml: str) -> str:
    doc = ET.fromstring(xml)
    blocks = [BLOCK[b.tag](b) for b in doc if b.tag in BLOCK]
    return "\n\n".join(b for b in blocks if b) + "\n"


def read_xml(path: Path) -> str:
    raw = json.loads(path.read_text(encoding="utf-8"))
    text = raw[0]["text"] if isinstance(raw, list) else json.dumps(raw)
    return json.JSONDecoder().raw_decode(text)[0]["data"]["xml"]


if __name__ == "__main__":
    source, target = Path(sys.argv[1]), Path(sys.argv[2])
    target.write_text(convert(read_xml(source)), encoding="utf-8")
    print(f"{target}: {target.stat().st_size} bytes")
