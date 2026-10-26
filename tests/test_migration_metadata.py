import json
from pathlib import Path
import subprocess
import sys


def test_migration_metadata_contains_all_tables() -> None:
    project_root = Path(__file__).resolve().parents[1]
    script = """
import ast
import json
from pathlib import Path
import sys

env_path = Path(sys.argv[1])
tree = ast.parse(env_path.read_text(encoding="utf-8"))
imports = [
    node
    for node in tree.body
    if isinstance(node, ast.ImportFrom)
    and node.module is not None
    and node.module.startswith("newtalk.")
]
namespace = {}
exec(compile(ast.Module(body=imports, type_ignores=[]), str(env_path), "exec"), namespace)
print(json.dumps(sorted(namespace["Base"].metadata.tables)))
"""
    # 隔离模型注册的全局状态，防止其他测试提前导入模型而掩盖迁移缺表。
    result = subprocess.run(
        [sys.executable, "-c", script, str(project_root / "migrations" / "env.py")],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )

    assert set(json.loads(result.stdout)) == {
        "devices",
        "identities",
        "dialogue_sessions",
        "dialogue_exchanges",
        "memory_jobs",
        "identity_deletion_jobs",
        "recovery_rate_limits",
    }
