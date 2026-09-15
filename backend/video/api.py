"""Local video routes with exclusive output and WebSocket ownership."""
import asyncio
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import secrets
import threading

from fastapi import File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .csv_timeline import MAX_CSV_BYTES
from .output import GameLoopVideoOutput
from .source import VideoSourceStore
from .waveforms import resolve_video_plan

_HANDOFF = ('/api/manual', '/api/estop', '/api/resume', '/api/session', '/api/replays',
            '/api/story', '/api/autopilot', '/api/chat', '/api/device', '/api/dlc/import',
            '/api/character/profile', '/api/history/clear')


def install_video_routes(app, state, root: Path, *, session_factory=None):
    """Install once; storage is created only on the first source operation."""
    store = None
    mode_lock = asyncio.Lock()
    socket_owner = None
    store_lock = threading.Lock()
    binding_sources = set()
    binding_versions = {}
    cleanup_tasks = set()
    retired_sessions = {}
    state.video_session = None

    def source_store():
        nonlocal store
        with store_lock:
            if store is None:
                store = VideoSourceStore(Path(root))
        return store

    def active(session_id=None):
        session = state.video_session
        if (session is None or session.closed or
                (session_id is not None and session.state()['session_id'] != session_id)):
            raise HTTPException(404, 'video session not found')
        return session

    async def close_current(reason):
        session = state.video_session
        if session is not None:
            result = await session.close(reason)
            remember(result)
            return result

    def remember(result):
        retired_sessions[result['session_id']] = result
        if len(retired_sessions) > 128:
            retired_sessions.pop(next(iter(retired_sessions)))

    def preempt_current():
        session = state.video_session
        if session is not None and not session.closed:
            session.output.preempt()

    async def storage_call(operation, *args):
        try:
            task = asyncio.create_task(asyncio.to_thread(operation, *args))
            cancelled = False
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            result = task.result()
            if cancelled:
                raise asyncio.CancelledError()
            return result
        except FileNotFoundError as exc:
            raise HTTPException(404, 'video source not found') from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(409, 'video storage could not be verified') from exc

    @app.middleware('http')
    async def handoff(request, call_next):
        path = request.url.path.rstrip('/')
        if request.method == 'POST' and path == '/api/estop':
            session = state.video_session
            if session is not None and not session.closed:
                session.interrupt('emergency_stop')
                cleanup = asyncio.create_task(session.close('emergency_stop'), name='video-estop-cleanup')
                cleanup_tasks.add(cleanup)
                def completed(task):
                    cleanup_tasks.discard(task)
                    if not task.cancelled():
                        task.exception()
                cleanup.add_done_callback(completed)
            return await call_next(request)
        if (request.method == 'POST' and not path.startswith('/api/video')
                and any(path == prefix or path.startswith(prefix + '/') for prefix in _HANDOFF)):
            preempt_current()
            async with mode_lock:
                try:
                    await close_current('mode_changed')
                except (RuntimeError, OSError):
                    # Safety endpoints must still run their own priority clear.
                    if path not in ('/api/estop', '/api/resume'):
                        return JSONResponse({'error': 'video output clear failed'}, status_code=409)
                return await call_next(request)
        return await call_next(request)

    @app.post('/api/video/sources')
    async def upload_video(file: UploadFile = File(...), duration_ms: int = Form(...)):
        async def uploaded_chunks():
            while chunk := await file.read(1024 * 1024):
                yield chunk
        async with mode_lock:
            await close_current('source_changed')
        try:
            sources = await storage_call(source_store)
            source = await sources.import_stream(file.filename or 'video', uploaded_chunks(), duration_ms)
            return asdict(source)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(409, 'video storage could not be verified') from exc
        finally:
            await file.close()

    @app.post('/api/video/sources/{source_id}/csv')
    async def upload_csv(source_id: str, file: UploadFile = File(...)):
        try:
            payload = await file.read(MAX_CSV_BYTES + 1)
            if len(payload) > MAX_CSV_BYTES:
                raise HTTPException(413, 'CSV exceeds size limit')
            async with mode_lock:
                if source_id in binding_sources:
                    raise HTTPException(409, 'CSV import already in progress')
                await close_current('csv_changed')
                binding_sources.add(source_id)
                binding_versions[source_id] = binding_versions.get(source_id, 0) + 1
            try:
                timeline = await storage_call(lambda: source_store().bind_csv(source_id, payload))
            finally:
                binding_sources.discard(source_id)
            return {'csv_sha256': timeline.sha256, 'row_count': len(timeline.intervals)}
        finally:
            await file.close()

    @app.post('/api/video/sessions')
    async def start_session(body: dict):
        if set(body) != {'source_id'} or not isinstance(body['source_id'], str):
            raise HTTPException(400, 'source_id is required')
        source_id = body['source_id']
        version = binding_versions.get(source_id, 0)
        source = await storage_call(lambda: source_store().get(source_id))
        timeline = await storage_call(lambda: source_store().bound_csv(source_id))
        if timeline is None:
            raise HTTPException(409, 'import a validated CSV before playback')
        presets = deepcopy(state.safety.presets)
        def prepare_plan():
            identity = hashlib.sha256(json.dumps(
                [(name, presets[name]) for name in sorted(presets)],
                sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            return resolve_video_plan(timeline, allowed=tuple(sorted(presets)),
                                      library_sha256=identity, seed=secrets.randbits(64))
        try:
            plan = await asyncio.to_thread(prepare_plan)
        except (ValueError, TypeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        plan_id = await storage_call(lambda: source_store().save_plan(source_id, plan))
        async with mode_lock:
            if source_id in binding_sources or version != binding_versions.get(source_id, 0):
                raise HTTPException(409, 'CSV changed while starting video; retry playback')
            if presets != state.safety.presets:
                raise HTTPException(409, 'waveform library changed; retry playback')
            async with state.timeline_transition_lock:
                if (getattr(state, 'story_runtime_owner', None) is not None
                        or getattr(state, 'story_planning_task', None) is not None
                        or getattr(state, 'story_preparation_task', None) is not None):
                    raise HTTPException(409, 'finish or cancel story preparation before video playback')
                try:
                    await close_current('session_replaced')
                    await state.loop.stop_timeline_session()
                    if hasattr(state, 'set_sensors'):
                        await state.set_sensors(False)
                    factory = session_factory
                    if factory is None:
                        from .session import VideoSession
                        factory = VideoSession
                    session = factory(plan, GameLoopVideoOutput(state.loop), source_id=source.source_id,
                                      duration_ms=source.duration_ms, timeline=timeline,
                                      dry_run=bool(state.cfg.get('app', {}).get('dry_run', True)))
                    state.video_session = session
                    result = await session.start()
                    return dict(result, plan_id=plan_id)
                except (ValueError, TypeError, RuntimeError, OSError) as exc:
                    await close_current('start_failed')
                    raise HTTPException(409, str(exc)) from exc

    @app.get('/api/video/state')
    async def current_state():
        session = state.video_session
        return session.state() if session is not None else {'session': None}

    @app.post('/api/video/sessions/{session_id}/stop')
    async def stop_session(session_id: str):
        session = state.video_session
        if session is not None and session.state()['session_id'] == session_id and not session.closed:
            session.interrupt('operator_stop')
        async with mode_lock:
            current = state.video_session
            if current is not None and current.state()['session_id'] == session_id:
                result = await current.close('operator_stop')
                remember(result)
                return result
            return retired_sessions.get(session_id, {'session_id': session_id, 'status': 'ended'})

    @app.websocket('/api/video/sessions/{session_id}/clock')
    async def clock_socket(socket: WebSocket, session_id: str):
        nonlocal socket_owner
        try:
            session = active(session_id)
        except HTTPException:
            await socket.close(code=1008)
            return
        if socket_owner is not None:
            await socket.close(code=1008)
            return
        socket_owner = socket
        await socket.accept()
        errors = asyncio.Queue(maxsize=1)
        changed = asyncio.Event()

        async def publish():
            while not session.closed and state.video_session is session:
                payload = errors.get_nowait() if not errors.empty() else session.state()
                await socket.send_json(payload)
                changed.clear()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(changed.wait(), timeout=0.1)
            await socket.close(code=1000)

        publisher = asyncio.create_task(publish(), name='video-clock-state')
        try:
            while not session.closed and state.video_session is session:
                try:
                    observation = await socket.receive_json()
                    if not isinstance(observation, dict):
                        raise ValueError('observation must be an object')
                    await session.observe(observation)
                    changed.set()
                except (ValueError, TypeError) as exc:
                    if not errors.empty():
                        errors.get_nowait()
                    errors.put_nowait({'error': str(exc)})
                    changed.set()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            publisher.cancel()
            with suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await publisher
            if state.video_session is session and not session.closed:
                session.interrupt('socket_disconnected')
            async with mode_lock:
                if state.video_session is session:
                    remember(await session.close('socket_disconnected'))
            if socket_owner is socket:
                socket_owner = None

    @app.on_event('shutdown')
    async def shutdown_video():
        await close_current('shutdown')
        if cleanup_tasks:
            await asyncio.gather(*tuple(cleanup_tasks), return_exceptions=True)
        if store is not None:
            await asyncio.to_thread(store.close)
