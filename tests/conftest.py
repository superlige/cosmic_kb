"""让 tests/ 下的共享辅助模块（如 _synthkb）可被各测试 `import`。"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
