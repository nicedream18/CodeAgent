"""Check showcase facts and local Markdown links; never run models or rewrite files."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SHOWCASE = Path("docs/internship/showcase")


def pointer_value(value: Any, pointer: str) -> Any:
    if pointer and not pointer.startswith("/"):
        raise ValueError("JSON pointer must start with /")
    for part in pointer.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def local_path(root: Path, base: Path, target: str) -> Path:
    path = (base / unquote(target)).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Link escapes repository")
    if not path.exists():
        raise ValueError("Missing target: " + target)
    return path


def render_facts(facts: list[dict[str, Any]]) -> str:
    lines = ["# 展示事实与证据索引", "", "由 facts.json 生成；检查命令只读，不修改文件。数值对应各自冻结版本，不合并分母。", ""]
    for fact in facts:
        lines += [f"## {fact['id']}：{fact['claim']}", "",
                  f"版本：`{fact['version']}`。范围：{fact['scope']}。", "",
                  "证据："]
        for item in fact["evidence"]:
            path = Path(item["path"])
            relative = Path("../../..") / path
            lines += [f"- [{path.name}]({relative.as_posix()}) · JSON pointer `{item['pointer']}` = `{json.dumps(item['expected'], ensure_ascii=False)}`"]
        lines += ["", "限制：" + fact["limitation"], ""]
    return "\n".join(lines)


def check_facts(root: Path) -> tuple[int, list[str]]:
    errors = []
    data = json.loads((root / SHOWCASE / "facts.json").read_text())
    facts = data["facts"]
    seen = set()
    for fact in facts:
        if fact["id"] in seen:
            errors.append("Duplicate fact: " + fact["id"])
        seen.add(fact["id"])
        for evidence in fact["evidence"]:
            try:
                path = local_path(root, root, evidence["path"])
                if hashlib.sha256(path.read_bytes()).hexdigest() != evidence["sha256"]:
                    raise ValueError("Evidence hash changed")
                actual = pointer_value(json.loads(path.read_text()), evidence["pointer"])
                if type(actual) is not type(evidence["expected"]) or actual != evidence["expected"]:
                    raise ValueError("Evidence value differs from claim")
            except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                errors.append(f"{fact['id']}: {exc}")
    if (root / SHOWCASE / "FACTS.md").read_text() != render_facts(facts):
        errors.append("FACTS.md differs from facts.json")
    return len(facts), errors


def check_links(root: Path, documents: list[Path]) -> tuple[int, list[str]]:
    count, errors = 0, []
    for document in documents:
        # Fenced examples may contain sample URLs that are not document links.
        prose = re.sub(r"```.*?```", "", document.read_text(), flags=re.S)
        for target in re.findall(r"\[[^\]\n]+\]\(([^\s)]+)\)", prose):
            if urlsplit(target).scheme:
                continue  # Remote links are citations, not an HTTP availability check.
            count += 1
            try:
                parts = urlsplit(target)
                path = local_path(root, document.parent, parts.path or document.name)
                if parts.fragment.startswith("L") and parts.fragment[1:].isdigit():
                    if int(parts.fragment[1:]) not in range(1, len(path.read_text().splitlines()) + 1):
                        raise ValueError("Source line out of range")
                elif parts.fragment:
                    raise ValueError("Use file or #L<number> links in checked documents")
            except (OSError, ValueError) as exc:
                errors.append(f"{document.relative_to(root)}: {target}: {exc}")
    return count, errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    docs = [ROOT / "README.md", *sorted((ROOT / "docs/internship").rglob("*.md"))]
    try:
        facts, fact_errors = check_facts(ROOT)
        links, link_errors = check_links(ROOT, docs)
        errors = fact_errors + link_errors
        print(json.dumps({"ok": not errors, "facts": facts, "documents": len(docs),
                          "local_links": links, "errors": errors,
                          "limits": "Checks local targets/line ranges and frozen JSON facts. Diagrams and prose require source review; remote URLs are not probed."},
                         ensure_ascii=False, indent=2))
        raise SystemExit(bool(errors))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Showcase check failed: {exc}\n")


if __name__ == "__main__":
    main()
