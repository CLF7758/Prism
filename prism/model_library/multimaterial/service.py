"""Unavailable facade for handoff. Does not simulate any successful operation.

AI 1 owns the real coordinator after the documented storage/runtime gates pass.
No disk, Qt, native runtime, thread or project mutation occurs in this scaffold.
"""
from ..contracts import ModelFeatureUnavailable
from .contracts import MaterialCapabilityReport, MaterialEditRequest, MaterialTarget


class MultiMaterialService:
    def capabilities(self, target: MaterialTarget | None = None,
                     working_ids: tuple[str, ...] = ()) -> MaterialCapabilityReport:
        return MaterialCapabilityReport()

    def inspect(self, target: MaterialTarget) -> str:
        raise ModelFeatureUnavailable('多材质解析尚未实现')

    def preview(self, request: MaterialEditRequest, draft_generation: int) -> str:
        raise ModelFeatureUnavailable('按材质预览尚未实现')

    def commit(self, request: MaterialEditRequest) -> str:
        raise ModelFeatureUnavailable('多材质事务提交尚未实现')

    def cancel(self, job_id: str) -> None:
        raise ModelFeatureUnavailable('多材质任务调度尚未实现')
