#!/usr/bin/env python3
"""校验 k8s 清单的 YAML 合法性 —— 重点是**重复键**。

为什么需要这个脚本（真实踩过的坑）：
  一次脚本批量往 CronJob 里追加 `envFrom:` 时，没注意到那个容器已经有
  `envFrom:`（SMTP 凭据）了 → 同一个 key 出现两次。

  - **PyYAML 会宽容接受**（后面的覆盖前面的，静默）
  - **kustomize / k8s 会直接报错**：
        line 49: mapping key "envFrom" already defined at line 45

  结果：我本地校验"全过"，推上去之后 Argo CD 直接卡在 Unknown，
  整个 GitOps 流水线停摆 —— 而报错信息只说第 49 行，看不出真正原因。

所以：**用严格的 loader 在 CI 里卡住重复键**，别让它有机会推到集群。
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("需要 pyyaml：pip install pyyaml")
    sys.exit(3)


class StrictLoader(yaml.SafeLoader):
    """在 SafeLoader 基础上禁止重复键。"""


def _no_duplicate_keys(loader: StrictLoader, node, deep: bool = False):
    mapping: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            mark = key_node.start_mark
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f'发现重复的键 "{key}"（第 {mark.line + 1} 行）—— '
                f'kustomize 会因此拒绝生成 manifest',
                mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys
)


def main(dirs: list[str] | None = None) -> int:
    targets = dirs or ["k8s"]
    files: list[Path] = []
    for d in targets:
        p = Path(d)
        if p.is_dir():
            files.extend(sorted(p.rglob("*.yaml")))
        elif p.is_file():
            files.append(p)
    if not files:
        print(f"没有在 {targets} 里找到 yaml 文件")
        return 0

    bad = 0
    for f in files:
        try:
            list(yaml.load_all(f.read_text(encoding="utf-8"), Loader=StrictLoader))
            print(f"  ✓ {f}")
        except yaml.YAMLError as exc:
            bad += 1
            print(f"  ✗ {f}\n      {exc}")
    print(f"\n检查 {len(files)} 个文件，问题 {bad} 个")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or None))
