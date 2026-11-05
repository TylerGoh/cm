from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cluster_manager.daemon.db.models.operation import Operation


class OperationRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, operation: Operation) -> None:
        await self._session.execute(
            insert(Operation)
            .values(
                agent_ip=operation.agent_ip,
                op_id=operation.op_id,
                generation=operation.generation,
                type=operation.type,
                started_at=operation.started_at,
                finished_at=operation.finished_at,
                failure=operation.failure,
                steps=operation.steps,
            )
            .on_conflict_do_nothing(index_elements=[Operation.agent_ip, Operation.op_id])
        )

    async def for_generation(self, agent_ip: str, generation: int) -> list[Operation]:
        result = await self._session.execute(
            select(Operation)
            .where(Operation.agent_ip == agent_ip, Operation.generation == generation)
            .order_by(Operation.started_at)
        )
        return list(result.scalars())

    async def recent(self, agent_ip: str, limit: int) -> list[Operation]:
        result = await self._session.execute(
            select(Operation)
            .where(Operation.agent_ip == agent_ip)
            .order_by(Operation.started_at.desc())
            .limit(limit)
        )
        return list(result.scalars())
