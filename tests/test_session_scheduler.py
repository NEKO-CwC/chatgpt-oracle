from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from chatgpt_oracle.models import Job
from chatgpt_oracle.scheduler import IdempotencyConflict, Scheduler, SchedulerBusy
from chatgpt_oracle.storage import JobStore


def queue(scheduler: Scheduler, task: str, session: str | None, *, publish: bool = True) -> None:
    scheduler.enqueue(task, 'consult', on_busy='queue', retry_after_seconds=1, session_id=session)
    if publish:
        scheduler.set_state(task, 'QUEUED')


def resident(scheduler: Scheduler, session: str) -> None:
    queue(scheduler, f'first-{session}', session)
    assert scheduler.reserve_next(os.getpid(), 'worker') == f'first-{session}'
    scheduler.complete(f'first-{session}', 'COMPLETE')


def export(scheduler: Scheduler, task: str, session: str, **kwargs: object) -> None:
    scheduler.enqueue_export(
        task, session_id=session, conversation_url='https://chatgpt.com/c/test', scope='all', count=10,
        request_key=None, input_fingerprint=task, on_busy='queue', retry_after_seconds=1, **kwargs,
    )


def stored_job(store: JobStore, task: str, session: str, state: str, *, receipt: bool = False) -> Job:
    job = Job.create('consult', 'one question', session_id=session)
    job.id = task
    job.state = state
    if receipt:
        job.conversation_url = 'https://chatgpt.com/c/confirmed'
    store.save(job)
    return job


def test_four_running_sessions_and_fifth_waits_until_explicit_release(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    for index in range(5):
        queue(scheduler, f'task-{index}', f'session-{index}')
    for index in range(4):
        assert scheduler.reserve_next(os.getpid(), 'worker') == f'task-{index}'
        scheduler.set_state(f'task-{index}', 'RUNNING', owner_pid=os.getpid())
    assert scheduler.status()['active_count'] == 4
    assert scheduler.reserve_next(os.getpid(), 'worker') is None

    scheduler.complete('task-0', 'COMPLETE')
    assert scheduler.status()['session_count'] == 4
    assert scheduler.reserve_next(os.getpid(), 'worker') is None
    queue(scheduler, 'followup-0', 'session-0')
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'followup-0'
    assert not scheduler.release_session('session-0')
    scheduler.complete('followup-0', 'COMPLETE')
    assert scheduler.release_session('session-0')
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'task-4'


def test_session_fifo_includes_admission_exports_and_browser_operations(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    queue(scheduler, 'admitting', 'a', publish=False)
    export(scheduler, 'export-a', 'a')
    scheduler.enqueue_browser_operation('browser-a', os.getpid(), 'browser', session_id='a')
    queue(scheduler, 'last-a', 'a')
    queue(scheduler, 'other', 'b')
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'other'
    assert scheduler.reserve_next(os.getpid(), 'worker') is None
    assert not scheduler.reserve_browser_operation('browser-a', os.getpid(), 'browser')
    scheduler.set_state('admitting', 'QUEUED')
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'admitting'
    scheduler.complete('admitting', 'COMPLETE')
    assert scheduler.reserve_next(os.getpid(), 'worker', allow_exports=False) is None
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'export-a'
    scheduler.complete('export-a', 'COMPLETE')
    assert scheduler.reserve_next(os.getpid(), 'worker') is None
    assert scheduler.reserve_browser_operation('browser-a', os.getpid(), 'browser')
    scheduler.complete('browser-a', 'COMPLETE')
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'last-a'


def test_cleanup_obeys_own_fifo_but_does_not_allocate_a_fifth_lane(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    for session in ('a', 'b', 'c', 'd'):
        resident(scheduler, session)
    queue(scheduler, 'waiting-e', 'e')
    scheduler.enqueue_browser_operation('cleanup-unresident', os.getpid(), 'cleanup', cleanup=True, session_id='unresident')
    assert scheduler.reserve_browser_operation('cleanup-unresident', os.getpid(), 'cleanup')
    assert scheduler.operation_session('cleanup-unresident') == 'unresident'
    assert scheduler.session_active('unresident') == 'cleanup-unresident'
    assert scheduler.status()['session_count'] == 4
    assert scheduler.reserve_next(os.getpid(), 'worker') is None
    scheduler.complete('cleanup-unresident', 'COMPLETE')
    assert scheduler.status()['session_count'] == 4
    assert scheduler.release_session('unresident')

    queue(scheduler, 'followup-a', 'a')
    scheduler.enqueue_browser_operation('cleanup-a', os.getpid(), 'cleanup', cleanup=True, session_id='a')
    assert not scheduler.reserve_browser_operation('cleanup-a', os.getpid(), 'cleanup')
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'followup-a'
    scheduler.complete('followup-a', 'COMPLETE')
    assert scheduler.reserve_browser_operation('cleanup-a', os.getpid(), 'cleanup')
    assert not scheduler.release_session('a', after_task_id='wrong')
    assert scheduler.release_session('a', after_task_id='cleanup-a')
    assert scheduler.status()['session_count'] == 4
    scheduler.complete('cleanup-a', 'COMPLETE')
    assert scheduler.status()['session_count'] == 3
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'waiting-e'


def test_unresident_cleanup_waits_for_running_lanes_then_excludes_them(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    queue(scheduler, 'running', 'a')
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'running'
    scheduler.enqueue_browser_operation('cleanup', os.getpid(), 'cleanup', cleanup=True, session_id='unresident')
    assert not scheduler.reserve_browser_operation('cleanup', os.getpid(), 'cleanup')
    scheduler.complete('running', 'COMPLETE')
    assert scheduler.reserve_browser_operation('cleanup', os.getpid(), 'cleanup')
    queue(scheduler, 'next', 'a')
    assert scheduler.reserve_next(os.getpid(), 'worker') is None
    scheduler.complete('cleanup', 'COMPLETE')
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'next'


def test_session_closing_is_an_atomic_admission_barrier(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    resident(scheduler, 'a')
    scheduler.enqueue_browser_operation('close', os.getpid(), 'cleanup', cleanup=True, session_id='a')
    with pytest.raises(SchedulerBusy) as error:
        queue(scheduler, 'late-submit', 'a')
    assert error.value.info.reason == 'SESSION_CLOSING'
    with pytest.raises(SchedulerBusy):
        export(scheduler, 'late-export', 'a')
    with pytest.raises(SchedulerBusy):
        scheduler.enqueue_browser_operation('late-probe', os.getpid(), 'probe', session_id='a')
    queue(scheduler, 'unrelated', 'b')
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'unrelated'


def test_pending_queries_include_other_processes_and_admitting_work(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    queue(scheduler, 'a1', 'a', publish=False)
    queue(scheduler, 'b1', 'b')
    queue(scheduler, 'anonymous', None)
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'b1'
    other = Scheduler(tmp_path)
    assert other.pending_session_ids() == {'a', 'b'}
    assert other.session_pending('a')
    assert other.session_active('b') == 'b1'
    scheduler.request_cancel('a1')
    scheduler.complete('b1', 'COMPLETE')
    assert other.pending_session_ids() == set()
    assert not other.session_pending('a')


def test_queue_full_reports_global_queue_for_tasks_and_exports(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    for index in range(8):
        scheduler.enqueue(f'q{index}', 'consult', session_id=f's{index}', on_busy='queue', retry_after_seconds=1)
    with pytest.raises(SchedulerBusy) as error:
        queue(scheduler, 'overflow', 'new')
    assert error.value.info.reason == 'CHATGPT_QUEUE_FULL'
    assert error.value.info.queue_length == 8
    with pytest.raises(SchedulerBusy) as error:
        export(scheduler, 'overflow-export', 'new')
    assert error.value.info.queue_length == 8


def test_reject_accounts_for_four_accepted_sessions_before_dispatch(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    for index in range(4):
        scheduler.enqueue(f'q{index}', 'consult', session_id=f's{index}', on_busy='reject', retry_after_seconds=1)
    with pytest.raises(SchedulerBusy) as error:
        scheduler.enqueue('fifth', 'consult', session_id='fifth', on_busy='reject', retry_after_seconds=1)
    assert error.value.info.reason == 'SESSION_CAPACITY'
    assert scheduler.status()['session_count'] == 0


def test_full_resident_capacity_has_no_invented_active_task_id(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    for session in ('a', 'b', 'c', 'd'):
        resident(scheduler, session)
    with pytest.raises(SchedulerBusy) as error:
        scheduler.enqueue('e', 'consult', session_id='e', on_busy='reject', retry_after_seconds=1)
    assert error.value.info.reason == 'SESSION_CAPACITY'
    assert error.value.info.active_task_id is None
    scheduler.enqueue('again-a', 'consult', session_id='a', on_busy='reject', retry_after_seconds=1)


def test_excluded_or_deferred_head_preserves_its_session_fifo(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    queue(scheduler, 'a1', 'a')
    queue(scheduler, 'a2', 'a')
    queue(scheduler, 'b1', 'b')
    assert scheduler.reserve_next(os.getpid(), 'worker', exclude={'a1'}) == 'b1'
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'a1'
    scheduler.defer_capacity('a1')
    assert scheduler.status('a1')['scheduler_state'] == 'QUEUED'
    assert scheduler.reserve_next(os.getpid(), 'worker') is None
    with scheduler._connect() as conn:
        conn.execute('UPDATE queue SET available_at=0 WHERE task_id=?', ('a1',))
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'a1'


def test_cancellation_cannot_be_undone_by_admission_or_capacity_requeue(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    queue(scheduler, 'admitting', 'a', publish=False)
    assert scheduler.request_cancel('admitting') == 'CANCELLED'
    scheduler.set_state('admitting', 'QUEUED')
    assert scheduler.reserve_next(os.getpid(), 'worker') is None
    queue(scheduler, 'running', 'a')
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'running'
    assert scheduler.request_cancel('running') == 'CANCEL_REQUESTED'
    scheduler.set_state('running', 'RUNNING', owner_pid=os.getpid())
    assert scheduler.status('running')['scheduler_state'] == 'CANCEL_REQUESTED'
    scheduler.defer_capacity('running')
    assert scheduler.status('running')['scheduler_state'] == 'CANCELLED'
    assert scheduler.reserve_next(os.getpid(), 'worker') is None
    scheduler.complete('running', 'CANCELLED')
    assert scheduler.request_cancel('running') == 'CANCELLED'
    assert scheduler.status()['active_count'] == 0


def test_state_mutation_requires_exact_process_owner(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    queue(scheduler, 'task', 'a')
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'task'
    scheduler.set_state('task', 'RUNNING', owner_pid=999999)
    assert scheduler.status('task')['scheduler_state'] == 'STARTING'
    assert scheduler.status()['active_state'] == 'STARTING'
    assert not scheduler.release_session('a', after_task_id='task')


def test_export_aliases_use_shared_scheduling_key_and_keep_original_owner(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    queue(scheduler, 'task-a', 'a')
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'task-a'
    export(scheduler, 'alias-export', 'alias', scheduling_session_id='a')
    assert scheduler.reserve_next(os.getpid(), 'worker') is None
    assert scheduler.get_export('alias-export')['session_id'] == 'alias'
    assert scheduler.operation_session('alias-export') == 'a'
    scheduler.complete('task-a', 'COMPLETE')
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'alias-export'


def test_idempotency_lookup_checks_fingerprint_before_closing_reject(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    scheduler.enqueue('one', 'consult', on_busy='queue', retry_after_seconds=1, session_id='a', request_id='r', request_hash='h')
    scheduler.complete('one', 'COMPLETE')
    scheduler.enqueue_browser_operation('close', os.getpid(), 'cleanup', cleanup=True, session_id='a')
    assert scheduler.idempotent_task('r', 'h') == 'one'
    assert scheduler.idempotent_task('missing', 'h') is None
    with pytest.raises(IdempotencyConflict):
        scheduler.idempotent_task('r', 'different')
    retry = scheduler.enqueue('other', 'consult', on_busy='queue', retry_after_seconds=1, session_id='a', request_id='r', request_hash='h')
    assert retry['task_id'] == 'one'


def test_maintenance_excludes_every_session_but_idle_residents_can_be_maintained(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    for session in ('a', 'b', 'c', 'd'):
        resident(scheduler, session)
    assert scheduler.acquire_external_slot('maintenance', os.getpid(), 'lease')
    queue(scheduler, 'task-a', 'a')
    scheduler.enqueue_browser_operation('probe-b', os.getpid(), 'probe', session_id='b')
    assert scheduler.reserve_next(os.getpid(), 'worker') is None
    assert not scheduler.reserve_browser_operation('probe-b', os.getpid(), 'probe')
    scheduler.release_external_slot('maintenance', 'wrong')
    assert scheduler.status()['active_task_id'] == 'maintenance'
    scheduler.release_external_slot('maintenance', 'lease')
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'task-a'
    assert not scheduler.acquire_external_slot('maintenance', os.getpid(), 'lease')
    assert scheduler.reserve_browser_operation('probe-b', os.getpid(), 'probe')


def test_legacy_anonymous_lane_is_ephemeral_and_serial(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    queue(scheduler, 'a', None)
    queue(scheduler, 'b', None)
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'a'
    assert scheduler.reserve_next(os.getpid(), 'worker') is None
    scheduler.complete('a', 'COMPLETE')
    assert scheduler.status()['session_count'] == 0
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'b'
    assert scheduler.operation_session('b') is None


def test_database_reservations_are_exclusive_across_processes(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    for index in range(4):
        queue(scheduler, f'first-{index}', f'session-{index}')
        queue(scheduler, f'second-{index}', f'session-{index}')
    script = (
        'import json,os,sys; from pathlib import Path; from chatgpt_oracle.scheduler import Scheduler; '
        'print(json.dumps(Scheduler(Path(sys.argv[1])).reserve_next(os.getpid(),sys.argv[2])))'
    )

    def claim(index: int) -> str | None:
        result = subprocess.run(
            [sys.executable, '-c', script, str(tmp_path), f'process-{index}'],
            capture_output=True, text=True, check=True, timeout=20,
        )
        return json.loads(result.stdout)

    with ThreadPoolExecutor(max_workers=6) as pool:
        claimed = list(pool.map(claim, range(6)))
    assert sorted(task for task in claimed if task) == [f'first-{index}' for index in range(4)]
    assert scheduler.status()['active_count'] == 4


@pytest.mark.parametrize('saved_state,receipt,expected', [
    ('STARTING', False, 'RECOVERY_REQUIRED'),
    ('SEND_PENDING', True, 'RECOVERY_REQUIRED'),
    ('RUNNING', True, 'QUEUED'),
    ('CANCEL_REQUESTED', True, 'QUEUED'),
    ('COMPLETE', True, 'COMPLETE'),
])
def test_dead_owner_recovery_requires_a_confirmed_durable_receipt(
    tmp_path: Path, saved_state: str, receipt: bool, expected: str,
) -> None:
    scheduler = Scheduler(tmp_path)
    store = JobStore(tmp_path)
    queue(scheduler, 'task', 'a')
    stored_job(store, 'task', 'a', saved_state, receipt=receipt)
    assert scheduler.reserve_next(999999, 'dead') == 'task'
    scheduler.recover_startup(store)
    assert scheduler.status('task')['scheduler_state'] == expected
    assert scheduler.session_active('a') is None
    assert scheduler.status()['session_count'] == 1
    recovered = store.get('task')
    if expected == 'RECOVERY_REQUIRED':
        assert recovered.state == 'ERROR'
        assert recovered.error_code == 'INTERRUPTED_BEFORE_RECEIPT'
        assert scheduler.reserve_next(os.getpid(), 'worker') is None
    elif expected == 'QUEUED':
        assert recovered.state == saved_state
        assert scheduler.reserve_next(os.getpid(), 'worker') == 'task'
    else:
        assert scheduler.reserve_next(os.getpid(), 'worker') is None


def test_recovery_does_not_touch_live_owner_and_recovers_dead_admission(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    store = JobStore(tmp_path)
    queue(scheduler, 'live', 'live')
    stored_job(store, 'live', 'live', 'STARTING')
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'live'
    queue(scheduler, 'saved', 'saved', publish=False)
    stored_job(store, 'saved', 'saved', 'QUEUED')
    queue(scheduler, 'missing', 'missing', publish=False)
    queue(scheduler, 'live-admission', 'live-admission', publish=False)
    with scheduler._connect() as conn:
        conn.execute("UPDATE queue SET admission_owner_pid=999999 WHERE task_id IN ('saved','missing')")
    scheduler.recover_startup(store)
    assert scheduler.session_active('live') == 'live'
    assert store.get('live').state == 'STARTING'
    assert scheduler.status('saved')['scheduler_state'] == 'QUEUED'
    assert scheduler.status('missing')['scheduler_state'] == 'ERROR'
    assert scheduler.status('live-admission')['scheduler_state'] == 'ADMITTING'


def test_legacy_active_slot_migration_never_resends_unknown_send(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    store = JobStore(tmp_path)
    queue(scheduler, 'legacy', None)
    stored_job(store, 'legacy', 'a', 'STARTING')
    with scheduler._connect() as conn:
        conn.execute("UPDATE queue SET state='STARTING' WHERE task_id='legacy'")
        conn.execute("UPDATE slot SET task_id='legacy',owner_pid=999999,owner_token='old',state='STARTING' WHERE id=1")
    # Without a durable-store recovery pass, the old lease stays exclusive.
    assert scheduler.reserve_next(os.getpid(), 'worker') is None
    scheduler.recover_startup(store)
    assert scheduler.status('legacy')['scheduler_state'] == 'RECOVERY_REQUIRED'
    assert scheduler.operation_session('legacy') == 'a'
    assert scheduler.status()['session_count'] == 1
    assert scheduler.status()['active_count'] == 0
    assert scheduler.release_session('a')


def test_old_schema_migration_tolerates_missing_export_metadata(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / 'scheduler.sqlite3') as conn:
        conn.executescript('''
            CREATE TABLE queue (
                task_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, state TEXT NOT NULL,
                mode TEXT NOT NULL, request_id TEXT UNIQUE, created_at REAL NOT NULL
            );
            INSERT INTO queue VALUES ('orphan-export',1,'COMPLETE','export',NULL,0);
        ''')
    scheduler = Scheduler(tmp_path)
    assert scheduler.status('orphan-export')['scheduler_state'] == 'COMPLETE'
    assert scheduler.operation_session('orphan-export') is None


def test_failed_cleanup_keeps_its_resident_lane_retryable(tmp_path: Path) -> None:
    scheduler = Scheduler(tmp_path)
    resident(scheduler, 'a')
    scheduler.enqueue_browser_operation('close-failed', os.getpid(), 'cleanup', cleanup=True, session_id='a')
    assert scheduler.reserve_browser_operation('close-failed', os.getpid(), 'cleanup')
    scheduler.complete('close-failed', 'ERROR')
    assert scheduler.status()['session_count'] == 1
    scheduler.enqueue_browser_operation('close-retry', os.getpid(), 'cleanup', cleanup=True, session_id='a')
    assert scheduler.reserve_browser_operation('close-retry', os.getpid(), 'cleanup')
    assert scheduler.release_session('a', after_task_id='close-retry')
    scheduler.complete('close-retry', 'COMPLETE')
    assert scheduler.status()['session_count'] == 0


def test_unconfirmed_send_blocks_later_session_turn_until_explicit_close(tmp_path):
    scheduler = Scheduler(tmp_path)
    store = JobStore(tmp_path)
    queue(scheduler, 'uncertain', 'a')
    stored_job(store, 'uncertain', 'a', 'SEND_PENDING', receipt=True)
    assert scheduler.reserve_next(999999, 'dead') == 'uncertain'
    scheduler.recover_startup(store)
    queue(scheduler, 'next-a', 'a')
    queue(scheduler, 'other-b', 'b')
    assert scheduler.reserve_next(os.getpid(), 'worker') == 'other-b'
    assert scheduler.reserve_next(os.getpid(), 'worker') is None
    assert 'a' in scheduler.pending_session_ids()
    assert set(scheduler.cancel_waiting_session_tasks('a')) == {'uncertain', 'next-a'}
    assert scheduler.status('uncertain')['scheduler_state'] == 'CANCELLED'
