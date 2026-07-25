"""安装朝夕旧 turn_service Python API；仅供根兼容 façade 使用。"""

from app.agent_runtime.turns import service as _service
from app.bootstrap.product_registry import ZHAOXI_APP_ID
from app.products.zhaoxi.application.turn_services import ZHAOXI_TURN_SERVICES


def _legacy_run_turn_for_account(ctx):
    return _service.run_product_turn(ctx, product_services=ZHAOXI_TURN_SERVICES)


def _legacy_build_channel_input_from_openclaw(payload, **kwargs):
    return _service.build_product_channel_input_from_openclaw(
        payload,
        app_id=ZHAOXI_APP_ID,
        **kwargs,
    )


def _legacy_handle_openclaw_turn(payload, **kwargs):
    result = _service.build_channel_input_from_openclaw(payload, **kwargs)
    if isinstance(result, _service.OpenClawTurnResponse):
        return result
    return _service.run_turn_for_account(result)


def _legacy_build_turn_llm_input(**kwargs):
    return _service.build_product_turn_llm_input(
        product_services=ZHAOXI_TURN_SERVICES,
        **kwargs,
    )


_service.run_turn_for_account = _legacy_run_turn_for_account
_service.build_channel_input_from_openclaw = _legacy_build_channel_input_from_openclaw
_service.handle_openclaw_turn = _legacy_handle_openclaw_turn
_service.build_turn_llm_input = _legacy_build_turn_llm_input

LEGACY_TURN_SERVICE_MODULE = _service

__all__ = ["LEGACY_TURN_SERVICE_MODULE"]
