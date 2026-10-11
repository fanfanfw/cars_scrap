"""Offline deterministic checks for the CARS-LOG-SPLIT listing log split.

No network, no DB, no browser, no secret/env reading: ``dotenv``/``psycopg2``/
``requests``/``botasaurus`` are stubbed INERT before any service/runner import, a
fake DB connection is injected for the runner tests, and all file writes go to a
temporary directory via a patched ``repo_root``. Plain assert-based script.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import logging
import re
import sys
import tempfile
import time
import types
import unittest.mock
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

KUALA_LUMPUR_TZ = ZoneInfo("Asia/Kuala_Lumpur")

STDERR_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} - INFO - marker[ _].+$")


def _install_inert_modules() -> None:
    """Block third-party side effects before any service/runner import."""

    def _unused(*args, **kwargs):
        raise RuntimeError("third-party side effect must not run in offline checks")

    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *a, **k: False
    dotenv.find_dotenv = lambda *a, **k: ""
    sys.modules["dotenv"] = dotenv

    psycopg2 = types.ModuleType("psycopg2")
    psycopg2.connect = _unused
    extras = types.ModuleType("psycopg2.extras")
    extras.Json = _unused
    extras.execute_values = _unused
    psycopg2.extras = extras
    sys.modules["psycopg2"] = psycopg2
    sys.modules["psycopg2.extras"] = extras

    requests = types.ModuleType("requests")
    requests.get = _unused
    requests.post = _unused
    requests.Timeout = TimeoutError
    requests.ConnectionError = ConnectionError
    sys.modules["requests"] = requests

    botasaurus = types.ModuleType("botasaurus")
    bbot = types.ModuleType("botasaurus.browser")
    bbot.Driver = _unused
    bbot.browser = _unused
    botasaurus.browser = bbot
    sys.modules["botasaurus"] = botasaurus
    sys.modules["botasaurus.browser"] = bbot


_install_inert_modules()

from cars_scrap_common import logging_utils  # noqa: E402
from cars_scrap_common.logging_utils import date_stamp_for, listing_log_service_name, setup_logger  # noqa: E402

SOURCES = ("carlist", "carmudi", "carsomeid", "mobil123", "olx", "mudahmy")

SERVICES = (
    ("carlist", "scrap_carlist_cars_botasaurus.carlist_service", "CarlistCarsService"),
    ("carmudi", "scrap_carmudi_cars_botasaurus.carmudi_service", "CarmudiCarsService"),
    ("carsomeid", "scrap_carsomeid_cars_botasaurus.carsomeid_service", "CarsomeIdCarsService"),
    ("mobil123", "scrap_mobil123_cars_botasaurus.mobil123_service", "Mobil123CarsService"),
    ("olx", "scrap_olx_cars_botasaurus.olx_service", "OlxCarsService"),
    ("mudahmy", "scrap_mudahmy_cars_botasaurus.mudahmy_service", "MudahMyCarsService"),
)

RUNNERS = (
    ("carlist", "scrap_carlist_cars_botasaurus.run_scraper", "CarlistCarsService"),
    ("carmudi", "scrap_carmudi_cars_botasaurus.run_scraper", "CarmudiCarsService"),
    ("carsomeid", "scrap_carsomeid_cars_botasaurus.run_scraper", "CarsomeIdCarsService"),
    ("mobil123", "scrap_mobil123_cars_botasaurus.run_scraper", "Mobil123CarsService"),
    ("olx", "scrap_olx_cars_botasaurus.run_scraper", "OlxCarsService"),
    ("mudahmy", "scrap_mudahmy_cars_botasaurus.run_scraper", "MudahMyCarsService"),
)


class _RecordingService:
    instances: list[dict] = []
    listing_calls = 0
    detail_calls = 0

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.listing_calls = 0
        cls.detail_calls = 0

    def __init__(self, **kwargs):
        _RecordingService.instances.append(dict(kwargs))

    def scrape_all_from_main(self, *args, **kwargs):
        _RecordingService.listing_calls += 1
        return []

    def scrape_listing_details(self, *args, **kwargs):
        _RecordingService.detail_calls += 1
        return []

    def scrape_listing_details_with_run_retries(self, *args, **kwargs):
        _RecordingService.detail_calls += 1
        return []

    def close(self):
        return


class _FakeCursor:
    def execute(self, *args, **kwargs):
        return None

    def fetchone(self):
        return (0,)

    def fetchall(self):
        return []

    def close(self):
        return None


class _FakeConnection:
    call_count = 0

    @classmethod
    def reset(cls):
        cls.call_count = 0

    @classmethod
    def factory(cls):
        cls.call_count += 1
        return cls()

    def cursor(self):
        return _FakeCursor()

    def close(self):
        return None


def _kl_epoch(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=KUALA_LUMPUR_TZ).timestamp()


def _fresh_logger(name):
    logger = logging.getLogger(name)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    logger.propagate = False
    return logger


def _close_logger_handlers(logger):
    for handler in list(logger.handlers):
        handler.close()


def _run_runner_with_argv(run_mod, class_name, source, argv):
    _RecordingService.reset()
    _FakeConnection.reset()
    db_mod = importlib.import_module(f"scrap_{source}_cars_botasaurus.database")
    raised = None
    with unittest.mock.patch.object(sys, "argv", ["prog", *argv]), \
            unittest.mock.patch.object(run_mod, class_name, _RecordingService), \
            unittest.mock.patch.object(db_mod, "get_connection", _FakeConnection.factory), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            run_mod.main()
        except SystemExit as exc:
            raised = exc
        except Exception as exc:  # noqa: BLE001
            raised = exc
    return _RecordingService.instances, _RecordingService.listing_calls, _RecordingService.detail_calls, _FakeConnection.call_count, raised


def test_dotenv_and_side_effects_blocked_before_import():
    assert sys.modules["dotenv"].load_dotenv() is False
    assert "psycopg2" in sys.modules
    assert "requests" in sys.modules
    assert "botasaurus" in sys.modules
    return True


def test_six_mode_log_names():
    for source in SOURCES:
        assert listing_log_service_name(source) == f"scrape_{source}_cars"
        assert listing_log_service_name(source, False) == f"scrape_{source}_cars"
        assert listing_log_service_name(source, True) == f"tracking_{source}_cars"
        assert listing_log_service_name(source, False) != listing_log_service_name(source, True)
    return True


def test_default_constructor_backward_compatible():
    for source, mod_name, class_name in SERVICES:
        module = importlib.import_module(mod_name)
        seen: list[str] = []

        def recorder(name, _seen=seen):
            _seen.append(name)
            return logging.getLogger(f"stub_{name}")

        with unittest.mock.patch.object(module, "setup_logger", recorder):
            default_service = getattr(module, class_name)()
            tracking_service = getattr(module, class_name)(tracking=True)

        assert seen == [f"scrape_{source}_cars", f"tracking_{source}_cars"], (source, seen)
        assert default_service.logger.name == f"stub_scrape_{source}_cars"
        assert tracking_service.logger.name == f"stub_tracking_{source}_cars"
        assert not hasattr(default_service, "log_mode")
        assert not hasattr(tracking_service, "log_mode")
    return True


def test_all_six_instances_file_contents_and_stderr():
    with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(
        logging_utils, "repo_root", lambda: Path(tmp)
    ):
        stamp = date_stamp_for(time.time())
        markers: dict[str, str] = {}
        instances = []
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            for source, mod_name, class_name in SERVICES:
                module = importlib.import_module(mod_name)
                cls = getattr(module, class_name)
                for tracking in (False, True):
                    svc = cls(tracking=tracking)
                    instances.append(svc)
                    name = listing_log_service_name(source, tracking)
                    marker = f"marker {name}"
                    markers[name] = marker
                    svc.logger.info("%s", marker)
            for svc in instances:
                for handler in svc.logger.handlers:
                    handler.flush()

        produced = sorted(p.name for p in Path(tmp, "logs").iterdir())
        expected = sorted(f"{name}_{stamp}.log" for name in markers)
        assert produced == expected, (produced, expected)

        err_lines = stderr.getvalue().splitlines()
        assert len(err_lines) == len(markers), err_lines
        for line in err_lines:
            assert STDERR_RE.match(line), line

        for name, marker in markers.items():
            file_lines = Path(tmp, "logs", f"{name}_{stamp}.log").read_text().splitlines()
            assert len(file_lines) == 1, (name, file_lines)
            assert marker in file_lines[0], (name, file_lines)
            # Exact one marker per file: absence of every other marker.
            text = file_lines[0]
            assert text.count(marker) == 1
            for other, other_marker in markers.items():
                if other != name:
                    assert other_marker not in text, (name, other, text)
            # Full content parity: the file line equals its stderr line.
            assert text in err_lines, (name, text)
            assert err_lines.count(text) == 1, (name, text)

        for svc in instances:
            _close_logger_handlers(svc.logger)
    return True


def test_repeat_init_output_count():
    with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(
        logging_utils, "repo_root", lambda: Path(tmp)
    ):
        name = "check_cars_log_split_repeat"
        logger = _fresh_logger(name)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            first = setup_logger(name)
        second = setup_logger(name)

        assert first is second
        assert len(second.handlers) == 2, len(second.handlers)

        second.info("two")
        for handler in second.handlers:
            handler.flush()

        stamp = date_stamp_for(time.time())
        content = Path(tmp, "logs", f"{name}_{stamp}.log").read_text()
        assert content.count("two") == 1, content
        assert buf.getvalue().count("two") == 1, buf.getvalue()
        _close_logger_handlers(second)
    return True


def test_same_instance_midnight_rotation_and_stderr():
    with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(
        logging_utils, "repo_root", lambda: Path(tmp)
    ):
        name = "check_cars_log_split_midnight"
        logger = _fresh_logger(name)

        stamps = (
            ("20251231", _kl_epoch(2025, 12, 31, 23, 59)),
            ("20260101", _kl_epoch(2026, 1, 1, 0, 0)),
            ("20260131", _kl_epoch(2026, 1, 31, 23, 59)),
            ("20260201", _kl_epoch(2026, 2, 1, 0, 0)),
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            setup_logger(name)  # stream handler must capture the redirected buffer
            for expect_stamp, ts in stamps:
                record = logging.LogRecord(name, logging.INFO, __file__, 0, f"marker_{expect_stamp}", (), None, None)
                record.created = ts
                record.msecs = 0
                logger.handle(record)
        for handler in logger.handlers:
            handler.flush()

        files = sorted(p.name for p in Path(tmp, "logs").iterdir())
        expected_files = [f"{name}_{s}.log" for s, _ in stamps]
        assert files == expected_files, files
        for expect_stamp, _ in stamps:
            text = Path(tmp, "logs", f"{name}_{expect_stamp}.log").read_text()
            assert f"marker_{expect_stamp}" in text
            for other_stamp, _ in stamps:
                if other_stamp != expect_stamp:
                    assert f"marker_{other_stamp}" not in text

        err_lines = stderr.getvalue().splitlines()
        assert len(err_lines) == len(stamps), err_lines
        for line in err_lines:
            assert STDERR_RE.match(line), line
        _close_logger_handlers(logger)
    return True


def test_rollover_recovers_after_open_failure():
    with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.object(
        logging_utils, "repo_root", lambda: Path(tmp)
    ):
        name = "check_cars_log_split_openfail"
        logger = _fresh_logger(name)
        setup_logger(name)
        handler = next(h for h in logger.handlers if isinstance(h, logging.FileHandler))

        day1 = logging.LogRecord(name, logging.INFO, __file__, 0, "marker_day1", (), None, None)
        day1.created = _kl_epoch(2026, 1, 1, 0, 0)
        day1.msecs = 0
        handler.emit(day1)
        assert handler._open_date == "20260101"
        assert handler.stream is not None

        errors: list[str] = []
        real_open = handler._open
        state = {"fail": True}

        def flaky_open():
            if state["fail"]:
                state["fail"] = False
                raise OSError("simulated open failure")
            return real_open()

        handler._open = flaky_open
        handler.handleError = lambda record: errors.append("handled")

        day2 = logging.LogRecord(name, logging.INFO, __file__, 0, "marker_day2", (), None, None)
        day2.created = _kl_epoch(2026, 1, 2, 0, 0)
        day2.msecs = 0
        handler.emit(day2)
        # Failed open: handleError invoked, state unchanged, stream reset to None.
        assert errors == ["handled"], errors
        assert handler._open_date == "20260101"
        assert handler.stream is None

        day2b = logging.LogRecord(name, logging.INFO, __file__, 0, "marker_day2b", (), None, None)
        day2b.created = _kl_epoch(2026, 1, 2, 0, 1)
        day2b.msecs = 0
        handler.emit(day2b)
        # Next record reopens and commits the new date.
        assert handler._open_date == "20260102"
        assert handler.stream is not None
        assert (Path(tmp, "logs", f"{name}_20260102.log")).exists()
        _close_logger_handlers(logger)
    return True


def test_real_argparse_flag_modes():
    track_runners = [
        ("carlist", "scrap_carlist_cars_botasaurus.run_scraper", "CarlistCarsService"),
        ("carmudi", "scrap_carmudi_cars_botasaurus.run_scraper", "CarmudiCarsService"),
        ("mobil123", "scrap_mobil123_cars_botasaurus.run_scraper", "Mobil123CarsService"),
        ("olx", "scrap_olx_cars_botasaurus.run_scraper", "OlxCarsService"),
        ("mudahmy", "scrap_mudahmy_cars_botasaurus.run_scraper", "MudahMyCarsService"),
    ]

    # Five --track-older-than: tracking mode, detail path, DB hit, no browser.
    for source, run_mod_name, class_name in track_runners:
        run_mod = importlib.import_module(run_mod_name)
        inst, listing, detail, db, raised = _run_runner_with_argv(run_mod, class_name, source, ["--track-older-than", "3m"])
        assert raised is None, (source, raised)
        assert inst == [{"tracking": True}], (source, inst)
        assert listing == 0 and detail >= 1 and db >= 1, (source, listing, detail, db)

    carsome = importlib.import_module("scrap_carsomeid_cars_botasaurus.run_scraper")

    # Carsome special --track-last-scraped-older-than: tracking.
    inst, listing, detail, db, raised = _run_runner_with_argv(carsome, "CarsomeIdCarsService", "carsomeid", ["--track-last-scraped-older-than", "1d"])
    assert raised is None, raised
    assert inst == [{"tracking": True}], inst
    assert listing == 0 and detail >= 1 and db >= 1, (listing, detail, db)

    # Carsome count aliases both trigger tracking but return before scraping.
    for flag in ("--track-count", "--track-count-only"):
        inst, listing, detail, db, raised = _run_runner_with_argv(
            carsome, "CarsomeIdCarsService", "carsomeid", ["--track-last-scraped-older-than", "1d", flag]
        )
        assert raised is None, (flag, raised)
        assert inst == [{"tracking": True}], (flag, inst)
        assert listing == 0 and detail == 0 and db >= 1, (flag, listing, detail, db)

    # Nontrigger: --track-last-scraped-older-than alone in the five stays listing.
    for source, run_mod_name, class_name in track_runners:
        run_mod = importlib.import_module(run_mod_name)
        inst, listing, detail, db, raised = _run_runner_with_argv(run_mod, class_name, source, ["--track-last-scraped-older-than", "1d"])
        assert raised is None, (source, raised)
        assert inst == [{"tracking": False}], (source, inst)
        assert listing >= 1 and detail == 0 and db == 0, (source, listing, detail, db)

    # Invalid combination: tracking logger chosen before the error, no browser/DB.
    for source, run_mod_name, class_name in track_runners:
        run_mod = importlib.import_module(run_mod_name)
        inst, listing, detail, db, raised = _run_runner_with_argv(run_mod, class_name, source, ["--track-older-than", "3m", "--details"])
        assert isinstance(raised, ValueError), (source, raised)
        assert inst == [{"tracking": True}], (source, inst)
        assert listing == 0 and detail == 0 and db == 0, (source, listing, detail, db)

    inst, listing, detail, db, raised = _run_runner_with_argv(
        carsome, "CarsomeIdCarsService", "carsomeid", ["--track-last-scraped-older-than", "1d", "--details"]
    )
    assert isinstance(raised, ValueError), raised
    assert inst == [{"tracking": True}]
    assert listing == 0 and detail == 0 and db == 0
    return True


def main() -> int:
    checks = (
        test_dotenv_and_side_effects_blocked_before_import,
        test_six_mode_log_names,
        test_default_constructor_backward_compatible,
        test_all_six_instances_file_contents_and_stderr,
        test_repeat_init_output_count,
        test_same_instance_midnight_rotation_and_stderr,
        test_rollover_recovers_after_open_failure,
        test_real_argparse_flag_modes,
    )
    failed = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - surface failure, stay offline
            failed += 1
            print(f"FAIL {check.__name__}: {exc}")
        else:
            print(f"PASS {check.__name__}")
    print("network_access: never_performed; db_access: faked; browser: never_launched; dotenv: inert")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
