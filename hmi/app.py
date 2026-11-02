"""HTTP and WebSocket interface of the HMI. Routes are async so they share the service's event loop."""

import asyncio
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from hmi.alarms import load_alarm_config
from hmi.logbook import Logbook
from hmi.plant_client import CommError, PlantClient
from hmi.service import HmiService, NothingToAcknowledge
from shared.registers import RegisterMap, load_register_map
from shared.settings import load_settings

MAX_HISTORY = 1000


class SpeedRequest(BaseModel):
    speed: float


class FaultRequest(BaseModel):
    active: bool


def create_app(service: HmiService, registers: RegisterMap, logbook: Logbook) -> FastAPI:
    app = FastAPI(title="Conveyor HMI")
    speed_point = registers["speed_setpoint"]

    async def plant_write(write) -> dict:
        try:
            await write
        except CommError as e:
            raise HTTPException(status_code=503, detail=f"plant unreachable: {e}") from e
        return {"ok": True}

    @app.get("/api/state")
    async def state() -> dict:
        return service.snapshot()

    @app.post("/api/commands/{name}")
    async def command(name: str) -> dict:
        if name not in service.commands:
            raise HTTPException(status_code=404, detail=f"unknown command: {name}")
        return await plant_write(service.send_command(name))

    @app.post("/api/speed")
    async def speed(request: SpeedRequest) -> dict:
        if not speed_point.min <= request.speed <= speed_point.max:
            raise HTTPException(status_code=422, detail=f"speed must be {speed_point.min} to {speed_point.max} {speed_point.unit}")
        return await plant_write(service.set_speed(request.speed))

    @app.post("/api/faults/{name}")
    async def fault(name: str, request: FaultRequest) -> dict:
        if name not in service.faults:
            raise HTTPException(status_code=404, detail=f"unknown fault: {name}")
        return await plant_write(service.set_fault(name, request.active))

    @app.post("/api/alarms/{name}/ack")
    async def acknowledge(name: str) -> dict:
        try:
            service.acknowledge(name)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown alarm: {name}") from None
        except NothingToAcknowledge:
            raise HTTPException(status_code=409, detail=f"{name} has nothing to acknowledge") from None
        return {"ok": True}

    @app.get("/api/alarm-events")
    async def alarm_events(limit: int = Query(100, ge=1, le=MAX_HISTORY)) -> list[dict]:
        return logbook.recent_alarm_events(limit)

    @app.get("/api/operations")
    async def operations(limit: int = Query(100, ge=1, le=MAX_HISTORY)) -> list[dict]:
        return logbook.recent_operations(limit)

    @app.websocket("/ws")
    async def live(websocket: WebSocket) -> None:
        await websocket.accept()
        queue = service.subscribe()
        try:
            await websocket.send_json(service.snapshot())
            while True:
                await websocket.send_json(await queue.get())
        except WebSocketDisconnect:
            pass
        finally:
            service.unsubscribe(queue)

    return app


async def main() -> None:
    settings = load_settings()
    registers = load_register_map()
    logbook = Logbook(Path(settings.hmi_db_path))
    client = PlantClient(settings.modbus_host, settings.modbus_port, registers)
    service = HmiService(client, registers, load_alarm_config(registers), logbook)
    app = create_app(service, registers, logbook)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.hmi_host, port=settings.hmi_port,
                                           timeout_graceful_shutdown=2))
    await service.start()
    serving = asyncio.create_task(server.serve())
    polling = asyncio.create_task(service.wait())
    try:
        await asyncio.wait({serving, polling}, return_when=asyncio.FIRST_COMPLETED)
        # A dead poll loop means a page of stale data, so take the web server down with it.
        server.should_exit = True
        await serving
        if polling.done():
            polling.result()
    finally:
        polling.cancel()
        await service.stop()
        logbook.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
