# -*- coding: utf-8 -*-
import logging, re
from PySide6.QtCore import QThread, Signal

from app.ui.guide.guide_utils import _is_heredoc

logger = logging.getLogger("[GuideWorkers]")
if not hasattr(logger, "warn"):
    logger.warn = logger.warning


class _FnWorker(QThread):
    result = Signal(object)
    error = Signal(str)
    _seq = 0
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        _FnWorker._seq += 1
        self.id = f"FnWorker#{_FnWorker._seq}"
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            r = self.fn(*self.args, **self.kwargs)
            self.result.emit(r)
        except Exception as e:
            logger.error(f"[{self.id}] falhou: {e}", exc_info=True)
            self.error.emit(str(e))


class _StreamWorker(QThread):
    line = Signal(str)
    finished_ok = Signal()
    error = Signal(str)

    _seq = 0

    def __init__(self, ssh, host, cmd, timeout_s=120):
        super().__init__()
        _StreamWorker._seq += 1
        self.id = f"StreamWorker#{_StreamWorker._seq}"
        self.ssh = ssh
        self.host = host
        self.cmd = cmd
        self.timeout_s = timeout_s
        self._stop_flag = False
        self._last_rc = None

    def _emit_chunk(self, text: str):
        if not text:
            return
        self.line.emit(text)
        m = re.search(r"\[guide\]\s*__RC=(\d+)", text)
        if m:
            try:
                self._last_rc = int(m.group(1))
            except Exception:
                self._last_rc = 1

    def stop(self):
        self._stop_flag = True
        logger.error(f"[{self.id}] stop solicitado")

    def run(self):
        try:
            wrapped = "{ " + self.cmd + " ; } ; rc=$?; printf \"\\n[guide] __RC=%s\\n\" \"$rc\"; exit $rc"
            if hasattr(self.ssh, "run_command_stream"):
                for chunk in self.ssh.run_command_stream(self.host, wrapped, timeout_s=self.timeout_s):
                    if self._stop_flag:
                        break
                    self._emit_chunk(chunk)
            else:
                self._run_no_stream()
        except Exception as e:
            self.error.emit(str(e))
            return

        if self._stop_flag:
            self.error.emit("Cancelado")
            return

        rc = 0 if self._last_rc is None else self._last_rc
        if rc != 0:
            self.error.emit(f"Comando falhou (rc={rc})")
            return

        self.finished_ok.emit()

    def _run_no_stream(self, use_classic: bool = False):
        try:
            if _is_heredoc(self.cmd):
                out = self.ssh.run_command(self.host, self.cmd, timeout=self.timeout_s)
            else:
                out = (
                    self.ssh.run_command(self.host, f"bash -lc '{self.cmd}'", timeout=self.timeout_s)
                    if use_classic else
                    self.ssh.run_command_cancellable(self.host, self.cmd, timeout_s=self.timeout_s)
                )
            self._emit_block_output(out)
        except Exception as e:
            self.error.emit(str(e))

    def _emit_block_output(self, out):
        try:
            if out is None:
                return
            if isinstance(out, str):
                for line in out.splitlines():
                    self.line.emit(line)
                return
            if isinstance(out, tuple) and len(out) >= 2:
                stdout, stderr = out[0] or "", out[1] or ""
                for line in str(stdout).splitlines():
                    self.line.emit(f"[stdout] {line}")
                for line in str(stderr).splitlines():
                    self.line.emit(f"[stderr] {line}")
                return
            if isinstance(out, dict):
                stdout, stderr = out.get("stdout", ""), out.get("stderr", "")
                for line in str(stdout).splitlines():
                    self.line.emit(f"[stdout] {line}")
                for line in str(stderr).splitlines():
                    self.line.emit(f"[stderr] {line}")
                return
            self.line.emit(str(out))
        except Exception as e:
            logger.error(f"[{self.id}] _emit_block_output: {e}")
