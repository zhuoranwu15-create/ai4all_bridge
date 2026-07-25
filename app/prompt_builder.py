"""旧 prompt builder import 路径的兼容 façade。"""

import sys

from app.agent_runtime.context import prompt_builder as _implementation

sys.modules[__name__] = _implementation
