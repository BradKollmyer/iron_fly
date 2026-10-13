from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from iron_fly import desk as desk_mod
from iron_fly.config import MODES, Settings
from iron_fly.current_config import current as current_config
from iron_fly.db import SESSION_TTL, connect, migrate
from iron_fly.desk import (
    CONFIG_MODES,
    apply_spec,
    bind_book_day,
    bind_chain,
    bind_player,
    clear_flies,
    close_strike,
    flatten,
    load_backtest,
    load_day_tape,
    load_marks,
    open_strike,
    preview_fly,
    replay_until,
    reset_player,
    seek_tape,
    set_policy,
    set_switches,
    set_wings,
    sheet,
    watch_frame,
)
from iron_fly.domain.market import DeskMarks
from iron_fly.engine import last_step, session_tape
from iron_fly.fly_monitor import latest_chain
from iron_fly.strategy.spec import parse_spec

log = logging.getLogger("iron_fly")

NOTICES = {
    "live": "Live sends are off. This desk is marks only.",
    "auto": "That action is automatic. Turn Auto Open or Auto Drop off to do it by hand.",
    "nomid": "No combo mid for that fly.",
    "missing": "That fly is not open.",
    "conflict": "That open would sell a contract already held long.",
    "nobid": "No bid to sell that leftover long.",
    "already closing": "That fly is already closing.",
}
_ET = ZoneInfo("America/New_York")

PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")




def _playhead(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromtimestamp(int(float(raw)), tz=_ET)
    except (TypeError, ValueError, OSError):
        return None


def _form_day(raw: object) -> date | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def tokens_match(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return secrets.compare_digest(left.encode(), right.encode())


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        conn = connect(cfg.db_path)
        try:
            migrate(conn)
            desk_mod.prepare_desk(conn, cfg)
            if not cfg.is_backtest:
                desk_mod.roll_session(conn)
            conn.commit()
        finally:
            conn.close()
        stop_loop = threading.Event()
        step_log = logging.getLogger("iron_fly.step")
        orders_log = logging.getLogger("iron_fly.orders")
        for named in (step_log, orders_log):
            named.setLevel(logging.INFO)
            if not any(getattr(handler, "_iron_fly_step", False) for handler in named.handlers):
                handler = logging.StreamHandler()
                handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
                handler._iron_fly_step = True  # type: ignore[attr-defined]
                named.addHandler(handler)
            named.propagate = False
        # Public desk is replay/backtest only; no live quote feeds.
        config_thread = None
        fly_thread = None
        engine_thread = None
        stream_thread = None
        if cfg.is_public:
            desk_mod.warm_shared_replay()
            threading.Thread(target=desk_mod.warm_default_day, name="replay-warm", daemon=True).start()
        yield
        stop_loop.set()
        for thread in (config_thread, fly_thread, engine_thread, stream_thread):
            if thread is not None:
                thread.join(timeout=2)

    app = FastAPI(title="Captain's Log", lifespan=lifespan)
    app.state.settings = cfg

    def db_conn() -> Iterator[sqlite3.Connection]:
        conn = connect(cfg.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def page(request: Request, name: str, context: dict):
        context["public"] = cfg.is_public
        return templates.TemplateResponse(request, name, context)

    def csrf_token(request: Request, conn: sqlite3.Connection) -> str:
        del conn
        token = request.session.get("csrf")
        return token if isinstance(token, str) else ""

    def require_csrf(request: Request, conn: sqlite3.Connection, csrf: str) -> None:
        if not tokens_match(csrf, csrf_token(request, conn)):
            raise HTTPException(status_code=400, detail="invalid form token")

    def signed_in_user(request: Request, conn: sqlite3.Connection):
        del request, conn
        return SimpleNamespace(email="", name="Guest")
    def refuse_public() -> None:
        raise HTTPException(status_code=404, detail="not available")

    def login_page(request: Request, notice: str | None = None):
        """Public desk has no login page."""
        del request, notice
        raise HTTPException(status_code=404, detail="not available")

    @app.get("/health")
    def health():
        return PlainTextResponse("ok")

    @app.get("/refresh")
    def desk_refresh(request: Request, conn: sqlite3.Connection = Depends(db_conn), after: int | None = None):
        if signed_in_user(request, conn) is None:
            return JSONResponse({"ladder": [], "flies": []}, status_code=401)
        if cfg.is_backtest:
            return JSONResponse({"ladder": [], "flies": [], "manual": True, "tape": []})
        try:
            marks = _live_marks(conn)
        except Exception:
            log.warning("refresh marks failed", exc_info=True)
            marks = DeskMarks(error="Quotes are unavailable", source=cfg.desk_mode)
        frame = watch_frame(conn, cfg, marks)
        frame["tape"] = _chart_tape(after)
        return JSONResponse(frame)

    def _chart_tape(after: int | None) -> list:
        """SPX prints for the chart. Public desk uses the session tape only."""
        return session_tape(after)

    def _live_marks(conn: sqlite3.Connection) -> DeskMarks:
        """The threads' latest SPX, chain, and config. A quiet book still reads the database."""
        step = last_step(conn)
        config = current_config(conn)
        marks = DeskMarks(source="memory", ts=datetime.now(_ET))
        raw = step.get("spx") if isinstance(step, dict) else None
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            marks.last = float(raw)
        marks.chain = latest_chain()
        if marks.last is None and marks.chain is None and desk_mod.quotes_session():
            marks = load_marks(cfg, conn, force=True)
        _apply_config_marks(marks, config)
        return marks

    def _apply_config_marks(marks: DeskMarks, config) -> None:
        """Open EM and current EM come from the config feed when present."""
        if config.em_open is not None:
            marks.em_open = config.em_open
        if config.em_now is not None:
            marks.em_now = config.em_now
        if config.em_pct is not None:
            marks.em_pct = config.em_pct
        if config.put40_wings is not None:
            marks.put_40_width = config.put40_wings

    @app.get("/")
    def home(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
        user = signed_in_user(request, conn)
        if user is not None:
            notice = request.query_params.get("notice")
            if cfg.is_backtest:
                raw_day = request.query_params.get("date")
                day = None
                if isinstance(raw_day, str) and raw_day:
                    try:
                        day = date.fromisoformat(raw_day)
                    except ValueError:
                        day = None
                try:
                    marks, days, chosen = load_backtest(cfg, day)
                except Exception:
                    log.warning("backtest tape failed", exc_info=True)
                    marks, days, chosen = DeskMarks(error="Quotes are unavailable", source="backtest"), [], ""
            else:
                try:
                    marks = load_marks(cfg, conn)
                except Exception:
                    log.warning("quotes failed", exc_info=True)
                    marks = DeskMarks(error="Quotes are unavailable", source=cfg.desk_mode)
                _apply_config_marks(marks, current_config(conn))
                days, chosen = [], ""
            playhead = _playhead(request.query_params.get("at"))
            if chosen:
                bind_book_day(conn, chosen)
            if cfg.is_backtest and chosen and playhead is not None:
                try:
                    replay_until(conn, cfg, date.fromisoformat(chosen), playhead)
                except Exception:
                    log.warning("backtest replay failed", exc_info=True)
            if playhead is not None:
                seek_tape(marks, int(playhead.timestamp()))
                if cfg.is_backtest and chosen:
                    bind_chain(cfg, marks, date.fromisoformat(chosen), playhead)
            model = sheet(conn, cfg, marks)
            model["tape_days"] = days
            model["tape_day"] = chosen
            model["player"] = cfg.is_backtest
            if model["player"]:
                model["em_now"] = None
                model["em_pct"] = None
                model["put_40_width"] = None
            spec_error = request.session.pop("spec_error", "")
            spec_applied = bool(request.session.pop("spec_applied", False))
            return page(
                request,
                "home.html",
                {
                    "user": user,
                    "csrf": csrf_token(request, conn),
                    "sheet": model,
                    "notice": NOTICES.get(notice, notice) if isinstance(notice, str) and notice else "",
                    "spec_error": spec_error if isinstance(spec_error, str) else "",
                    "spec_applied": spec_applied,
                },
            )
        notice = request.query_params.get("notice")
        return login_page(request, notice if isinstance(notice, str) else None)

    @app.get("/auth/google")
    async def auth_google():
        refuse_public()

    @app.get("/auth/callback")
    async def auth_callback():
        refuse_public()

    @app.post("/logout")
    def logout():
        refuse_public()

    @app.get("/settings")
    def settings_page():
        raise HTTPException(status_code=404, detail="not available")

    def desk_marks(conn: sqlite3.Connection) -> DeskMarks:
        try:
            return load_marks(cfg, conn, force=True)
        except Exception:
            log.warning("quotes failed", exc_info=True)
            return DeskMarks(error="Quotes are unavailable", source=cfg.desk_mode)

    def wants_frame(request: Request) -> bool:
        return "application/json" in (request.headers.get("accept") or "")

    def action_frame(
        conn: sqlite3.Connection,
        tape_day: str,
        at: str,
        code: str | None = None,
        **extra: object,
    ) -> JSONResponse:
        when = _playhead(at)
        day = _form_day(tape_day)
        marks = backtest_marks(day, when) if cfg.is_backtest and day is not None else desk_marks(conn)
        frame = watch_frame(conn, cfg, marks)
        if when is not None:
            frame["at"] = int(when.timestamp())
        if code:
            frame["notice"] = NOTICES.get(code, code)
        frame.update(extra)
        return JSONResponse(frame)

    def desk_redirect(code: str | None, tape_day: str = "", at: str = "") -> RedirectResponse:
        params: list[tuple[str, str]] = []
        if cfg.is_backtest and tape_day:
            try:
                params.append(("date", date.fromisoformat(tape_day).isoformat()))
            except ValueError:
                pass
        if at:
            params.append(("at", at))
        if code:
            params.append(("notice", code))
        target = "/" if not params else "/?" + urlencode(params)
        return RedirectResponse(target, status_code=303)

    def backtest_marks(day: date | None, when: datetime | None) -> DeskMarks:
        try:
            marks = load_day_tape(cfg, day) if day is not None else load_backtest(cfg, day)[0]
        except Exception:
            log.warning("backtest tape failed", exc_info=True)
            return DeskMarks(error="Quotes are unavailable", source="backtest")
        if when is not None:
            seek_tape(marks, int(when.timestamp()))
            if day is not None:
                bind_chain(cfg, marks, day, when)
        return marks

    @app.post("/desk/reset")
    def desk_reset(
        request: Request,
        tape_day: str = Form(""),
        at: str = Form(""),
        csrf: str = Form(""),
        conn: sqlite3.Connection = Depends(db_conn),
    ):
        if signed_in_user(request, conn) is None:
            return RedirectResponse("/", status_code=303)
        require_csrf(request, conn, csrf)
        if cfg.is_backtest:
            clear_flies(conn)
        if wants_frame(request):
            return action_frame(conn, tape_day, at, reset=True)
        params: list[tuple[str, str]] = [("reset", "1")]
        if tape_day:
            try:
                params.insert(0, ("date", date.fromisoformat(tape_day).isoformat()))
            except ValueError:
                pass
        return RedirectResponse("/?" + urlencode(params), status_code=303)

    @app.post("/desk/preview")
    def desk_preview(
        request: Request,
        strike: str = Form(""),
        wings: str = Form(""),
        csrf: str = Form(""),
        conn: sqlite3.Connection = Depends(db_conn),
    ):
        if signed_in_user(request, conn) is None:
            return JSONResponse({"notice": "Sign in"}, status_code=401)
        require_csrf(request, conn, csrf)
        try:
            k = float(strike)
            width = int(wings)
        except ValueError:
            return JSONResponse({"notice": "Choose a strike."})
        marks = desk_marks(conn)
        try:
            notice = preview_fly(conn, cfg, marks, k, width)
        except LookupError as exc:
            notice = str(exc)
        return JSONResponse({"notice": notice})

    @app.post("/desk/open")
    def desk_open(
        request: Request,
        strike: str = Form(""),
        wings: str = Form(""),
        credit: str = Form(""),
        tape_day: str = Form(""),
        at: str = Form(""),
        csrf: str = Form(""),
        conn: sqlite3.Connection = Depends(db_conn),
    ):
        if signed_in_user(request, conn) is None:
            return RedirectResponse("/", status_code=303)
        require_csrf(request, conn, csrf)
        try:
            k = float(strike)
        except ValueError:
            return desk_redirect("missing", tape_day, at)
        width = None
        premium = None
        if wings:
            try:
                width = int(wings)
            except ValueError:
                width = None
        if credit:
            try:
                premium = float(credit)
            except ValueError:
                premium = None
        when = _playhead(at)
        day = _form_day(tape_day)
        marks = backtest_marks(day, when) if cfg.is_backtest else desk_marks(conn)
        code = open_strike(conn, cfg, marks, k, now=when, day=day, wings=width, credit=premium)
        if wants_frame(request):
            return action_frame(conn, tape_day, at, code)
        return desk_redirect(code, tape_day, at)

    @app.post("/desk/close")
    def desk_close(
        request: Request,
        strike: str = Form(""),
        tape_day: str = Form(""),
        at: str = Form(""),
        csrf: str = Form(""),
        conn: sqlite3.Connection = Depends(db_conn),
    ):
        if signed_in_user(request, conn) is None:
            return RedirectResponse("/", status_code=303)
        require_csrf(request, conn, csrf)
        try:
            k = float(strike)
        except ValueError:
            return desk_redirect("missing", tape_day, at)
        when = _playhead(at)
        day = _form_day(tape_day)
        marks = backtest_marks(day, when) if cfg.is_backtest else desk_marks(conn)
        code = close_strike(conn, cfg, marks, k, now=when, day=day)
        if wants_frame(request):
            return action_frame(conn, tape_day, at, code)
        return desk_redirect(code, tape_day, at)

    @app.post("/desk/flatten")
    def desk_flatten(
        request: Request,
        tape_day: str = Form(""),
        at: str = Form(""),
        csrf: str = Form(""),
        conn: sqlite3.Connection = Depends(db_conn),
    ):
        if signed_in_user(request, conn) is None:
            return RedirectResponse("/", status_code=303)
        require_csrf(request, conn, csrf)
        when = _playhead(at)
        day = _form_day(tape_day)
        marks = backtest_marks(day, when) if cfg.is_backtest else desk_marks(conn)
        code = flatten(conn, cfg, marks, now=when, day=day)
        if wants_frame(request):
            return action_frame(conn, tape_day, at, code)
        return desk_redirect(code, tape_day, at)

    @app.post("/desk/leftover-close")
    def desk_leftover_close(
        request: Request,
        occ: str = Form(""),
        tape_day: str = Form(""),
        at: str = Form(""),
        csrf: str = Form(""),
        conn: sqlite3.Connection = Depends(db_conn),
    ):
        if signed_in_user(request, conn) is None:
            return RedirectResponse("/", status_code=303)
        require_csrf(request, conn, csrf)
        symbol = occ or request.query_params.get("occ") or ""
        when = _playhead(at)
        day = _form_day(tape_day)
        marks = backtest_marks(day, when) if cfg.is_backtest else desk_marks(conn)
        from iron_fly.orders import close_leftover_long

        chain = marks.chain
        code = close_leftover_long(conn, cfg, chain, symbol.strip())
        if wants_frame(request):
            return action_frame(conn, tape_day, at, code)
        return desk_redirect(code, tape_day, at)

    @app.post("/desk/switches")
    def desk_switches(
        request: Request,
        use_config: str = Form("0"),
        auto_open: str = Form("0"),
        auto_drop: str = Form("0"),
        tape_day: str = Form(""),
        at: str = Form(""),
        csrf: str = Form(""),
        conn: sqlite3.Connection = Depends(db_conn),
    ):
        if signed_in_user(request, conn) is None:
            return RedirectResponse("/", status_code=303)
        require_csrf(request, conn, csrf)
        set_switches(conn, use_config == "1", auto_open == "1", auto_drop == "1")
        if wants_frame(request):
            return action_frame(conn, tape_day, at)
        return desk_redirect(None, tape_day, at)

    @app.post("/desk/policy")
    def desk_policy(
        request: Request,
        regime: str = Form(""),
        stop_pad: str = Form(""),
        wings: str = Form(""),
        run_mode: str = Form(""),
        tape_day: str = Form(""),
        at: str = Form(""),
        csrf: str = Form(""),
        conn: sqlite3.Connection = Depends(db_conn),
    ):
        if signed_in_user(request, conn) is None:
            return RedirectResponse("/", status_code=303)
        require_csrf(request, conn, csrf)
        pad = None
        if stop_pad != "":
            try:
                pad = int(stop_pad)
            except ValueError:
                pad = None
        width = None
        if wings != "":
            try:
                width = int(wings)
            except ValueError:
                width = None
        set_policy(conn, regime, pad if pad is not None else -1, run_mode or None, width)
        if wants_frame(request):
            return action_frame(conn, tape_day, at)
        return desk_redirect(None, tape_day, at)

    @app.post("/desk/wings")
    def desk_wings(
        request: Request,
        wings: str = Form(""),
        csrf: str = Form(""),
        conn: sqlite3.Connection = Depends(db_conn),
    ):
        if signed_in_user(request, conn) is None:
            return JSONResponse({"ok": False}, status_code=401)
        require_csrf(request, conn, csrf)
        try:
            set_wings(conn, int(wings))
        except ValueError:
            pass
        return JSONResponse({"ok": True})

    @app.post("/desk/step")
    def desk_step(
        request: Request,
        at: str = Form(""),
        duration: str = Form("0"),
        tape_day: str = Form(""),
        csrf: str = Form(""),
        conn: sqlite3.Connection = Depends(db_conn),
    ):
        if signed_in_user(request, conn) is None:
            return JSONResponse({"ladder": [], "flies": []}, status_code=401)
        require_csrf(request, conn, csrf)
        day = _form_day(tape_day)
        when = _playhead(at)
        if not cfg.is_backtest or day is None or when is None:
            return JSONResponse({"ladder": [], "flies": [], "manual": True})
        try:
            seconds = max(0, int(float(duration)))
        except ValueError:
            seconds = 0
        end = when if seconds == 0 else when + timedelta(seconds=seconds)
        step_log = logging.getLogger("iron_fly.step")
        step_log.info(
            "step POST day=%s at=%s duration=%ss until=%s",
            day.isoformat(),
            when.astimezone(_ET).strftime("%H:%M:%S"),
            seconds,
            end.astimezone(_ET).strftime("%H:%M:%S"),
        )
        try:
            end = replay_until(conn, cfg, day, end)
        except Exception:
            log.warning("backtest replay failed", exc_info=True)
        frame = watch_frame(conn, cfg, backtest_marks(day, end))
        frame["at"] = int(end.timestamp())
        flies = (
            " ".join(f"{row.get('slot')}:{row.get('strike')}:{row.get('status')}" for row in frame.get("flies") or [])
            or "none"
        )
        step_log.info(
            "step return flies=%s pnl=%s watch=%s",
            flies,
            frame.get("pnl"),
            len(frame.get("ladder") or []),
        )
        return JSONResponse(frame)

    @app.get("/desk/chain")
    def desk_chain(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
        if signed_in_user(request, conn) is None:
            return JSONResponse({"strikes": [], "quotes": {}}, status_code=401)
        if cfg.is_backtest:
            day = _form_day(request.query_params.get("date") or "")
            when = _playhead(request.query_params.get("at"))
            if day is None or when is None:
                return JSONResponse({"strikes": [], "quotes": {}, "spot": None})
            packet = desk_mod.chain_packet(desk_mod.chain_asof(cfg, day, when))
            marks = backtest_marks(day, when)
            packet["spot"] = marks.last
            packet["source"] = "cache"
            return JSONResponse(packet)
        marks = desk_marks(conn)
        packet = desk_mod.chain_packet(marks.chain)
        packet["spot"] = marks.last
        packet["source"] = marks.source or "replay"
        if marks.error:
            packet["error"] = marks.error
        return JSONResponse(packet)

    @app.get("/desk/frame")
    def desk_frame(request: Request, conn: sqlite3.Connection = Depends(db_conn)):
        if signed_in_user(request, conn) is None:
            return JSONResponse({"ladder": [], "flies": []}, status_code=401)
        day = _form_day(request.query_params.get("date") or "")
        when = _playhead(request.query_params.get("at"))
        if not cfg.is_backtest or day is None:
            return JSONResponse({"ladder": [], "flies": [], "manual": True})
        return JSONResponse(watch_frame(conn, cfg, backtest_marks(day, when)))

    @app.post("/desk/config")
    def desk_config(
        request: Request,
        spec: str = Form(""),
        tape_day: str = Form(""),
        csrf: str = Form(""),
        conn: sqlite3.Connection = Depends(db_conn),
    ):
        if signed_in_user(request, conn) is None:
            return RedirectResponse("/", status_code=303)
        require_csrf(request, conn, csrf)
        target = "/"
        chosen = ""
        if tape_day:
            try:
                chosen = date.fromisoformat(tape_day).isoformat()
            except ValueError:
                chosen = ""
        if cfg.is_backtest and chosen:
            target = "/?date=" + chosen
        if cfg.desk_mode not in CONFIG_MODES:
            if wants_frame(request):
                return action_frame(conn, tape_day, "", "config")
            return RedirectResponse(target, status_code=303)
        try:
            parsed = parse_spec(spec)
        except ValueError as exc:
            if wants_frame(request):
                return action_frame(conn, tape_day, "", str(exc), applied=False)
            request.session["spec_error"] = str(exc)
            request.session.pop("spec_applied", None)
            return RedirectResponse(target, status_code=303)
        apply_spec(conn, parsed)
        request.session.pop("spec_error", None)
        if wants_frame(request):
            return action_frame(conn, tape_day, "", applied=True, spec_summary=parsed.summary())
        request.session["spec_applied"] = True
        return RedirectResponse(target, status_code=303)

    @app.middleware("http")
    async def assign_player(request: Request, call_next):
        player = "owner"
        if cfg.is_public:
            found = request.session.get("player")
            if not isinstance(found, str) or len(found) < 8:
                found = secrets.token_urlsafe(18)
                request.session["player"] = found
            if not isinstance(request.session.get("csrf"), str) or not request.session.get("csrf"):
                request.session["csrf"] = secrets.token_urlsafe(32)
            player = found
        token = bind_player(player)
        try:
            return await call_next(request)
        finally:
            reset_player(token)

    static_dir = PACKAGE_DIR / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.session_secret,
        session_cookie="public" if cfg.is_public else "session",
        https_only=cfg.https,
        same_site="lax",
        max_age=int((timedelta(days=180) if cfg.is_public else SESSION_TTL).total_seconds()),
    )
    return app




def hydrate_replay_cache(*, required: bool = False) -> None:
    """Pull the newest REPLAY_BOOTSTRAP_DAYS from R2 into REPLAY_CACHE.

    Used by `main`, Docker entrypoint, and `make run-local`. Skipped when
    REPLAY_HYDRATE=0. Tests call create_app directly and do not hit the network.
    """
    flag = os.environ.get("REPLAY_HYDRATE", "1").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return
    from iron_fly.replay_store import bootstrap_days, default_replay_dir, hydrate

    root = default_replay_dir()
    last = bootstrap_days()
    try:
        downloaded, skipped, errors = hydrate(root, last=last, continue_on_error=True)
        log.info(
            "replay hydrate dir=%s last=%s downloaded=%s skipped=%s errors=%s",
            root,
            last,
            downloaded,
            skipped,
            len(errors),
        )
        for item in errors[:5]:
            log.warning("replay hydrate %s", item)
    except Exception:
        have = list(root.glob("????-??-??.json")) if root.is_dir() else []
        if have and not required:
            log.exception("replay hydrate failed; using %s local day(s)", len(have))
            return
        raise


def _load_dotenv() -> None:
    """Load repo `.env` into os.environ when keys are unset (host `make run-local`)."""
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    _load_dotenv()
    mode = os.environ.get("DESK_MODE", "backtest-public").strip().lower()
    if mode in MODES:
        hydrate_replay_cache(required=False)
    raw = os.environ.get("LISTEN_PORT", "8000").strip() or "8000"
    uvicorn.run(create_app(), host="0.0.0.0", port=int(raw))
