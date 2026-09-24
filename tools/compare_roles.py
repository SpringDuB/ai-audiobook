"""把新系统的 lines/*.jsonl 与旧系统 roles_*.json 的说话人标注做对照。

用法：
  uv run python tools/compare_roles.py --legacy <旧书籍目录> --book <bookId> --data-dir data
输出：可对照行数与"说话人一致"比例，并列出前 20 条差异（行 id / 旧标注 / 新标注）。
"""

import argparse
import json
from pathlib import Path


def load_legacy(directory: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(directory.glob("roles_*.json")):
        rows.extend(json.loads(path.read_text(encoding="utf-8")))
    return rows


def normalize(text: str) -> str:
    return "".join(char for char in text if char.strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy", required=True)
    parser.add_argument("--book", required=True)
    parser.add_argument("--data-dir", default="data")
    args = parser.parse_args()

    legacy = {normalize(row["text"]): row["role"] for row in load_legacy(Path(args.legacy))}
    lines_dir = Path(args.data_dir) / "books" / args.book / "analysis" / "lines"
    matched = agreed = 0
    mismatches: list[tuple[str, str, str]] = []
    for path in sorted(lines_dir.glob("chapter_*.jsonl")):
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            row = json.loads(raw)
            expected = legacy.get(normalize(row["text"]))
            if expected is None:
                continue
            matched += 1
            mine = row.get("speaker_name") or row.get("speaker")
            if expected == mine or (expected in {"旁白", "叙述"} and row.get("speaker") == "narrator"):
                agreed += 1
            elif len(mismatches) < 20:
                mismatches.append((row["id"], expected, mine))
    rate = (agreed / matched) if matched else 0.0
    print(f"可对照行数 {matched}，说话人一致 {agreed}，一致率 {rate:.1%}")
    for line_id, expected, mine in mismatches:
        print(f"  差异 {line_id}: 旧={expected} 新={mine}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
