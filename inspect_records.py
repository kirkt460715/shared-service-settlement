"""装载本地 JSON 集合并检查交换文件的基本结构。"""
from pathlib import Path
import json
import sys

DATA = Path(__file__).resolve().parent / "data"


def read_collection(name, directory=DATA):
    """返回集合及记录键。文件名使用合同中登记的集合名。"""
    if not name.replace("_", "").isalnum():
        raise ValueError("集合名不合法")
    with (directory / f"{name}.json").open(encoding="utf-8") as source:
        content = json.load(source)
    fields = content["fields"]
    key = content["key"]
    rows = content["rows"]
    if key not in fields or len(fields) != len(set(fields)) or not isinstance(rows, list):
        raise ValueError(f"{name} 的字段登记无效")
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.keys() != set(fields):
            raise ValueError(f"{name} 有不符合字段登记的记录")
        if not all(isinstance(value, str) for value in row.values()):
            raise ValueError(f"{name} 的字段值必须是字符串")
        if not row[key] or row[key] in seen:
            raise ValueError(f"{name} 的记录键缺失或重复")
        seen.add(row[key])
    return rows, key


def main():
    """逐表查看资料能否读取及记录数。"""
    for path in sorted(DATA.glob("*.json")):
        rows, _ = read_collection(path.stem)
        print(f"{path.stem}: {len(rows)} 条记录")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from error
