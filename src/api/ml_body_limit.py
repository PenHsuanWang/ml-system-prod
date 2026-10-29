"""Bound HTTP bodies, including chunked requests, before JSON validation."""


class BodyLimitMiddleware:
    def __init__(self, app, *, max_bytes: int = 65536) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from starlette.responses import JSONResponse
        messages = []
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            size += len(message.get("body", b""))
            if size > self.max_bytes:
                await JSONResponse(status_code=413, content={"code": "PAYLOAD_TOO_LARGE",
                                                             "message": "Request body is too large", "retryable": False})(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        async def replay():
            return messages.pop(0) if messages else await receive()
        await self.app(scope, replay, send)
