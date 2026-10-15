#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""YY-Flow 项目接入的唯一入口。默认只预览，--apply 才写入。

预览：
  python scripts/setup_project.py --project-root <项目根> --skill-root <代码根> --clients cursor,codex,antigravity

写入：
  python scripts/setup_project.py --project-root <项目根> --skill-root <代码根> --clients cursor,codex,antigravity --apply

诊断、恢复、移除：
  python scripts/setup_project.py --project-root <项目根> --diagnose
  python scripts/setup_project.py --project-root <项目根> --recover
  python scripts/setup_project.py --project-root <项目根> --recover --apply
  python scripts/setup_project.py --project-root <项目根> --remove
  python scripts/setup_project.py --project-root <项目根> --remove --apply

--skip-self-check 只给故障注入使用，不表示真实客户端已经连通，也不能当作 AC-14 通过。
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts._lib.onboarding.engine import main


if __name__ == "__main__":
    raise SystemExit(main())
