"""FastAPI application exposing the tariff-group supply API."""
from __future__ import annotations

import os

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, RedirectResponse

from .schemas import (
    BaseIn,
    BaseOut,
    ChangeRecord,
    CountryDetailIn,
    CountryDetailOut,
    DetailListOut,
    MappingIn,
    MappingOut,
    OriginOut,
    SummaryOut,
    VersionListOut,
)
from .store import ApiError, Store

DESCRIPTION = """新版税则拆并税号后的供数 API。

* 先 `PUT /base` 固定相邻两年各税号进口额（整数分），有版本后底表即锁定；
* 再 `POST /mappings` 按递增版号提交**完整**新旧税号对照（同号也按年份区分，
  未变税号以 `(code, code)` 出现）；
* 经对照直接或间接相连的税号归为一组，`GET /summary` 给出现行汇总
  （组号、两侧税号、各年合计）；
* `GET /changes/{version}` 按版取供数变更：首版全量，后续给被替换版号、
  应撤下组号、应写入汇总；
* 对照漏掉底表税号或引用未知税号时整版拒收（422），原供数仍可查询；
* 同版重传原对照返回当时的变更（200），改内容则冲突（409）。

首版对照发布后开放**来源国判定**：

* `PUT /details/{year}/{code}` 按年份和税号替换国别明细：已确认的国别金额 +
  每笔待核金额及其候选国家（金额均为非负整数分，待核金额可分摊给候选国家）；
  明细合计须等于底表，不等则 422 且保留上次明细；
* `GET /groups/{group_id}/origin` 按现行组号逐年判定：给出在所有可行分摊下
  都独占首位的国家；无法判定时给出一份首位并列的分摊、或两份首位不同的分摊，
  供分析员追查；组内该年底表总额为零报无进口，总额非零而缺明细则列出缺项；
* 一次查询的版号、组成员和国别明细取自同一已提交状态（单个 SQLite 读事务），
  分摊计算在事务外进行，期间其他实例可继续更正明细和发布对照。
"""


def create_app(db_path: str) -> FastAPI:
    app = FastAPI(title="税则税号分组供数 API", version="1.0.0", description=DESCRIPTION)
    store = Store(db_path)
    app.state.store = store

    @app.exception_handler(ApiError)
    async def api_error_handler(_, exc: ApiError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok"}

    # ------------------------------------------------------------- 底表
    @app.put(
        "/base",
        response_model=BaseOut,
        tags=["底表"],
        summary="固定相邻两年各税号进口额（整数分）",
    )
    def put_base(payload: BaseIn):
        amounts = [(a.year, a.code, a.amount_cents) for a in payload.amounts]
        return store.put_base(amounts)

    @app.get("/base", tags=["底表"], summary="查看当前底表")
    def get_base():
        return store.get_base()

    # ----------------------------------------------------------- 对照版本
    @app.post(
        "/mappings",
        response_model=ChangeRecord,
        status_code=201,
        tags=["对照版本"],
        summary="按递增版号提交完整新旧税号对照",
        description="新版权返回 201 及本版变更；同版同内容重传返回 200 及当时的变更；"
        "同版不同内容返回 409；漏报/引用未知税号返回 422 且整版拒收。",
    )
    def submit_mapping(payload: MappingIn, response: Response):
        pairs = [(e.old_code, e.new_code) for e in payload.entries]
        record, created = store.submit_mapping(payload.version, pairs)
        if not created:
            response.status_code = 200
        return record

    @app.get(
        "/mappings/{version}",
        response_model=MappingOut,
        tags=["对照版本"],
        summary="查看某版对照内容",
    )
    def get_mapping(version: int):
        mapping = store.get_mapping(version)
        if mapping is None:
            raise ApiError(404, f"mapping version {version} not found")
        return mapping

    @app.get(
        "/versions",
        response_model=VersionListOut,
        tags=["对照版本"],
        summary="列出全部对照版本",
    )
    def list_versions():
        return store.list_versions()

    # --------------------------------------------------------------- 供数
    @app.get(
        "/summary",
        response_model=SummaryOut,
        tags=["供数"],
        summary="现行汇总：组号、两侧税号、各年合计",
    )
    def get_summary():
        return store.get_summary()

    @app.get(
        "/changes/{version}",
        response_model=ChangeRecord,
        tags=["供数"],
        summary="按版取供数变更",
        description="首版给全量；后续给被替换的版号、应撤下的组号和应写入的汇总。",
    )
    def get_change(version: int):
        record = store.get_change(version)
        if record is None:
            raise ApiError(404, f"version {version} not found")
        return record

    # ----------------------------------------------------------- 国别明细
    @app.put(
        "/details/{year}/{code}",
        response_model=CountryDetailOut,
        tags=["国别明细"],
        summary="替换某年某税号的国别明细（首版对照发布后）",
        description="填入已确认的国别金额与每笔待核金额及候选国家；合计须等于底表，"
        "不等则 422 且保留上次明细；未发布对照时 409，税号不在底表时 404。",
    )
    def put_detail(year: int, code: str, payload: CountryDetailIn):
        return store.put_detail(
            year,
            code.strip(),
            payload.confirmed,
            [item.model_dump() for item in payload.pending],
        )

    @app.get(
        "/details/{year}/{code}",
        response_model=CountryDetailOut,
        tags=["国别明细"],
        summary="查看某年某税号的国别明细",
    )
    def get_detail(year: int, code: str):
        return store.get_detail(year, code.strip())

    @app.get(
        "/details",
        response_model=DetailListOut,
        tags=["国别明细"],
        summary="列出全部国别明细",
    )
    def list_details():
        return store.list_details()

    # ---------------------------------------------------------- 来源国判定
    @app.get(
        "/groups/{group_id}/origin",
        response_model=OriginOut,
        tags=["来源国判定"],
        summary="按现行组号判定来源国",
        description="逐年给出在所有可行分摊下都独占首位的国家；无法判定时给出一份"
        "首位并列的分摊、或两份首位不同的分摊；总额为零报无进口，缺明细列出缺项。"
        "版号、组成员、国别明细取自同一已提交状态。",
    )
    def get_group_origin(group_id: int):
        return store.get_group_origin(group_id)

    return app


app = create_app(os.environ.get("DB_PATH", "data/app.db"))
