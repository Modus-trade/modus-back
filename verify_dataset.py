"""Compare the demo JSON with the 20 sample appeals in the supplied DOCX.

Usage: python verify_dataset.py path/to/Датасет.docx [--sync]
Only the source fields are compared. Dates, people and routing are demo additions.
"""

import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile


def source_rows(path: Path) -> list[dict[str, str]]:
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = [
        "".join(node.itertext()).strip()
        for node in root.findall(".//w:body//w:p", ns)
    ]
    paragraphs = [line for line in paragraphs if line]
    rows = []
    for index, line in enumerate(paragraphs):
        heading = re.fullmatch(r"(A-\d{3}) · (.+?) · (.+)", line)
        if not heading:
            continue
        if index + 3 >= len(paragraphs):
            raise ValueError(f"Incomplete record {heading.group(1)}")
        metadata = re.fullmatch(
            r"Автор:\s*(.*?)\s+Приоритет:\s*(.*?)\s+Статус:\s*(.*)",
            paragraphs[index + 1],
        )
        action = re.fullmatch(
            r"Условный ответственный:\s*(.*?)\s+Пример дальнейшего действия:\s*(.*)",
            paragraphs[index + 3],
        )
        description = paragraphs[index + 2]
        if not metadata or not action or not description.startswith("Текст обращения. "):
            raise ValueError(f"Unexpected source format for {heading.group(1)}")
        rows.append(
            {
                "number": heading.group(1),
                "type": heading.group(2),
                "category": heading.group(3),
                "author": metadata.group(1),
                "priority": metadata.group(2),
                "status": metadata.group(3),
                "description": description.removeprefix("Текст обращения. "),
                "responsible_role": action.group(1),
                "action": action.group(2),
            }
        )
    return rows


def main() -> int:
    if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and sys.argv[2] != "--sync"):
        print("Usage: python verify_dataset.py path/to/Датасет.docx [--sync]", file=sys.stderr)
        return 2
    source = source_rows(Path(sys.argv[1]))
    dataset_path = Path(__file__).parent / "dataset.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    if len(source) != 20 or len(dataset) != 20:
        print(f"Expected 20 records; DOCX={len(source)}, JSON={len(dataset)}", file=sys.stderr)
        return 1
    if len(sys.argv) == 3:
        if [row["number"] for row in source] != [row["number"] for row in dataset]:
            raise ValueError("Source and JSON record order differs")
        for original, imported in zip(source, dataset):
            imported["responsible_role"] = original["responsible_role"]
        dataset_path.write_text(
            json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    for original, imported in zip(source, dataset):
        for key, value in original.items():
            if imported.get(key) != value:
                print(
                    f"{original['number']} {key}: DOCX={value!r}, JSON={imported.get(key)!r}",
                    file=sys.stderr,
                )
                return 1
    print("20/20 source records match backend/dataset.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
