"""Companion World product domain layer（朝夕相伴 多居民私人世界）。

分层不变量：本包只经 agent_runtime 端口访问 Agent Runtime，禁止直接
import app.db.* / app.turn_service（由 tests/test_layer_boundaries.py 的
stdlib-AST 边界门禁执行，D-12）。M0 为空骨架，暂无业务逻辑。
"""
