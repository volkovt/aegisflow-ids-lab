from __future__ import annotations
from typing import Callable, Set
import threading
import logging

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget, QProgressBar, QToolButton

from app.ui.components.spinner_animation import _SpinnerAnimator


class TaskManager:
    """
    Orquestrador genérico de Workers/ResultWorkers.
    - Liga/desliga spinner global e de botões
    - Gerencia ciclo de vida (adicionar, remoção, cancelamento e quiesce)
    - Emite logs via callback (append_log)
    """

    def __init__(
        self,
        *,
        global_progress: QProgressBar,
        status_target: QWidget,
        append_log: Callable[[str], None],
        logger: logging.Logger,
    ) -> None:
        self._workers: Set[object] = set()
        self._lock = threading.RLock()
        self.global_progress = global_progress
        self.status_target = status_target
        self.append_log = append_log
        self.logger = logger
        self._status_spinner: _SpinnerAnimator | None = None

    # ---------------- Busy/Spinner -----------------
    def _on_worker_start(self, tag: str) -> None:
        try:
            self.global_progress.setVisible(True)
            msg = f"{tag or 'tarefa'} em execução…"
            if self._status_spinner is None:
                self._status_spinner = _SpinnerAnimator(self.status_target, msg)
            else:
                self._status_spinner.base_text = msg
            self._status_spinner.start()
        except Exception as e:
            self.append_log(f"[WARN] TaskManager._on_worker_start: {e}")

    def _on_worker_done(self, tag: str, w: object) -> None:
        try:
            with self._lock:
                self._workers.discard(w)
            if not self._workers:
                self.global_progress.setVisible(False)
                if self._status_spinner:
                    self._status_spinner.stop("")
        except Exception as e:
            self.append_log(f"[WARN] TaskManager._on_worker_done: {e}")

    def wire_button(self, btn: QWidget, worker: object, *, active_label: str, idle_label: str) -> None:
        """Conecta spinner a um botão (ou status bar)."""
        try:
            target = btn if not isinstance(btn, QToolButton) else self.status_target
            spinner = _SpinnerAnimator(target, active_label)
            spinner.start()

            def _restore():
                try:
                    spinner.stop("" if target is self.status_target else idle_label)
                    if target is self.status_target and hasattr(btn, "setText"):
                        btn.setText(idle_label)
                except Exception as e:
                    self.append_log(f"[WARN] TaskManager._restore: {e}")

            # Workers têm sinais .done/.error
            try:
                worker.done.connect(_restore)
            except Exception:
                pass
            try:
                worker.error.connect(lambda _msg: _restore())
            except Exception:
                pass
        except Exception as e:
            self.append_log(f"[WARN] TaskManager.wire_button: {e}")

    # ---------------- Lifecycle -----------------
    def keep(self, worker: object, *, tag: str = "") -> None:
        try:
            with self._lock:
                self._workers.add(worker)
            self._on_worker_start(tag)
            try:
                worker.done.connect(lambda: self._on_worker_done(tag, worker))
            except Exception:
                pass
            self.append_log(f"[Thread] iniciado {tag or worker}")
        except Exception as e:
            self.append_log(f"[WARN] TaskManager.keep: {e}")

    def cancel_worker(self, w: object, *, reason: str = "") -> None:
        try:
            if hasattr(w, "request_cancel"):
                w.request_cancel(reason)
            if hasattr(w, "cancel"):
                w.cancel()
            if hasattr(w, "requestInterruption"):
                try:
                    w.requestInterruption()
                except Exception:
                    pass
            if hasattr(w, "quit"):
                try:
                    w.quit()
                except Exception:
                    pass
        except Exception as e:
            self.append_log(f"[WARN] TaskManager.cancel_worker: {e}")

    def quiesce(self, *, reason: str = "quiesce", timeout_ms: int = 5000) -> None:
        try:
            self.append_log(f"[Thread] Quiescendo background por '{reason}'…")
            with self._lock:
                workers = list(self._workers)

            for w in workers:
                self.cancel_worker(w, reason=reason)

            # Aguarda de forma cooperativa
            deadline = QTimer()
            for w in workers:
                try:
                    if hasattr(w, "wait"):
                        w.wait(timeout_ms)
                except Exception as e:
                    self.append_log(f"[WARN] TaskManager.wait: {e}")

            with self._lock:
                still = [x for x in self._workers if getattr(x, "isRunning", lambda: False)()]
            for w in still:
                try:
                    self.append_log("[Thread] Forçando término de worker remanescente…")
                    if hasattr(w, "terminate"):
                        w.terminate()
                except Exception as e:
                    self.append_log(f"[WARN] TaskManager.terminate: {e}")

            self.global_progress.setVisible(False)
            if self._status_spinner:
                try:
                    self._status_spinner.stop("")
                except Exception as e:
                    self.append_log(f"[WARN] TaskManager.reset spinner: {e}")
        except Exception as e:
            self.append_log(f"[WARN] TaskManager.quiesce: {e}")