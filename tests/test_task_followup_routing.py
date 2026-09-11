"""Routing regressions: real session stores, locks and MCP -> HTTP -> dispatcher."""
import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

import cc_mcp_server
import dispatcher
import http_server
import task_routes
from run_control import ActiveRunRegistry
from session_store import SessionStore

CHAT = 'oc_test'
THREAD = 'omt_child'
KEY = f'{CHAT}:{THREAD}'


class Bot:
    def __init__(self, name, runner, *, shared=True):
        self.profile = NS(name=name, runner=runner, app_id=f'app_{name}',
                          dispatch_model='', default_model='', allowed_group_chat_ids={CHAT})
        self.store = SessionStore(profile=name, default_runner=runner,
                                  shared_thread_sessions=shared)
        self.store._data[f'ou_{name}'] = {'private': {}}
        self.active_runs = ActiveRunRegistry()
        self.locks = {}
        self.feishu = NS(reply_post=AsyncMock(), reply_text=AsyncMock(),
                         reply_card=AsyncMock(return_value='om_card'),
                         list_thread_messages=AsyncMock(),
                         get_message_thread_id=AsyncMock(return_value=THREAD),
                         send_post_to_chat=AsyncMock(return_value='om_root'))

    def _ensure_chat_lock(self, key):
        return self.locks.setdefault(key, asyncio.Lock())


@pytest.fixture
async def setup(monkeypatch):
    caller, target = Bot('spx', 'claude'), Bot('agy', 'agy')
    monkeypatch.setattr(dispatcher, '_bots', {'spx': caller, 'agy': target})
    monkeypatch.setattr(dispatcher, '_DISPATCH_TASKS', set())
    monkeypatch.setattr(dispatcher, '_DISPATCH_CHILDREN', {})
    monkeypatch.setattr(dispatcher, '_DISPATCH_PARENTS', {})
    monkeypatch.setattr(dispatcher, 'build_lark_system_prompt', lambda *a, **kw: f'owner={a[0].name}')
    execute = AsyncMock(return_value='done')
    monkeypatch.setattr(dispatcher, '_run_and_display', execute)
    root = NS(chat_id=CHAT, message_id='om_root', parent_id=None,
              sender=NS(sender_type='app', id='app_agy'))
    caller.feishu.list_thread_messages.return_value = [root]
    await target.store.get_current('ou_agy', KEY)
    yield caller, target, execute, root
    tasks = list(dispatcher._DISPATCH_TASKS)
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def bind():
    return task_routes.bind(chat_id=CHAT, thread_id=THREAD, profile='agy',
                            user_id='ou_agy', anchor='om_root')


async def append(caller, *, stop=False, thread=THREAD, chat=CHAT):
    return await dispatcher.steer_or_append_thread(
        caller, user_id='ou_spx', group_chat_id=chat, thread_id=thread,
        instruction='next step', stop_first=stop)


async def drain():
    await asyncio.wait_for(asyncio.gather(*list(dispatcher._DISPATCH_TASKS)), 2)


@pytest.mark.parametrize('stop', [False, True])
async def test_cross_bot_idle_resumes_target_not_caller(setup, stop):
    caller, target, execute, _ = setup
    bind()
    # Persist a recognizable backend ID without invoking an external runner.
    raw = await target.store.get_current_raw('ou_agy', KEY)
    raw['session_id'] = 'agy-original'
    result = await append(caller, stop=stop)
    await drain()
    assert result['ok'] and result['agent'] == 'agy'
    args = execute.await_args.args
    assert args[0] is target and args[1] == 'ou_agy'
    assert args[6].session_id == 'agy-original' and args[6].runner == 'agy'
    assert not caller.store.has_chat_record('ou_spx', KEY)
    caller.feishu.reply_post.assert_not_awaited()
    target.feishu.reply_post.assert_awaited_once()
    assert '执行者：agy' in target.feishu.reply_post.await_args.kwargs['body_text']


async def test_busy_append_waits_on_target_lock_preserves_context(setup):
    caller, target, execute, _ = setup
    bind()
    lock = target._ensure_chat_lock(KEY)
    await lock.acquire()
    result = await append(caller)
    assert result['queued'] is True  # covers startup: lock held before ActiveRun exists
    await asyncio.sleep(0)
    execute.assert_not_awaited()
    raw = await target.store.get_current_raw('ou_agy', KEY)
    raw['session_id'] = 'finished-current-turn'
    lock.release()
    await drain()
    assert execute.await_args.args[6].session_id == 'finished-current-turn'


async def test_steer_stops_only_target_registry(setup, monkeypatch):
    caller, target, execute, _ = setup
    bind()
    target.active_runs.start_run('ou_agy', KEY, 'om_target_running')
    caller.active_runs.start_run('ou_spx', KEY, 'om_wrong_old_run')
    lock = target._ensure_chat_lock(KEY)
    await lock.acquire()

    async def stop(registry, user, chat, **kwargs):
        assert registry is target.active_runs and user == 'ou_agy' and chat == KEY
        registry.clear_run(user, chat)
        lock.release()
        return True

    stopper = AsyncMock(side_effect=stop)
    monkeypatch.setattr(dispatcher, 'stop_run', stopper)
    result = await append(caller, stop=True)
    await drain()
    assert result['stopped'] is True
    stopper.assert_awaited_once()
    assert caller.active_runs.get_run('ou_spx', KEY) is not None
    assert '显式中止' in execute.await_args.args[4]
    assert execute.await_args.args[0] is target


async def test_steer_does_not_stop_replacement_run_after_notice_await(setup, monkeypatch):
    caller, target, execute, _ = setup
    bind()
    target.active_runs.start_run('ou_agy', KEY, 'om_old_run')

    async def replace_run(*args, **kwargs):
        target.active_runs.start_run('ou_agy', KEY, 'om_new_run')

    target.feishu.reply_post.side_effect = replace_run
    stopper = AsyncMock()
    monkeypatch.setattr(dispatcher, 'stop_run', stopper)
    await append(caller, stop=True)
    await drain()
    stopper.assert_not_awaited()
    assert '显式中止' not in execute.await_args.args[4]


async def test_stop_failure_does_not_execute_followup(setup, monkeypatch):
    caller, target, execute, _ = setup
    bind()
    target.active_runs.start_run('ou_agy', KEY, 'om_running')
    monkeypatch.setattr(dispatcher, 'stop_run', AsyncMock(side_effect=RuntimeError('stop failed')))
    await append(caller, stop=True)
    await drain()
    execute.assert_not_awaited()
    target.feishu.reply_text.assert_awaited_once()


async def test_legacy_root_wins_over_polluted_caller_session(setup):
    caller, target, execute, _ = setup
    await caller.store.get_current('ou_spx', KEY)
    (await caller.store.get_current_raw('ou_spx', KEY))['session_id'] = 'wrong-claude'
    result = await append(caller)
    await drain()
    assert result['agent'] == 'agy'
    assert execute.await_args.args[6].runner == 'agy'
    assert task_routes.get(CHAT, THREAD)['profile'] == 'agy'
    assert (await caller.store.get_current('ou_spx', KEY)).session_id == 'wrong-claude'


async def test_restart_reloads_ownership_without_network_or_session_id(setup):
    caller, target, execute, _ = setup
    bind()
    importlib.reload(task_routes)
    target.store = SessionStore(profile='agy', default_runner='agy')
    caller.feishu.list_thread_messages.side_effect = AssertionError('must use persisted binding')
    result = await append(caller)
    await drain()
    assert result['agent'] == 'agy'
    assert execute.await_args.args[6].runner == 'agy'
    assert execute.await_args.args[1] == 'ou_agy'


@pytest.mark.parametrize('problem', ['missing_bot', 'corrupt_store', 'wrong_group', 'unknown_app', 'reply_not_root'])
async def test_unresolved_ownership_fails_without_post_stop_or_session_creation(setup, monkeypatch, problem):
    caller, target, execute, root = setup
    if problem == 'missing_bot':
        bind()
        monkeypatch.setattr(dispatcher, '_bots', {'spx': caller})
    elif problem == 'corrupt_store':
        Path(task_routes._path()).write_text('{bad')
    elif problem == 'wrong_group':
        root.chat_id = 'oc_another_group'
    elif problem == 'unknown_app':
        root.sender.id = 'app_not_loaded'
    else:
        root.parent_id = 'om_unavailable_root'
    result = await append(caller, stop=True)
    assert not result['ok']
    assert not caller.store.has_chat_record('ou_spx', KEY)
    caller.feishu.reply_post.assert_not_awaited()
    target.feishu.reply_post.assert_not_awaited()
    execute.assert_not_awaited()


async def test_cross_group_binding_cannot_be_reused(setup):
    caller, target, execute, _ = setup
    bind()
    result = await append(caller, chat='oc_other')
    assert not result['ok']
    execute.assert_not_awaited()


@pytest.mark.parametrize('persisted', [False, True])
async def test_message_alias_resolves_canonical_thread(setup, persisted):
    caller, target, execute, _ = setup
    if persisted:
        bind()
    result = await append(caller, thread='om_root')
    await drain()
    assert result['thread_id'] == THREAD
    assert execute.await_args.args[2] == KEY


async def test_delayed_thread_id_is_canonicalized(setup):
    caller, target, execute, _ = setup
    task_routes.bind(chat_id=CHAT, thread_id='om_root', profile='agy',
                     user_id='ou_agy', anchor='om_root')
    result = await append(caller, thread='om_root')
    await drain()
    assert result['thread_id'] == THREAD
    assert task_routes.get(CHAT, THREAD)['thread_id'] == THREAD
    assert task_routes.get(CHAT, 'om_root')['thread_id'] == THREAD


async def test_legacy_unshared_owner_comes_from_target_bucket(setup):
    caller, _, execute, _ = setup
    target = Bot('agy', 'agy', shared=False)
    target.store._data = {'ou_agy': {'private': {}}}
    await target.store.get_current('ou_actual_owner', KEY)
    dispatcher._bots['agy'] = target
    result = await append(caller)
    await drain()
    assert result['ok']
    assert execute.await_args.args[1] == 'ou_actual_owner'


async def test_ambiguous_unshared_users_never_fall_back_to_primary(setup):
    caller, _, execute, _ = setup
    target = Bot('agy', 'agy', shared=False)
    target.store._data = {'ou_agy': {'private': {}}}
    await target.store.get_current('ou_first', KEY)
    await target.store.get_current('ou_second', KEY)
    dispatcher._bots['agy'] = target
    assert not (await append(caller))['ok']
    execute.assert_not_awaited()


async def test_ambiguous_human_thread_is_rejected(setup):
    caller, target, execute, root = setup
    root.sender.sender_type = 'user'
    await caller.store.get_current('ou_spx', KEY)
    assert not (await append(caller))['ok']
    execute.assert_not_awaited()


async def test_same_bot_legacy_thread_keeps_session(setup):
    caller, target, execute, root = setup
    root.sender.id = 'app_spx'
    await caller.store.get_current('ou_spx', KEY)
    result = await append(caller)
    await drain()
    assert result['agent'] == 'spx'
    assert execute.await_args.args[0] is caller


async def test_dispatch_binds_before_worker_runs(setup, monkeypatch):
    caller, target, _, _ = setup
    seen = []

    async def spawn(bot, **kwargs):
        seen.append(task_routes.get(CHAT, THREAD))
        return True, 'ok'

    monkeypatch.setattr(dispatcher, 'handle_spawn', spawn)
    result = await dispatcher.dispatch_task(caller, user_id='ou_spx', group_chat_id=CHAT,
                                            title='t', prompt='p', target_bot=target)
    children = list(dispatcher._DISPATCH_CHILDREN.get(CHAT, set()))
    await asyncio.gather(*children)
    assert result['ok']
    assert seen[0]['profile'] == 'agy' and seen[0]['user_id'] == 'ou_agy'


async def test_binding_write_failure_prevents_worker_start(setup, monkeypatch):
    caller, target, _, _ = setup
    worker = AsyncMock()
    monkeypatch.setattr(dispatcher, 'handle_spawn', worker)
    Path(task_routes._path()).write_text('broken')
    result = await dispatcher.dispatch_task(caller, user_id='ou_spx', group_chat_id=CHAT,
                                            title='t', prompt='p', target_bot=target)
    assert not result['ok']
    worker.assert_not_awaited()


@pytest.mark.parametrize('stop', [False, True])
async def test_mcp_http_dispatcher_session_integration(setup, monkeypatch, stop):
    caller, target, execute, _ = setup
    bind()
    monkeypatch.setattr(http_server, '_bots', dispatcher._bots)
    monkeypatch.setattr(http_server, '_bot_loop', asyncio.get_running_loop())
    monkeypatch.setattr(http_server, '_handlers', NS(steer_thread=dispatcher.steer_or_append_thread))
    monkeypatch.setattr(http_server, '_control_token', 'test-route-token')
    server = http_server.start_control_server(0)
    monkeypatch.setenv('CC_LARK_CONTROL_PORT', str(server.server_address[1]))
    monkeypatch.setenv('CC_LARK_CONTROL_TOKEN', 'test-route-token')
    monkeypatch.setenv('CC_LARK_PROFILE', 'spx')
    monkeypatch.setenv('CC_LARK_CHAT_ID', CHAT)
    monkeypatch.setenv('CC_LARK_USER_ID', 'ou_spx')
    try:
        tool = cc_mcp_server._tool_steer_task if stop else cc_mcp_server._tool_append_to_task
        result = await asyncio.to_thread(tool, {'thread_id': THREAD, 'message': 'integration'})
        await drain()
        assert not result.get('isError')
        assert 'task owner agy' in result['content'][0]['text']
        assert execute.await_args.args[0] is target
        assert execute.await_args.args[6].runner == 'agy'
        assert not caller.store.has_chat_record('ou_spx', KEY)
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()


def test_conflicting_bindings_are_not_overwritten():
    original = bind()
    with pytest.raises(ValueError, match='conflicting'):
        task_routes.bind(**{**original, 'profile': 'spx'})
    assert task_routes.get(CHAT, THREAD) == original


@pytest.mark.parametrize('runner', ['agy', 'claude', 'codex'])
@pytest.mark.parametrize('mode', ['idle_append', 'busy_append', 'busy_steer'])
@pytest.mark.parametrize('legacy', [False, True], ids=['persisted-owner', 'legacy-root'])
async def test_assigned_executor_matrix(setup, monkeypatch, runner, mode, legacy):
    """Every assigned executor keeps its bot identity, backend and original session."""
    caller, _, execute, root = setup
    name = f'assigned_{runner}'
    owner = f'ou_{name}'
    target = Bot(name, runner)
    dispatcher._bots[name] = target
    session = await target.store.get_current_raw(owner, KEY)
    session['session_id'] = f'{runner}-original-session'
    root.sender.id = target.profile.app_id
    if not legacy:
        task_routes.bind(chat_id=CHAT, thread_id=THREAD, profile=name,
                         user_id=owner, anchor='om_root')

    lock = target._ensure_chat_lock(KEY)
    if mode != 'idle_append':
        await lock.acquire()
    stopper = AsyncMock()
    if mode == 'busy_steer':
        target.active_runs.start_run(owner, KEY, 'om_current')

        async def stop(registry, user, chat, **kwargs):
            assert registry is target.active_runs and user == owner and chat == KEY
            registry.clear_run(user, chat)
            lock.release()
            return True

        stopper.side_effect = stop
    monkeypatch.setattr(dispatcher, 'stop_run', stopper)

    result = await append(caller, stop=mode == 'busy_steer')
    assert result['ok'] and result['agent'] == name
    if mode == 'busy_append':
        assert result['queued']
        await asyncio.sleep(0)
        execute.assert_not_awaited()
        lock.release()
    await drain()
    args = execute.await_args.args
    assert args[0] is target and args[1] == owner
    assert args[6].runner == runner
    assert args[6].session_id == f'{runner}-original-session'
    assert not caller.store.has_chat_record('ou_spx', KEY)
    caller.feishu.reply_post.assert_not_awaited()
    if mode == 'busy_steer':
        stopper.assert_awaited_once()
    else:
        stopper.assert_not_awaited()
