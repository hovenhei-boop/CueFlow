from __future__ import annotations

import shutil
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from time import monotonic
from typing import Any

from cueflow.api import Workspace
from cueflow.config import RuntimeConfig, TrialConfig
from cueflow.errors import (
    ContractError,
    TrialAdmissionError,
    TrialExecutionStopped,
    TrialNotFoundError,
)
from cueflow.media import ProbeResult, probe_source
from cueflow.media_object_store import MediaObjectStore, media_ref_from_payload
from cueflow.orchestrator import ProviderFactories
from cueflow.trial_execution import TrialAlivePulse, TrialExecutionGate
from cueflow.trial_pricing import TrialPricing, usage_record
from cueflow.trial_storage import (
    LifecycleInspector,
    StorageReadiness,
    TosLifecycleInspector,
    check_disk_capacity,
    evaluate_storage_readiness,
    media_ref_from_result,
    object_ref_json,
    safe_job_workspace,
    trial_store_factories,
)
from cueflow.trial_store import (
    TrialAdmission,
    TrialStore,
    accounting_date,
    before,
    now_utc,
    public_request,
)

StoreFactory = Callable[[], TrialStore]
MediaStoreFactory = Callable[[], MediaObjectStore]
ProbeFactory = Callable[[Path, RuntimeConfig], ProbeResult]


class TrialService:
    """Single-host anonymous Trial admission, execution, accounting, and cleanup."""

    def __init__(
        self,
        config: TrialConfig,
        *,
        store_factory: StoreFactory | None = None,
        pricing: TrialPricing | None = None,
        storage_inspector: LifecycleInspector | None = None,
        source_store_factory: MediaStoreFactory | None = None,
        provider_factories: ProviderFactories | None = None,
        runtime: RuntimeConfig | None = None,
        probe_factory: ProbeFactory = probe_source,
    ) -> None:
        self.config = config
        self._store_factory = store_factory or (lambda: TrialStore(config.database_path))
        self._pricing = pricing or TrialPricing(config.pricing_path)
        self._storage_inspector = storage_inspector or TosLifecycleInspector()
        source, work, result = trial_store_factories(config)
        self._source_store_factory = source_store_factory or source
        self._provider_factories = provider_factories or ProviderFactories(
            media=work, result_media=result
        )
        self._runtime = runtime or RuntimeConfig.detect()
        self._probe = probe_factory
        self._executor = ThreadPoolExecutor(
            max_workers=config.global_concurrency, thread_name_prefix="cueflow-trial"
        )
        self._futures: set[Future[None]] = set()
        self._futures_lock = threading.Lock()
        self._readiness: StorageReadiness | None = None
        self._readiness_checked_at = 0.0

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)

    def wait_for_idle(self, *, timeout_seconds: float = 30.0) -> None:
        with self._futures_lock:
            futures = tuple(self._futures)
        for future in futures:
            future.result(timeout=timeout_seconds)

    def heartbeat(
        self, visitor_id: str, *, fingerprint_hmac: str | None, now: str | None = None
    ) -> dict[str, Any]:
        current = now or now_utc()
        store = self._store_factory()
        try:
            return store.touch_visitor(
                visitor_id,
                fingerprint_hmac=fingerprint_hmac,
                now=current,
                online_window_seconds=self.config.online_window_seconds,
            )
        finally:
            store.close()

    def submit(
        self,
        *,
        job_id: str,
        request_id: str,
        visitor_id: str,
        ip_hmac: str,
        fingerprint_hmac: str | None,
        media_path: Path,
        references: Sequence[Path],
        keywords: Sequence[str],
        now: str | None = None,
    ) -> dict[str, Any]:
        current = now or now_utc()
        self.heartbeat(visitor_id, fingerprint_hmac=fingerprint_hmac, now=current)
        if media_path.stat().st_size >= self.config.max_source_bytes:
            return self._reject_invalid(
                job_id, request_id, visitor_id, ip_hmac, fingerprint_hmac, current,
                "media must be smaller than 500,000,000 bytes",
            )
        try:
            probe = self._probe(media_path, self._runtime)
        except Exception as exc:
            return self._reject_invalid(
                job_id, request_id, visitor_id, ip_hmac, fingerprint_hmac, current, str(exc)
            )
        if probe.duration_ms >= self.config.max_source_duration_ms:
            return self._reject_invalid(
                job_id, request_id, visitor_id, ip_hmac, fingerprint_hmac, current,
                "media must be shorter than 60 minutes",
            )
        estimate = self._pricing.estimate_max_cost(probe.duration_ms, at=current)
        admission = TrialAdmission(
            request_id=request_id,
            job_id=job_id,
            visitor_id=visitor_id,
            action_kind="create",
            ip_hmac=ip_hmac,
            fingerprint_hmac=fingerprint_hmac,
            audio_duration_ms=probe.duration_ms,
            estimated_max_cost_micros=estimate,
            created_at=current,
            accounting_date=accounting_date(current),
        )
        self._admit(admission)
        self._submit_future(
            lambda: self._execute_new(admission, media_path, references, keywords)
        )
        return self._request(request_id)

    def retry_or_resume(
        self,
        *,
        job_id: str,
        action_kind: str,
        visitor_id: str,
        ip_hmac: str,
        fingerprint_hmac: str | None,
        now: str | None = None,
    ) -> dict[str, Any]:
        if action_kind not in {"retry", "resume"}:
            raise ContractError("Trial action must be retry or resume")
        current = now or now_utc()
        self.heartbeat(visitor_id, fingerprint_hmac=fingerprint_hmac, now=current)
        store = self._store_factory()
        try:
            previous = store.latest_job(job_id, visitor_id)
        finally:
            store.close()
        workspace_path = safe_job_workspace(self.config.work_root, job_id)
        if not workspace_path.is_dir() or previous["core_run_id"] is None:
            raise TrialNotFoundError("Trial workspace is no longer available")
        duration = int(previous["audio_duration_ms"])
        request_id = "req_" + uuid.uuid4().hex
        admission = TrialAdmission(
            request_id=request_id,
            job_id=job_id,
            visitor_id=visitor_id,
            action_kind=action_kind,
            ip_hmac=ip_hmac,
            fingerprint_hmac=fingerprint_hmac,
            audio_duration_ms=duration,
            estimated_max_cost_micros=self._pricing.estimate_max_cost(duration, at=current),
            created_at=current,
            accounting_date=accounting_date(current),
        )
        self._admit(admission)
        store = self._store_factory()
        try:
            store.bind_core_run(request_id, str(previous["core_run_id"]))
        finally:
            store.close()
        self._submit_future(
            lambda: self._execute_existing(admission, str(previous["core_run_id"]))
        )
        return self._request(request_id)

    def sweep(self, *, now: str | None = None) -> dict[str, Any]:
        current = now or now_utc()
        store = self._store_factory()
        try:
            stale = store.sweep_stale(
                stale_before=before(current, self.config.stale_request_seconds), now=current
            )
            unknown = store.classify_unknown(
                created_before=before(current, self.config.unknown_budget_hold_seconds), now=current
            )
            anonymized = store.anonymize_before(
                observed_before=before(current, 35 * 24 * 60 * 60)
            )
            expired_jobs = store.expired_workspace_jobs(
                terminal_before=before(current, self.config.workspace_retention_seconds)
            )
        finally:
            store.close()
        removed: list[str] = []
        for job_id in expired_jobs:
            path = safe_job_workspace(self.config.work_root, job_id)
            if path.is_dir():
                shutil.rmtree(path)
                removed.append(job_id)
        if monotonic() - self._readiness_checked_at >= 24 * 60 * 60:
            self.refresh_storage_readiness()
        return {
            "interrupted_request_ids": stale,
            "unknown_request_ids": unknown,
            "removed_workspace_job_ids": removed,
            "anonymized": anonymized,
        }

    def refresh_storage_readiness(self) -> StorageReadiness:
        self._readiness = evaluate_storage_readiness(self._storage_inspector, self.config)
        self._readiness_checked_at = monotonic()
        return self._readiness

    def disk_capacity(self) -> dict[str, Any]:
        store = self._store_factory()
        try:
            active = store.active_concurrency()
        finally:
            store.close()
        capacity = check_disk_capacity(
            self.config.work_root,
            global_concurrency=self.config.global_concurrency,
            active_concurrency=active,
            max_source_bytes=self.config.max_source_bytes,
            expansion_factor=self.config.workspace_expansion_factor,
            safety_margin_bytes=self.config.disk_safety_margin_bytes,
            max_used_ratio=self.config.disk_max_used_ratio,
        )
        return {
            "ready": capacity.ready,
            "used_ratio": capacity.used_ratio,
            "free_bytes": capacity.free_bytes,
            "required_free_bytes": capacity.required_free_bytes,
        }

    def summary(self, *, now: str | None = None) -> dict[str, Any]:
        current = now or now_utc()
        store = self._store_factory()
        try:
            result = store.summary(
                accounting_date=accounting_date(current),
                online_after=before(current, self.config.online_window_seconds),
                default_daily_budget_micros=self.config.daily_budget_micros,
            )
        finally:
            store.close()
        result["storage_ready"] = bool(self._readiness and self._readiness.ready)
        result["disk"] = self.disk_capacity()
        return result

    def list_requests(self, *, limit: int = 100) -> list[dict[str, Any]]:
        store = self._store_factory()
        try:
            return [dict(row) for row in store.requests(limit=min(max(limit, 1), 500))]
        finally:
            store.close()

    def jobs(self, visitor_id: str) -> list[dict[str, Any]]:
        store = self._store_factory()
        try:
            return [public_request(dict(row)) for row in store.jobs(visitor_id)]
        finally:
            store.close()

    def job(self, job_id: str, visitor_id: str) -> dict[str, Any]:
        store = self._store_factory()
        try:
            return public_request(dict(store.latest_job(job_id, visitor_id)))
        finally:
            store.close()

    def result_ref(self, job_id: str, visitor_id: str) -> Mapping[str, Any]:
        store = self._store_factory()
        try:
            row = store.latest_job(job_id, visitor_id)
            raw = row["result_object_json"]
        finally:
            store.close()
        if raw is None:
            raise TrialNotFoundError("Trial result is unavailable")
        import json

        value = json.loads(str(raw))
        if not isinstance(value, Mapping):
            raise TrialNotFoundError("Trial result is unavailable")
        return value

    def result_url(self, job_id: str, visitor_id: str) -> str:
        value = self.result_ref(job_id, visitor_id)
        factory = self._provider_factories.result_media or self._provider_factories.media
        store = factory()
        try:
            return store.presign_get(media_ref_from_payload(dict(value)))
        finally:
            store.close()

    def append_control(
        self, action: str, *, reason: str, daily_budget_micros: int | None = None
    ) -> dict[str, Any]:
        store = self._store_factory()
        try:
            return store.append_control(
                action,
                reason=reason,
                daily_budget_micros=daily_budget_micros,
                default_daily_budget_micros=self.config.daily_budget_micros,
                now=now_utc(),
            )
        finally:
            store.close()

    def _admit(self, admission: TrialAdmission) -> None:
        readiness = self._readiness or self.refresh_storage_readiness()
        store = self._store_factory()
        try:
            if not readiness.ready:
                raise store.record_external_rejection(
                    admission, reason="storage_not_ready", status_code=503
                )
            active = store.active_concurrency()
            disk = check_disk_capacity(
                self.config.work_root,
                global_concurrency=self.config.global_concurrency,
                active_concurrency=active,
                max_source_bytes=self.config.max_source_bytes,
                expansion_factor=self.config.workspace_expansion_factor,
                safety_margin_bytes=self.config.disk_safety_margin_bytes,
                max_used_ratio=self.config.disk_max_used_ratio,
            )
            if not disk.ready:
                raise store.record_external_rejection(
                    admission, reason="disk_capacity", status_code=503
                )
            store.admit(admission, self.config)
        finally:
            store.close()

    def _reject_invalid(
        self,
        job_id: str,
        request_id: str,
        visitor_id: str,
        ip_hmac: str,
        fingerprint_hmac: str | None,
        now: str,
        detail: str,
    ) -> dict[str, Any]:
        admission = TrialAdmission(
            request_id, job_id, visitor_id, "create", ip_hmac, fingerprint_hmac,
            0, 0, now, accounting_date(now)
        )
        store = self._store_factory()
        try:
            error = store.record_external_rejection(
                admission, reason="invalid_media", status_code=400
            )
        finally:
            store.close()
        raise TrialAdmissionError(detail or str(error), reason=error.reason, status_code=400)

    def _execute_new(
        self,
        admission: TrialAdmission,
        media_path: Path,
        references: Sequence[Path],
        keywords: Sequence[str],
    ) -> None:
        workspace_path = safe_job_workspace(self.config.work_root, admission.job_id)
        self._mark_running(admission.request_id)
        workspace: Workspace | None = None
        status = "failed"
        try:
            with TrialAlivePulse(
                self._store_factory,
                admission.request_id,
                interval_seconds=self.config.alive_interval_seconds,
            ):
                source_store = self._source_store_factory()
                try:
                    source_ref = source_store.upload(media_path, object_name=media_path.name)
                finally:
                    source_store.close()
                store = self._store_factory()
                try:
                    store.record_source_object(
                        admission.request_id, object_ref_json(source_ref)
                    )
                finally:
                    store.close()
                gate = TrialExecutionGate(self._store_factory, admission.request_id)
                workspace = Workspace(workspace_path, execution_control=gate)
                handle = workspace.create_run(
                    media_path, references=references, keywords=keywords
                )
                core_run_id = str(handle["run_id"])
                store = self._store_factory()
                try:
                    store.bind_core_run(admission.request_id, core_run_id)
                finally:
                    store.close()
                status = self._execute_workspace(
                    workspace, admission, core_run_id, "create"
                )
        except BaseException as exc:
            self._record_pre_execution_failure(admission.request_id, exc)
        finally:
            if workspace is not None:
                workspace.close()
        if status == "succeeded":
            self._remove_workspace(admission.job_id)

    def _execute_existing(self, admission: TrialAdmission, core_run_id: str) -> None:
        workspace_path = safe_job_workspace(self.config.work_root, admission.job_id)
        gate = TrialExecutionGate(self._store_factory, admission.request_id)
        self._mark_running(admission.request_id)
        workspace: Workspace | None = None
        status = "failed"
        try:
            with TrialAlivePulse(
                self._store_factory,
                admission.request_id,
                interval_seconds=self.config.alive_interval_seconds,
            ):
                workspace = Workspace(workspace_path, execution_control=gate)
                status = self._execute_workspace(
                    workspace, admission, core_run_id, admission.action_kind
                )
        except BaseException as exc:
            self._record_pre_execution_failure(admission.request_id, exc)
        finally:
            if workspace is not None:
                workspace.close()
        if status == "succeeded":
            self._remove_workspace(admission.job_id)

    def _execute_workspace(
        self, workspace: Workspace, admission: TrialAdmission, core_run_id: str, action: str
    ) -> str:
        before_invocations = {
            str(row["invocation_id"])
            for row in workspace.registry.invocations_for_run(core_run_id)
        }
        started = monotonic()
        result: dict[str, Any]
        try:
            if action == "retry":
                result = workspace.retry_run(
                    core_run_id, runtime=self._runtime, factories=self._provider_factories
                )
            else:
                result = workspace.execute_run(
                    core_run_id, runtime=self._runtime, factories=self._provider_factories
                )
        except TrialExecutionStopped:
            result = workspace.get_result(core_run_id)
        except BaseException:
            result = workspace.get_result(core_run_id)
        elapsed = round((monotonic() - started) * 1000)
        self._snapshot_usage(workspace, admission, core_run_id, before_invocations)
        status = str(result["status"])
        if status not in {"needs_review", "succeeded", "failed", "cancelled", "interrupted"}:
            status = "failed"
        reason = "operationally_stopped" if status == "interrupted" else None
        store = self._store_factory()
        try:
            if status == "succeeded":
                result_ref = media_ref_from_result(result)
                store.record_result(
                    admission.request_id, object_ref_json(result_ref), now=now_utc()
                )
            store.mark_terminal(
                admission.request_id,
                status=status,
                now=now_utc(),
                processing_duration_ms=elapsed,
                reject_reason=reason,
            )
            store.finalize_cost(admission.request_id, now=now_utc())
        finally:
            store.close()
        return status

    def _mark_running(self, request_id: str) -> None:
        store = self._store_factory()
        try:
            store.mark_running(request_id, now=now_utc())
        finally:
            store.close()

    def _remove_workspace(self, job_id: str) -> None:
        path = safe_job_workspace(self.config.work_root, job_id)
        if path.is_dir():
            shutil.rmtree(path)

    def _snapshot_usage(
        self,
        workspace: Workspace,
        admission: TrialAdmission,
        core_run_id: str,
        before_invocations: set[str],
    ) -> None:
        rows = [
            dict(row)
            for row in workspace.registry.invocations_for_run(core_run_id)
            if str(row["invocation_id"]) not in before_invocations
        ]
        store = self._store_factory()
        try:
            for row in rows:
                store.upsert_usage(usage_record(
                    row,
                    request_id=admission.request_id,
                    audio_duration_ms=admission.audio_duration_ms,
                    pricing=self._pricing,
                    now=now_utc(),
                ))
        finally:
            store.close()

    def _record_pre_execution_failure(self, request_id: str, exc: BaseException) -> None:
        store = self._store_factory()
        try:
            current = dict(store.request(request_id))
            reason: str | None
            if current["execution_status"] == "interrupted":
                status, reason = "interrupted", "operationally_stopped"
            else:
                status = "interrupted" if isinstance(exc, TrialExecutionStopped) else "failed"
                reason = "operationally_stopped" if status == "interrupted" else None
            store.mark_terminal(request_id, status=status, now=now_utc(), reject_reason=reason)
            store.finalize_cost(request_id, now=now_utc())
        finally:
            store.close()

    def _request(self, request_id: str) -> dict[str, Any]:
        store = self._store_factory()
        try:
            return public_request(dict(store.request(request_id)))
        finally:
            store.close()

    def _submit_future(self, action: Callable[[], None]) -> None:
        future = self._executor.submit(action)
        with self._futures_lock:
            self._futures.add(future)
        future.add_done_callback(self._forget_future)

    def _forget_future(self, future: Future[None]) -> None:
        with self._futures_lock:
            self._futures.discard(future)
