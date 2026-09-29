import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


logger = logging.getLogger("webapp.api")


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, field_errors: dict | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.field_errors = field_errors


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def api_error(_: Request, exc: APIError):
        body = {"code": exc.code, "message": exc.message}
        if exc.field_errors:
            body["field_errors"] = exc.field_errors
        return JSONResponse(status_code=exc.status, content=body)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        fields: dict[str, str] = {}
        for item in exc.errors():
            fields[".".join(str(part) for part in item["loc"] if part not in {"body", "query"})] = item["msg"]
        return JSONResponse(status_code=422, content={
            "code": "validation_error", "message": "Проверьте заполнение формы", "field_errors": fields,
        })

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        message = exc.detail if isinstance(exc.detail, str) else "Запрос не выполнен"
        return JSONResponse(status_code=exc.status_code, content={"code": "http_error", "message": message})

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        logger.exception("unhandled_api_error", extra={"method": request.method, "path": request.url.path})
        return JSONResponse(status_code=500, content={"code": "internal_error", "message": "Внутренняя ошибка сервера"})
