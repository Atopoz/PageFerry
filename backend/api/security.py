"""集中保护会改变本地状态的 API endpoint, 避免把 CORS 当作身份认证。"""

from hmac import compare_digest
from typing import Annotated

from fastapi import Header, HTTPException, Request, status


def require_boot_token(
    request: Request,
    supplied_token: Annotated[
        str | None,
        Header(alias="X-PageFerry-Boot-Token"),
    ] = None,
) -> None:
    """配置 boot token 时使用 constant-time compare 拒绝未授权 write。"""

    configured_token = request.app.state.settings.boot_token
    if configured_token is None:
        return
    expected_token = configured_token.get_secret_value()
    if not expected_token:
        return
    if supplied_token is None or not compare_digest(supplied_token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Invalid boot token."},
        )
