"""契约/模型层：主动消息的纯类型与数据定义（无 I/O，零下游依赖）。

分类 registry、`ProactiveCandidate` 候选协议、prompts、纯工具。作为最底层，被召回/选择/
存储/派发共同依赖，自身不依赖任何 proactive 模块，从根上杜绝循环 import。
"""
