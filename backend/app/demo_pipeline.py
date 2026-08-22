# backend/app/demo_pipeline.py
"""答辩演示管线：让一条聊天消息真实走完 7 跳，并把真实标识符与真实耗时留痕。

本模块只做编排与留痕，不重写任何业务逻辑：

  * 第 1 跳消息解析复用 ``cloud_gateway.message_parser.parse_pricing_event``；
  * 第 2 跳是对已部署 Cloud Run 入口的真实 HTTP 调用；
  * 第 3/4 跳的执行名、去重判定、updatedRange 全部取自入口响应与 Workflows 执行结果；
  * 第 5/6 跳复用 ``PricingWorkflowStore.create`` 与后端现有的算价函数；
  * 第 7 跳复用 ``backend.engine.core.formatter`` 的导出能力。

拿不到的字段一律留空并在 ``note`` 里写清楚原因，禁止补默认值假装成功。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from pydantic import BaseModel, Field

from backend.app.pricing_workflow import PricingWorkflowCreateReq, PricingWorkflowStore
from backend.engine.core.formatter import build_export_frames, write_export_xlsx
from cloud_gateway.message_parser import MessageParseError, parse_pricing_event


FIXTURES_DIR = Path(__file__).resolve().parent / "demo_fixtures"

DEFAULT_INGRESS_URL = "https://pricing-huachat-ingress-339841524852.europe-west1.run.app"
DEFAULT_PROJECT_ID = "gen-lang-client-0394580582"
DEFAULT_REGION = "europe-west1"
DEFAULT_WORKFLOW_NAME = "pricing-request-sandbox"
DEFAULT_SPREADSHEET_ID = "1g96wjgMSGMhXrzLGwCAIQic_mYkS8xsThxJryfFHJ2A"
DEFAULT_SHEET_NAME = "2026.07"
DEFAULT_PNS = ("DEMO-PN-002", "DEMO-PN-003")

WORKFLOW_EXECUTIONS_API = "https://workflowexecutions.googleapis.com/v1"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
CONSOLE_BASE = "https://console.cloud.google.com/workflows/workflow"

SCENARIOS = ("normal", "duplicate", "blocked")
SCENARIO_LABELS = {"normal": "正常链路", "duplicate": "去重拦截", "blocked": "契约拦截"}

# 7 跳的 key / 顺序 / 文案由前后端契约固定，不在运行期改动。
STEP_CATALOG: Tuple[Dict[str, Any], ...] = (
    {"key": "message", "index": 1, "title": "消息构造与字段解析", "subtitle": "脱敏消息 · 14 列任务行", "layer": "local"},
    {"key": "ingress", "index": 2, "title": "Cloud Run 入口", "subtitle": "鉴权 · 契约校验 · 派发", "layer": "cloud"},
    {"key": "workflow", "index": 3, "title": "Google Workflows 编排", "subtitle": "执行状态 · 去重判定", "layer": "cloud"},
    {"key": "sheet", "index": 4, "title": "Google Sheets 任务行", "subtitle": "updatedRange · 写入内容", "layer": "cloud"},
    {"key": "ingest", "index": 5, "title": "内网摄取与任务建立", "subtitle": "任务 id · 幂等键 · 状态机", "layer": "local"},
    {"key": "pricing", "index": 6, "title": "确定性算价", "subtitle": "匹配路径 · 价格层级 · 版本", "layer": "local"},
    {"key": "artifact", "index": 7, "title": "GSP 模板制品 + 人工复核边界", "subtitle": "xlsx 制品 · 未授权提交", "layer": "local"},
)

# 入口冷启动实测约 3 秒，留足余量；Workflows 执行结果轮询上限同样按现场节奏收敛。
INGRESS_TIMEOUT_S = 30.0
HEALTH_TIMEOUT_S = 5.0
HEALTH_CACHE_S = 30.0
WORKFLOW_POLL_TIMEOUT_S = 25.0
WORKFLOW_POLL_INTERVAL_S = 1.0
STREAM_IDLE_TIMEOUT_S = 300.0


class DemoPipelineError(RuntimeError):
    pass


class DemoRunNotFound(DemoPipelineError):
    pass


class DemoRunBusy(DemoPipelineError):
    pass


class DemoHttpError(RuntimeError):
    """真实网络调用失败（连接不上、超时、响应不可解析）。"""


class DemoRunReq(BaseModel):
    scenario: str = Field(default="normal", max_length=32)
    customer: Optional[str] = Field(default=None, max_length=200)
    pns: List[str] = Field(default_factory=list)
    product_line: Optional[str] = Field(default=None, max_length=200)
    tier: Optional[str] = Field(default=None, max_length=100)
    reuse_message_id: Optional[str] = Field(default=None, max_length=200)


def _env(name: str, default: str) -> str:
    return (os.getenv(name) or "").strip() or default


def _now() -> float:
    return time.time()


def _ms(started_at: float, ended_at: float) -> int:
    return int(round((ended_at - started_at) * 1000))


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _loads_soft(raw: str) -> Any:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text[:4000]}


def _perform(request: urllib.request.Request, timeout: float) -> Tuple[int, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - 目标地址来自后端配置
            body = resp.read().decode("utf-8", "replace")
            return int(getattr(resp, "status", 0) or 0), _loads_soft(body)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover - 读取错误体失败时仍要保留状态码
            body = ""
        return int(exc.code), _loads_soft(body)
    except Exception as exc:
        raise DemoHttpError(f"{type(exc).__name__}: {exc}") from exc


def _http_post_json(
    url: str,
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    timeout: float = INGRESS_TIMEOUT_S,
) -> Tuple[int, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    return _perform(request, timeout)


def _http_get_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = HEALTH_TIMEOUT_S,
) -> Tuple[int, Any]:
    request = urllib.request.Request(url, method="GET")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    return _perform(request, timeout)


def _sse(event: str, payload: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _fact(label: str, value: Any) -> Dict[str, str]:
    return {"label": label, "value": "" if value is None else str(value)}


def _execution_id(execution_name: str) -> str:
    return str(execution_name or "").rsplit("/", 1)[-1]


class _LiveRun:
    """一次运行的内存态：快照 + 事件流游标。"""

    def __init__(self, snapshot: Dict[str, Any]) -> None:
        self.snapshot = snapshot
        self.events: List[Dict[str, Any]] = []
        self.finished = False
        self.condition = threading.Condition()


class DemoPipeline:
    """演示管线协调器：串行跑 7 跳、广播状态变化、落盘快照。"""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        compute_rows: Callable[[List[str], bool], Dict[str, Any]],
        workflow_store: Callable[[], PricingWorkflowStore],
        engine_meta: Callable[[], Dict[str, Any]],
        execution_context: Optional[Callable[[Optional[str]], Dict[str, Any]]] = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.runs_dir = self.runtime_dir / "demo_runs"
        self.artifacts_dir = self.runs_dir / "artifacts"
        self._compute_rows = compute_rows
        self._workflow_store = workflow_store
        self._engine_meta = engine_meta
        self._execution_context = execution_context
        self._lock = threading.RLock()
        self._live: Dict[str, _LiveRun] = {}
        self._active_run_id: Optional[str] = None
        self._health_cache: Optional[Tuple[float, Optional[Dict[str, Any]]]] = None

    # ---------- 配置 ----------

    def ensure_dirs(self) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def ingress_url() -> str:
        return _env("DEMO_INGRESS_URL", DEFAULT_INGRESS_URL).rstrip("/")

    @staticmethod
    def ingress_token() -> str:
        # token 只在进程内存里出现，任何返回体与落盘快照都不得回显。
        return (os.getenv("DEMO_INGRESS_TOKEN") or "").strip()

    @classmethod
    def gcp_access_token(cls) -> str:
        """只读拉取 Workflows 执行结果用的访问令牌，没有就不拉。

        直接给定的 DEMO_GCP_ACCESS_TOKEN 优先；否则用 refresh token 现换一个。
        访问令牌一小时过期，答辩现场不能指望手工续，所以缓存到过期前 5 分钟。
        """
        explicit = (os.getenv("DEMO_GCP_ACCESS_TOKEN") or "").strip()
        if explicit:
            return explicit
        return cls._refreshed_access_token()

    _token_cache: Dict[str, Any] = {"value": "", "expires_at": 0.0}
    _token_lock = threading.Lock()

    @classmethod
    def _refreshed_access_token(cls) -> str:
        client_id = (os.getenv("DEMO_GCP_CLIENT_ID") or "").strip()
        client_secret = (os.getenv("DEMO_GCP_CLIENT_SECRET") or "").strip()
        refresh_token = (os.getenv("DEMO_GCP_REFRESH_TOKEN") or "").strip()
        if not (client_id and client_secret and refresh_token):
            return ""
        with cls._token_lock:
            cached = cls._token_cache
            if cached["value"] and cached["expires_at"] > _now() + 300:
                return str(cached["value"])
            payload = urllib.parse.urlencode(
                {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                OAUTH_TOKEN_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    body = json.loads(response.read().decode("utf-8"))
            except Exception:
                # 换不到令牌只影响执行结果回填，链路其余部分照常演示。
                return ""
            token = str(body.get("access_token") or "")
            if not token:
                return ""
            cls._token_cache = {
                "value": token,
                "expires_at": _now() + float(body.get("expires_in") or 3600),
            }
            return token

    @staticmethod
    def environment() -> Dict[str, str]:
        return {
            "project_id": _env("DEMO_GCP_PROJECT", DEFAULT_PROJECT_ID),
            "region": _env("DEMO_GCP_REGION", DEFAULT_REGION),
            "workflow_name": _env("DEMO_WORKFLOW_NAME", DEFAULT_WORKFLOW_NAME),
            "spreadsheet_id": _env("DEMO_SPREADSHEET_ID", DEFAULT_SPREADSHEET_ID),
            "sheet_name": _env("DEMO_SHEET_NAME", DEFAULT_SHEET_NAME),
        }

    def workflow_console_url(self) -> str:
        env = self.environment()
        return (
            f"{CONSOLE_BASE}/{env['region']}/{env['workflow_name']}"
            f"?project={env['project_id']}"
        )

    def execution_console_url(self, execution_name: str) -> str:
        env = self.environment()
        return (
            f"{CONSOLE_BASE}/{env['region']}/{env['workflow_name']}"
            f"/execution/{_execution_id(execution_name)}?project={env['project_id']}"
        )

    def data_version(self) -> Optional[str]:
        """由已加载的价格数据文件派生的数据版本，引擎没加载就没有版本。"""
        meta = self._engine_meta() or {}
        if not meta.get("loaded"):
            return None
        parts: List[str] = []
        epochs: List[int] = []
        for file_key, epoch_key in (
            ("france_price_file", "country_data_updated_at_epoch"),
            ("sys_price_file", "sys_data_updated_at_epoch"),
        ):
            path = meta.get(file_key)
            epoch = meta.get(epoch_key)
            if not path or epoch is None:
                continue
            parts.append(f"{Path(str(path)).name}:{int(float(epoch))}")
            epochs.append(int(float(epoch)))
        if not parts or not epochs:
            return None
        digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:10]
        stamp = datetime.fromtimestamp(max(epochs), tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"v-{stamp}-{digest}"

    def _health(self) -> Optional[Dict[str, Any]]:
        cached = self._health_cache
        if cached and (_now() - cached[0]) < HEALTH_CACHE_S:
            return cached[1]
        payload: Optional[Dict[str, Any]] = None
        try:
            status, body = _http_get_json(f"{self.ingress_url()}/health", timeout=HEALTH_TIMEOUT_S)
            if status == 200 and isinstance(body, dict):
                payload = body
        except DemoHttpError:
            payload = None
        self._health_cache = (_now(), payload)
        return payload

    def config(self) -> Dict[str, Any]:
        env = self.environment()
        health = self._health()
        ingress_configured = bool(self.ingress_token())
        saved = self._saved_snapshots(limit=200)
        last_message_id = None
        for snapshot in saved:
            if snapshot.get("message_id") and snapshot.get("scenario") != "blocked":
                last_message_id = snapshot.get("message_id")
                break
        return {
            "live_available": bool(ingress_configured and health and health.get("ok")),
            "ingress_configured": ingress_configured,
            "cloud_run_url": self.ingress_url(),
            "cloud_run_health": health,
            "workflow_console_url": self.workflow_console_url(),
            "project_id": env["project_id"],
            "region": env["region"],
            "spreadsheet_id": env["spreadsheet_id"],
            "sheet_name": env["sheet_name"],
            "data_version": self.data_version(),
            "last_message_id": last_message_id,
            "replay_count": len(saved) + len(self._fixture_snapshots()),
            "workflow_result_available": bool(self.gcp_access_token()),
            "defaults": {
                "customer": _env("DEMO_CUSTOMER", "Demo Customer"),
                "pns": self._default_pns(),
                "product_line": _env("DEMO_PRODUCT_LINE", "Video"),
                "tier": _env("DEMO_TIER", "Country"),
            },
        }

    @staticmethod
    def _default_pns() -> List[str]:
        raw = (os.getenv("DEMO_DEFAULT_PNS") or "").strip()
        if raw:
            values = [item.strip() for item in raw.replace("，", ",").split(",")]
            cleaned = [item for item in values if item]
            if cleaned:
                return cleaned
        return list(DEFAULT_PNS)

    # ---------- 快照读写 ----------

    def _snapshot_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    def _write_snapshot(self, snapshot: Dict[str, Any]) -> None:
        self.ensure_dirs()
        path = self._snapshot_path(str(snapshot.get("run_id")))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_jsonable(snapshot), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _saved_snapshots(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        if not self.runs_dir.exists():
            return []
        snapshots: List[Dict[str, Any]] = []
        for path in sorted(self.runs_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(payload, dict) and payload.get("run_id"):
                payload["source"] = "live"
                snapshots.append(payload)
        snapshots.sort(key=lambda item: float(item.get("started_at") or 0.0), reverse=True)
        return snapshots[:limit]

    def _fixture_snapshots(self) -> List[Dict[str, Any]]:
        if not FIXTURES_DIR.exists():
            return []
        snapshots: List[Dict[str, Any]] = []
        for path in sorted(FIXTURES_DIR.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(payload, dict) and payload.get("run_id"):
                payload["source"] = "fixture"
                snapshots.append(payload)
        return snapshots

    def snapshot(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            live = self._live.get(run_id)
            if live is not None:
                return _jsonable(live.snapshot)
        path = self._snapshot_path(run_id)
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["source"] = "live"
            return payload
        for fixture in self._fixture_snapshots():
            if fixture.get("run_id") == run_id:
                return fixture
        raise DemoRunNotFound(f"run_id not found: {run_id}")

    def list_runs(self, *, limit: int = 50) -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = []
        for snapshot in self._saved_snapshots(limit=limit) + self._fixture_snapshots():
            rows.append(
                {
                    "run_id": snapshot.get("run_id"),
                    "scenario": snapshot.get("scenario"),
                    "started_at": snapshot.get("started_at"),
                    "final_state": snapshot.get("final_state"),
                    "total_ms": snapshot.get("total_ms"),
                    "label": snapshot.get("label") or self._label(snapshot),
                    "source": snapshot.get("source") or "live",
                }
            )
        rows.sort(key=lambda item: float(item.get("started_at") or 0.0), reverse=True)
        return {"runs": rows}

    @staticmethod
    def _label(snapshot: Dict[str, Any]) -> str:
        scenario = str(snapshot.get("scenario") or "")
        name = SCENARIO_LABELS.get(scenario, scenario or "演示")
        started_at = float(snapshot.get("started_at") or 0.0)
        stamp = datetime.fromtimestamp(started_at).strftime("%Y-%m-%d %H:%M") if started_at else "--"
        return f"{stamp} {name}"

    def artifact(self, run_id: str) -> Tuple[Path, str]:
        snapshot = self.snapshot(run_id)
        artifact = snapshot.get("artifact") or {}
        path = Path(str(artifact.get("path") or ""))
        if not artifact or not artifact.get("path") or not path.exists():
            raise DemoRunNotFound(f"run has no artifact: {run_id}")
        return path, str(artifact.get("filename") or path.name)

    # ---------- 运行 ----------

    def start_run(self, req: DemoRunReq) -> Dict[str, Any]:
        scenario = (req.scenario or "normal").strip().lower()
        if scenario not in SCENARIOS:
            raise DemoPipelineError(f"scenario must be one of {', '.join(SCENARIOS)}")

        pns = [str(pn).strip() for pn in (req.pns or []) if str(pn).strip()]
        if not pns:
            pns = self._default_pns()
        customer = (req.customer or _env("DEMO_CUSTOMER", "Demo Customer")).strip()
        product_line = (req.product_line or _env("DEMO_PRODUCT_LINE", "Video")).strip()
        tier = (req.tier or _env("DEMO_TIER", "Country")).strip()

        message_id = (req.reuse_message_id or "").strip()
        if scenario == "duplicate" and not message_id:
            message_id = self._last_message_id()
            if not message_id:
                raise DemoPipelineError("没有可复用的 message_id，请先跑一次 normal 场景")
        if not message_id:
            message_id = f"demo-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"

        run_id = f"demorun-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        started_at = _now()
        env = self.environment()
        snapshot: Dict[str, Any] = {
            "run_id": run_id,
            "scenario": scenario,
            "message_id": message_id,
            "started_at": started_at,
            "ended_at": None,
            "total_ms": None,
            "final_state": "running",
            "source": "live",
            "label": None,
            "environment": {
                "project_id": env["project_id"],
                "region": env["region"],
                "workflow_name": env["workflow_name"],
                "spreadsheet_id": env["spreadsheet_id"],
                "sheet_name": env["sheet_name"],
                "data_version": self.data_version(),
                "cloud_run_url": self.ingress_url(),
            },
            "steps": [
                {
                    **spec,
                    "state": "pending",
                    "started_at": None,
                    "ended_at": None,
                    "duration_ms": None,
                    "facts": [],
                    "request": None,
                    "response": None,
                    "links": [],
                    "note": None,
                }
                for spec in STEP_CATALOG
            ],
            "artifact": None,
        }
        snapshot["label"] = self._label(snapshot)

        run = _LiveRun(snapshot)
        with self._lock:
            if self._active_run_id is not None:
                raise DemoRunBusy(f"已有演示正在运行：{self._active_run_id}")
            self._active_run_id = run_id
            self._live[run_id] = run
            self._prune_live_locked()

        context: Dict[str, Any] = {
            "scenario": scenario,
            "pns": pns,
            "customer": customer,
            "product_line": product_line,
            "tier": tier,
            "message_id": message_id,
        }
        # 先取初态副本再启动线程，否则返回体会被后台线程改写成推进到一半的状态
        initial = _jsonable(snapshot)
        thread = threading.Thread(target=self._execute, args=(run, context), daemon=True)
        thread.start()
        return initial

    def _last_message_id(self) -> Optional[str]:
        for snapshot in self._saved_snapshots(limit=200):
            if snapshot.get("scenario") == "blocked":
                continue
            steps = {str(step.get("key")): step for step in (snapshot.get("steps") or [])}
            ingress = steps.get("ingress") or {}
            if ingress.get("state") in ("ok", "duplicate") and snapshot.get("message_id"):
                return str(snapshot.get("message_id"))
        return None

    def _prune_live_locked(self, keep: int = 10) -> None:
        if len(self._live) <= keep:
            return
        for run_id in list(self._live.keys())[: len(self._live) - keep]:
            if run_id != self._active_run_id:
                self._live.pop(run_id, None)

    # ---------- 步骤工具 ----------

    @staticmethod
    def _step(run: _LiveRun, key: str) -> Dict[str, Any]:
        for step in run.snapshot["steps"]:
            if step.get("key") == key:
                return step
        raise KeyError(key)

    def _emit(self, run: _LiveRun, step: Dict[str, Any]) -> None:
        with run.condition:
            run.events.append(
                {
                    "run_id": run.snapshot["run_id"],
                    "step": _jsonable(step),
                    "seq": len(run.events) + 1,
                }
            )
            run.condition.notify_all()

    def _begin(self, run: _LiveRun, key: str) -> Dict[str, Any]:
        step = self._step(run, key)
        step["state"] = "running"
        step["started_at"] = _now()
        self._emit(run, step)
        return step

    def _finish(
        self,
        run: _LiveRun,
        step: Dict[str, Any],
        state: str,
        *,
        facts: Optional[List[Dict[str, str]]] = None,
        note: Optional[str] = None,
        request: Optional[Dict[str, Any]] = None,
        response: Optional[Dict[str, Any]] = None,
        links: Optional[List[Dict[str, str]]] = None,
        started_at: Optional[float] = None,
        ended_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        if started_at is not None:
            step["started_at"] = started_at
        if step.get("started_at") is None:
            step["started_at"] = _now()
        step["ended_at"] = ended_at if ended_at is not None else _now()
        step["duration_ms"] = _ms(float(step["started_at"]), float(step["ended_at"]))
        step["state"] = state
        if facts is not None:
            step["facts"] = facts
        if note is not None:
            step["note"] = note
        if request is not None:
            step["request"] = _jsonable(request)
        if response is not None:
            step["response"] = _jsonable(response)
        if links is not None:
            step["links"] = links
        self._emit(run, step)
        return step

    def _skip_rest(self, run: _LiveRun, keys: List[str], note: str) -> None:
        for key in keys:
            step = self._step(run, key)
            if step.get("state") != "pending":
                continue
            now = _now()
            step["state"] = "skipped"
            step["started_at"] = now
            step["ended_at"] = now
            step["duration_ms"] = 0
            step["note"] = note
            self._emit(run, step)

    # ---------- 7 跳 ----------

    def _execute(self, run: _LiveRun, context: Dict[str, Any]) -> None:
        try:
            self._step_message(run, context)
            self._step_ingress(run, context)
            self._step_workflow(run, context)
            self._step_sheet(run, context)
            self._step_ingest_and_pricing(run, context)
            self._step_artifact(run, context)
        except Exception as exc:  # pragma: no cover - 兜底：演示进程不允许因为一跳异常而挂死
            note = f"演示管线异常终止：{type(exc).__name__}: {exc}"
            for step in run.snapshot["steps"]:
                if step.get("state") in ("pending", "running"):
                    self._finish(run, step, "error", note=note)
        finally:
            self._finalize(run)

    def _message_payload(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """构造脱敏演示消息；blocked 场景故意不写 PN 行，用来触发入口 422。"""
        lines = ["定价需求", f"需求: 端到端演示 · {SCENARIO_LABELS[context['scenario']]}"]
        if context["product_line"]:
            lines.append(f"产品线: {context['product_line']}")
        if context["scenario"] != "blocked":
            lines.append("PN: " + ", ".join(context["pns"]))
        if context["tier"]:
            lines.append(f"层级: {context['tier']}")
        if context["customer"]:
            lines.append(f"客户: {context['customer']}")
        return {
            "message_id": context["message_id"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sender_name": _env("DEMO_REQUESTER", "演示账号"),
            "text": "\n".join(lines),
        }

    def _step_message(self, run: _LiveRun, context: Dict[str, Any]) -> None:
        step = self._begin(run, "message")
        payload = self._message_payload(context)
        context["payload"] = payload
        request = {
            "method": "LOCAL",
            "url": "cloud_gateway.message_parser.parse_pricing_event",
            "body": payload,
        }
        try:
            message = parse_pricing_event(payload)
        except MessageParseError as exc:
            facts = [
                _fact("消息 ID", context["message_id"]),
                _fact("解析结果", f"不满足契约（{exc.code}）"),
                _fact("PN 列表", "（缺失）"),
                _fact("客户", context["customer"]),
                _fact("价格应用层级", context["tier"]),
                _fact("产品线", context["product_line"]),
            ]
            response = {"method": "LOCAL", "status": 422, "body": {"code": exc.code, "message": exc.detail}}
            if context["scenario"] == "blocked":
                # 这是 blocked 场景要展示的真实拦截：本地已判定不通过，仍按原样发往入口。
                self._finish(
                    run,
                    step,
                    "ok",
                    facts=facts,
                    request=request,
                    response=response,
                    note="按场景故意构造的不合契约消息（缺 PN 行），原样发往入口以展示真实拦截",
                )
                return
            self._finish(
                run,
                step,
                "error",
                facts=facts,
                request=request,
                response=response,
                note="本地解析失败，未向入口发送",
            )
            context["halt"] = "message_parse_failed"
            return

        row = message.to_sheet_row()
        context["message"] = message
        context["row"] = row
        facts = [
            _fact("消息 ID", message.message_id),
            _fact("发起人", message.requester),
            _fact("PN 列表", ", ".join(message.pns)),
            _fact("PN 数", len(message.pns)),
            _fact("客户", message.customer_name or "（未填写）"),
            _fact("价格应用层级", message.price_level or "（未填写）"),
            _fact("产品线", message.product_line or "（未填写）"),
            _fact("任务行", f"{len(row)} 列（A:N）"),
            _fact("初始状态列", f"{row[11]} / {row[12]}"),
        ]
        self._finish(
            run,
            step,
            "ok",
            facts=facts,
            request=request,
            response={"method": "LOCAL", "status": 200, "body": {"row": row}},
        )

    def _step_ingress(self, run: _LiveRun, context: Dict[str, Any]) -> None:
        if context.get("halt"):
            self._skip_rest(run, ["ingress"], "上一跳未通过，未发起真实调用")
            return
        step = self._begin(run, "ingress")
        token = self.ingress_token()
        url = f"{self.ingress_url()}/events/huachat"
        # 头里带的是机密 token，留痕只写“已配置”，绝不回显取值。
        request = {
            "method": "POST",
            "url": url,
            "headers": {"X-HuaChat-Token": "(已配置，不回显)"},
            "body": context["payload"],
        }
        if not token:
            context["halt"] = "ingress_token_missing"
            self._finish(
                run,
                step,
                "error",
                facts=[
                    _fact("服务", "pricing-huachat-ingress"),
                    _fact("区域", self.environment()["region"]),
                    _fact("鉴权", "未配置 DEMO_INGRESS_TOKEN"),
                ],
                request={**request, "headers": {"X-HuaChat-Token": "(未配置)"}},
                note="后端环境变量 DEMO_INGRESS_TOKEN 缺失，本次未发起真实网络调用；注入后重试即可实时演示",
            )
            return

        try:
            status, body = _http_post_json(url, context["payload"], {"X-HuaChat-Token": token})
        except DemoHttpError as exc:
            context["halt"] = "ingress_unreachable"
            self._finish(
                run,
                step,
                "error",
                facts=[
                    _fact("服务", "pricing-huachat-ingress"),
                    _fact("区域", self.environment()["region"]),
                    _fact("失败原因", str(exc)),
                ],
                request=request,
                note="入口不可达（网络或服务异常），后续节点未执行",
            )
            return

        body_dict = body if isinstance(body, dict) else {"raw": body}
        response = {"status": status, "body": body_dict}
        env = self.environment()
        links = [{"label": "Cloud Run 服务地址", "url": self.ingress_url()}]

        if status == 200:
            execution_name = str(body_dict.get("execution_name") or "")
            context["ingress_body"] = body_dict
            context["execution_name"] = execution_name
            facts = [
                _fact("服务", "pricing-huachat-ingress"),
                _fact("区域", env["region"]),
                _fact("HTTP 状态", status),
                _fact("消息 ID", body_dict.get("message_id")),
                _fact("PN 数", body_dict.get("pn_count")),
                _fact("派发方式", "Google Workflows" if execution_name else "直接写表"),
            ]
            if execution_name:
                facts.append(_fact("执行名", execution_name))
                facts.append(_fact("执行初始状态", body_dict.get("execution_state")))
            facts.append(_fact("submission_authorized", body_dict.get("submission_authorized")))
            self._finish(run, step, "ok", facts=facts, request=request, response=response, links=links)
            return

        if status == 422:
            detail = body_dict.get("detail") if isinstance(body_dict, dict) else None
            code = detail.get("code") if isinstance(detail, dict) else None
            message = detail.get("message") if isinstance(detail, dict) else None
            context["halt"] = "blocked"
            self._finish(
                run,
                step,
                "blocked",
                facts=[
                    _fact("服务", "pricing-huachat-ingress"),
                    _fact("区域", env["region"]),
                    _fact("HTTP 状态", status),
                    _fact("拦截原因", code or "契约校验不通过"),
                    _fact("说明", message or ""),
                ],
                request=request,
                response=response,
                links=links,
                note="入口在写入任何外部系统之前拒绝了这条消息，未触达 Workflows 与工作簿",
            )
            return

        context["halt"] = "ingress_error"
        self._finish(
            run,
            step,
            "error",
            facts=[
                _fact("服务", "pricing-huachat-ingress"),
                _fact("区域", env["region"]),
                _fact("HTTP 状态", status),
            ],
            request=request,
            response=response,
            links=links,
            note="入口返回非预期状态码，后续节点未执行",
        )

    def _fetch_execution(self, execution_name: str, access_token: str) -> Tuple[Dict[str, Any], int, Optional[Dict[str, Any]]]:
        """只读轮询 Workflows 执行，返回 (最后一次响应体, 轮询次数, 执行返回值)。"""
        url = f"{WORKFLOW_EXECUTIONS_API}/{execution_name}"
        headers = {"Authorization": f"Bearer {access_token}"}
        deadline = _now() + WORKFLOW_POLL_TIMEOUT_S
        polls = 0
        body: Dict[str, Any] = {}
        while True:
            polls += 1
            status, payload = _http_get_json(url, headers, timeout=HEALTH_TIMEOUT_S)
            body = payload if isinstance(payload, dict) else {"status": status, "raw": payload}
            if status != 200:
                body = {"http_status": status, **body}
                return body, polls, None
            state = str(body.get("state") or "")
            if state and state != "ACTIVE":
                break
            if _now() >= deadline:
                return body, polls, None
            time.sleep(WORKFLOW_POLL_INTERVAL_S)
        result = body.get("result")
        parsed: Optional[Dict[str, Any]] = None
        if isinstance(result, str):
            candidate = _loads_soft(result)
            if isinstance(candidate, dict):
                parsed = candidate
        elif isinstance(result, dict):
            parsed = result
        return body, polls, parsed

    def _step_workflow(self, run: _LiveRun, context: Dict[str, Any]) -> None:
        if context.get("halt"):
            self._skip_rest(run, ["workflow"], self._halt_note(context))
            return
        execution_name = str(context.get("execution_name") or "")
        env = self.environment()
        if not execution_name:
            # 入口如果退回 direct_sheet 模式，就没有编排这一跳，如实标 skipped。
            self._skip_rest(run, ["workflow"], "入口本次未经过 Workflows（direct_sheet 模式），无执行名")
            return

        step = self._begin(run, "workflow")
        links = [{"label": "GCP 控制台执行详情", "url": self.execution_console_url(execution_name)}]
        facts = [
            _fact("工作流", env["workflow_name"]),
            _fact("区域", env["region"]),
            _fact("执行 ID", _execution_id(execution_name)),
            _fact("执行名", execution_name),
        ]
        access_token = self.gcp_access_token()
        if not access_token:
            facts.append(_fact("执行状态", context.get("ingress_body", {}).get("execution_state") or "ACTIVE"))
            self._finish(
                run,
                step,
                "ok",
                facts=facts,
                links=links,
                note="未配置 DEMO_GCP_ACCESS_TOKEN（只读），只能确认执行已创建，未拉取执行结果",
            )
            return

        url = f"{WORKFLOW_EXECUTIONS_API}/{execution_name}"
        try:
            body, polls, result = self._fetch_execution(execution_name, access_token)
        except DemoHttpError as exc:
            facts.append(_fact("执行状态", "未知"))
            self._finish(
                run,
                step,
                "ok",
                facts=facts,
                links=links,
                request={"method": "GET", "url": url, "headers": {"Authorization": "Bearer (不回显)"}},
                note=f"执行已创建，但拉取执行结果失败：{exc}",
            )
            return

        context["execution_body"] = body
        context["execution_result"] = result
        state = str(body.get("state") or "")
        facts.append(_fact("执行状态", state or "未知"))
        facts.append(_fact("轮询次数", polls))
        if body.get("startTime"):
            facts.append(_fact("开始时间", body.get("startTime")))
        if body.get("endTime"):
            facts.append(_fact("结束时间", body.get("endTime")))
        request = {"method": "GET", "url": url, "headers": {"Authorization": "Bearer (不回显)"}}
        response = {"status": int(body.get("http_status") or 200), "body": body}

        if result is not None:
            duplicate = bool(result.get("duplicate"))
            facts.append(_fact("去重判定", "命中（message_id 已存在）" if duplicate else "未命中"))
            facts.append(_fact("阶段", result.get("stage")))
            if duplicate:
                context["duplicate"] = True
                self._finish(
                    run,
                    step,
                    "duplicate",
                    facts=facts,
                    links=links,
                    request=request,
                    response=response,
                    note="编排在扫描到相同 [HUACHAT:message_id] 标记后直接返回，未再写表",
                )
                return
            self._finish(run, step, "ok", facts=facts, links=links, request=request, response=response)
            return

        if state in ("FAILED", "CANCELLED"):
            context["halt"] = "workflow_failed"
            self._finish(
                run,
                step,
                "error",
                facts=facts,
                links=links,
                request=request,
                response=response,
                note="Workflows 执行未成功，后续节点未执行",
            )
            return

        self._finish(
            run,
            step,
            "ok",
            facts=facts,
            links=links,
            request=request,
            response=response,
            note="轮询窗口内未拿到执行返回值（执行可能仍在进行），本次不展示 updatedRange",
        )

    def _step_sheet(self, run: _LiveRun, context: Dict[str, Any]) -> None:
        if context.get("halt"):
            self._skip_rest(run, ["sheet"], self._halt_note(context))
            return
        env = self.environment()
        result = context.get("execution_result")
        ingress_body = context.get("ingress_body") or {}
        # 入口如果是直接写表模式，updatedRange 就在入口响应里。
        if result is None and ingress_body.get("updated_range") is not None:
            result = {
                "updated_range": ingress_body.get("updated_range"),
                "duplicate": bool(ingress_body.get("duplicate")),
            }

        step = self._begin(run, "sheet")
        row = context.get("row") or []
        facts = [
            _fact("目标工作簿", env["spreadsheet_id"]),
            _fact("标签页", env["sheet_name"]),
        ]
        if result is None:
            self._finish(
                run,
                step,
                "skipped",
                facts=facts,
                note="本次没有取到编排执行结果，无法展示 updatedRange；写表由 Workflows 异步完成，未观测不等于未发生",
            )
            return

        duplicate = bool(result.get("duplicate"))
        if duplicate:
            context["duplicate"] = True
            facts.append(_fact("去重判定", "命中"))
            facts.append(_fact("写入行数", 0))
            self._finish(
                run,
                step,
                "duplicate",
                facts=facts,
                response={"body": result},
                note="相同 message_id 已存在，工作簿未追加第二行",
            )
            return

        updated_range = result.get("updated_range")
        facts.append(_fact("updatedRange", updated_range or "（执行结果未包含）"))
        facts.append(_fact("写入列数", f"{len(row)} 列（A:N）"))
        facts.append(_fact("状态列", f"{row[11]} / {row[12]}" if len(row) >= 13 else ""))
        self._finish(
            run,
            step,
            "ok" if updated_range else "skipped",
            facts=facts,
            response={"body": result},
            note=None if updated_range else "执行结果里没有 updatedRange 字段",
        )

    def _step_ingest_and_pricing(self, run: _LiveRun, context: Dict[str, Any]) -> None:
        """第 5、6 跳共用一次 PricingWorkflowStore.create：建任务在前，算价在后。

        create() 内部先落任务态再回调算价，这里按算价回调的起止时刻把两段真实
        耗时拆开，两个节点的 duration_ms 都是真实测量值，只是事件在 create()
        返回后才一并广播。
        """
        if context.get("halt"):
            self._skip_rest(run, ["ingest", "pricing"], self._halt_note(context))
            return
        if context.get("duplicate"):
            self._skip_rest(run, ["ingest", "pricing"], "去重命中，链路提前收束，未建立第二个任务")
            return

        ingest = self._begin(run, "ingest")
        pns = list(context["pns"])
        message_id = context["message_id"]
        idempotency_key = f"demo:{message_id}"
        timing: Dict[str, float] = {}

        def _timed_compute(pn_list: List[str], apply_black_markup: bool) -> Dict[str, Any]:
            timing["pricing_started_at"] = _now()
            try:
                return self._compute_rows(pn_list, apply_black_markup)
            finally:
                timing["pricing_ended_at"] = _now()

        create_started_at = float(ingest["started_at"])
        try:
            task = self._workflow_store().create(
                PricingWorkflowCreateReq(
                    pns=pns,
                    source="demo-pipeline",
                    notify=False,
                    idempotency_key=idempotency_key,
                    submission_authorized=False,
                ),
                _timed_compute,
                execution_context=self._execution_context(None) if self._execution_context else None,
            )
        except Exception as exc:
            context["halt"] = "ingest_failed"
            self._finish(
                run,
                ingest,
                "error",
                facts=[_fact("幂等键", idempotency_key), _fact("失败原因", f"{type(exc).__name__}: {exc}")],
                note="任务建立失败，后续节点未执行",
            )
            self._skip_rest(run, ["pricing"], "任务建立失败")
            return

        create_ended_at = _now()
        pricing_started_at = timing.get("pricing_started_at", create_ended_at)
        pricing_ended_at = timing.get("pricing_ended_at", create_ended_at)
        context["task"] = task

        self._finish(
            run,
            ingest,
            "ok",
            facts=[
                _fact("任务 id", task.get("task_id")),
                _fact("幂等键", task.get("idempotency_key")),
                _fact("副作用键", task.get("effect_key")),
                _fact("来源", (task.get("input") or {}).get("source")),
                _fact("状态机初态", "pricing"),
                _fact("幂等重放", "是" if task.get("idempotent_replay") else "否"),
            ],
            started_at=create_started_at,
            ended_at=pricing_started_at,
        )

        pricing = self._step(run, "pricing")
        pricing["state"] = "running"
        pricing["started_at"] = pricing_started_at
        self._emit(run, pricing)

        pricing_result = task.get("pricing_result") or {}
        report = pricing_result.get("report") or {}
        items = list(report.get("items") or [])
        validation = task.get("validation") or {}
        execution_context = task.get("execution_context") or {}
        facts = [
            _fact("PN 数", report.get("count_total", len(pns))),
            _fact("未命中", report.get("count_not_found", len(list(report.get("not_found") or [])))),
            _fact("规则版本", execution_context.get("bundle_hash") or "（控制面未初始化）"),
            _fact("数据版本", self.data_version() or "（引擎未加载数据）"),
            _fact("任务状态", task.get("state")),
            _fact("校验结果", "通过" if validation.get("ok") else "，".join(validation.get("errors") or []) or "未校验"),
        ]
        for item in items[:5]:
            facts.append(
                _fact(
                    f"{item.get('pn')}",
                    " · ".join(
                        part
                        for part in (
                            f"类目 {item.get('category')}" if item.get("category") else None,
                            f"价格组 {item.get('price_group')}" if item.get("price_group") else None,
                            f"规则 {item.get('pricing_rule_name')}" if item.get("pricing_rule_name") else None,
                            f"匹配 {item.get('fr_match_mode')}/{item.get('sys_match_mode')}",
                            f"来源 {item.get('price_source')}" if item.get("price_source") else None,
                            f"FOB {item.get('fob')}" if item.get("fob") is not None else None,
                            f"DDP {item.get('ddp')}" if item.get("ddp") is not None else None,
                            f"状态 {item.get('status')}",
                        )
                        if part
                    ),
                )
            )
        if task.get("last_error"):
            facts.append(_fact("错误", task.get("last_error")))

        state = "ok" if (pricing_result.get("rows") and task.get("state") != "failed") else "error"
        note = None
        if state == "error":
            note = "算价未产出结果（引擎未加载数据或 PN 全部未命中），后续制品未生成"
            context["halt"] = "pricing_failed"
        elif not validation.get("ok"):
            note = "算价完成但校验未通过，任务停在 manual_review 等待人工复核"
        self._finish(
            run,
            pricing,
            state,
            facts=facts,
            response={"body": {"report": {k: v for k, v in report.items() if k != "items"}, "items": items}},
            note=note,
            started_at=pricing_started_at,
            ended_at=pricing_ended_at,
        )

    def _step_artifact(self, run: _LiveRun, context: Dict[str, Any]) -> None:
        if context.get("halt"):
            self._skip_rest(run, ["artifact"], self._halt_note(context))
            return
        if context.get("duplicate"):
            self._skip_rest(run, ["artifact"], "去重命中，链路提前收束，未生成制品")
            return
        task = context.get("task") or {}
        rows = list(((task.get("pricing_result") or {}).get("rows")) or [])
        step = self._begin(run, "artifact")
        if not rows:
            self._finish(run, step, "skipped", note="没有算价结果行，未生成制品")
            return

        run_id = run.snapshot["run_id"]
        out_dir = self.artifacts_dir / run_id
        try:
            frames = build_export_frames(rows)
            out_path = write_export_xlsx(frames, out_dir=out_dir, level="country")
        except Exception as exc:
            self._finish(
                run,
                step,
                "error",
                facts=[_fact("失败原因", f"{type(exc).__name__}: {exc}")],
                note="制品导出失败",
            )
            return

        size = out_path.stat().st_size
        digest = hashlib.sha256(out_path.read_bytes()).hexdigest()[:16]
        run.snapshot["artifact"] = {
            "path": str(out_path),
            "filename": out_path.name,
            "size": size,
            "sha256_16": digest,
        }
        self._finish(
            run,
            step,
            "ok",
            facts=[
                _fact("文件名", out_path.name),
                _fact("数据行数", len(rows)),
                _fact("字节数", size),
                _fact("sha256 前 16 位", digest),
                _fact("任务状态", task.get("state")),
                _fact("submission_authorized", "false"),
                _fact("人工复核边界", "制品只落到内网目录，GSP 提交仍需人工授权"),
            ],
            links=[{"label": "下载 xlsx", "url": f"/api/demo/runs/{run_id}/artifact"}],
        )

    @staticmethod
    def _halt_note(context: Dict[str, Any]) -> str:
        halt = str(context.get("halt") or "")
        return {
            "blocked": "入口拦截，未触达外部系统",
            "ingress_token_missing": "入口未配置 token，本次未发起真实调用",
            "ingress_unreachable": "入口不可达，链路在第 2 跳终止",
            "ingress_error": "入口返回非预期状态码，链路终止",
            "workflow_failed": "编排执行失败，链路终止",
            "message_parse_failed": "消息解析失败，链路终止",
            "ingest_failed": "任务建立失败，链路终止",
            "pricing_failed": "算价未产出结果，链路终止",
        }.get(halt, "上游节点未通过，本节点未执行")

    def _finalize(self, run: _LiveRun) -> None:
        snapshot = run.snapshot
        snapshot["ended_at"] = _now()
        snapshot["total_ms"] = _ms(float(snapshot["started_at"]), float(snapshot["ended_at"]))
        states = [str(step.get("state")) for step in snapshot["steps"]]
        if "error" in states:
            final_state = "error"
        elif "blocked" in states:
            final_state = "blocked"
        elif "duplicate" in states:
            final_state = "duplicate"
        else:
            final_state = "ok"
        # 先带着最终状态落盘，再把 final_state 写回内存快照。
        # 反过来的话，调用方看到 final_state 不再是 running 时文件可能还没写完，
        # 前端紧接着取快照或下载制品就会扑空。
        persisted = {**snapshot, "final_state": final_state}
        persisted["label"] = self._label(persisted)
        persist_error: Optional[str] = None
        try:
            self._write_snapshot(persisted)
        except Exception as exc:  # 落盘失败不该中断已经跑完的演示，但必须让人看见
            persist_error = f"{type(exc).__name__}: {exc}"
        snapshot["label"] = persisted["label"]
        snapshot["persist_error"] = persist_error
        snapshot["final_state"] = final_state
        with run.condition:
            run.finished = True
            run.condition.notify_all()
        with self._lock:
            if self._active_run_id == run.snapshot["run_id"]:
                self._active_run_id = None

    # ---------- SSE ----------

    def stream(self, run_id: str) -> Iterator[str]:
        with self._lock:
            run = self._live.get(run_id)
        if run is None:
            snapshot = self.snapshot(run_id)  # 找不到就抛 DemoRunNotFound
            return self._replay_stream(snapshot)
        return self._live_stream(run)

    @staticmethod
    def _replay_stream(snapshot: Dict[str, Any]) -> Iterator[str]:
        run_id = snapshot.get("run_id")
        seq = 0
        for step in snapshot.get("steps") or []:
            seq += 1
            yield _sse("step", {"run_id": run_id, "step": step, "seq": seq})
        yield _sse(
            "done",
            {
                "run_id": run_id,
                "final_state": snapshot.get("final_state"),
                "total_ms": snapshot.get("total_ms"),
            },
        )

    @staticmethod
    def _live_stream(run: _LiveRun) -> Iterator[str]:
        cursor = 0
        deadline = _now() + STREAM_IDLE_TIMEOUT_S
        while True:
            with run.condition:
                if cursor >= len(run.events) and not run.finished:
                    run.condition.wait(timeout=1.0)
                pending = run.events[cursor:]
                cursor += len(pending)
                finished = run.finished and cursor >= len(run.events)
            for event in pending:
                yield _sse("step", event)
            if finished:
                break
            if not pending:
                yield ": keepalive\n\n"
            if _now() > deadline:
                break
        snapshot = run.snapshot
        yield _sse(
            "done",
            {
                "run_id": snapshot.get("run_id"),
                "final_state": snapshot.get("final_state"),
                "total_ms": snapshot.get("total_ms"),
            },
        )
