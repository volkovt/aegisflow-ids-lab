# python
import sys
import os
from typing import Any, Optional

import pandas as pd

try:
    import pyarrow as pa  # opcional, mas recomendado para .parquet
    _HAS_PYARROW = True
except Exception:
    _HAS_PYARROW = False

from PySide6.QtCore import (
    Qt,
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    QRegularExpression, QThread, QObject, Signal,
)
from PySide6.QtGui import QAction, QKeySequence, QGuiApplication, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QFileDialog,
    QTableView,
    QToolBar,
    QLineEdit,
    QLabel,
    QWidget,
    QHBoxLayout,
    QVBoxLayout,
    QDockWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QMessageBox, QProgressBar,
)

class ParquetLoader(QObject):
    loaded = Signal(object, str)   # DataFrame, path
    error = Signal(str, str)       # mensagem, path
    finished = Signal()

    def __init__(self, path: str):
        super().__init__()
        self.path = path

    def run(self):
        try:
            if _HAS_PYARROW:
                df = pd.read_parquet(self.path, engine="pyarrow")
            else:
                df = pd.read_parquet(self.path)
            self.loaded.emit(df, self.path)
        except Exception as ex:
            self.error.emit(str(ex), self.path)
        finally:
            self.finished.emit()


class DataFrameModel(QAbstractTableModel):
    def __init__(self, df: Optional[pd.DataFrame] = None, parent=None):
        super().__init__(parent)
        self._df = df if df is not None else pd.DataFrame()

    def setDataFrame(self, df: pd.DataFrame):
        self.beginResetModel()
        self._df = df
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return 0 if self._df is None else int(self._df.shape[0])

    def columnCount(self, parent=QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return 0 if self._df is None else int(self._df.shape[1])

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid() or role not in (Qt.DisplayRole, Qt.EditRole):
            return None
        try:
            val = self._df.iat[index.row(), index.column()]
        except Exception:
            return None
        # Renderização segura
        if pd.isna(val):
            return ""
        return str(val)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> Any:
        if role != Qt.DisplayRole or self._df is None:
            return None
        if orientation == Qt.Horizontal:
            try:
                return str(self._df.columns[section])
            except Exception:
                return None
        else:
            return str(section)

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemIsEnabled
        return Qt.ItemIsSelectable | Qt.ItemIsEnabled


class AnyColumnFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._needle = ""

    def setNeedle(self, text: str):
        self._needle = text.strip().lower()
        # Usar regex vazia evita custo quando filtro está vazio
        if self._needle:
            self.setFilterRegularExpression(QRegularExpression(".+"))  # força checagem
        else:
            self.setFilterRegularExpression(QRegularExpression())
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not self._needle:
            return True
        cols = self.sourceModel().columnCount()
        sm = self.sourceModel()
        needle = self._needle
        for c in range(cols):
            idx = sm.index(source_row, c, source_parent)
            val = sm.data(idx, Qt.DisplayRole)
            if val and needle in str(val).lower():
                return True
        return False


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Leitor de Parquet • PySide6")
        self.resize(1200, 700)
        self.setAcceptDrops(True)

        # Modelo
        self._df_model = DataFrameModel()
        self._proxy = AnyColumnFilterProxy(self)
        self._proxy.setSourceModel(self._df_model)

        # Tabela
        self.table = QTableView(self)
        self.table.setModel(self._proxy)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectItems)
        self.table.setSelectionMode(QTableView.ExtendedSelection)
        self.table.horizontalHeader().setStretchLastSection(True)

        # Barra de ferramentas com filtro
        toolbar = QToolBar("Ações", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.act_open = QAction("Abrir", self)
        self.act_open.setShortcut(QKeySequence.Open)
        self.act_open.triggered.connect(self.open_dialog)
        toolbar.addAction(self.act_open)

        self.act_export = QAction("Exportar CSV", self)
        self.act_export.setShortcut("Ctrl+E")
        self.act_export.triggered.connect(self.export_csv)
        toolbar.addAction(self.act_export)

        self.act_copy = QAction("Copiar seleção", self)
        self.act_copy.setShortcut(QKeySequence.Copy)
        self.act_copy.triggered.connect(self.copy_selection)
        toolbar.addAction(self.act_copy)

        toolbar.addSeparator()
        toolbar.addWidget(QLabel("Filtro: ", self))
        self.ed_filter = QLineEdit(self)
        self.ed_filter.setPlaceholderText("Digite para filtrar em qualquer coluna…")
        self.ed_filter.textChanged.connect(self._proxy.setNeedle)
        toolbar.addWidget(self.ed_filter)

        toolbar.addSeparator()
        self.act_theme = QAction("Tema escuro", self)
        self.act_theme.setCheckable(True)
        self.act_theme.toggled.connect(self.toggle_theme)
        toolbar.addAction(self.act_theme)

        # Dock: Esquema
        self.schema_dock = QDockWidget("Esquema", self)
        self.schema_tree = QTreeWidget(self.schema_dock)
        self.schema_tree.setHeaderLabels(["Coluna", "Tipo", "Nulos %"])
        self.schema_tree.setUniformRowHeights(True)
        self.schema_dock.setWidget(self.schema_tree)
        self.addDockWidget(Qt.RightDockWidgetArea, self.schema_dock)

        # Central
        central = QWidget(self)
        lay = QVBoxLayout(central)
        lay.addWidget(self.table)
        self.setCentralWidget(central)

        # Status bar
        self.status = self.statusBar()
        self._current_path = None
        self._apply_light()

        # \[NOVO] estado de carregamento + barra de progresso
        self._loading = False
        self._thread = None
        self._worker = None
        self._progress = QProgressBar(self)
        self._progress.setRange(0, 0)  # indeterminado
        self._progress.setVisible(False)
        self.status.addPermanentWidget(self._progress)

    # ---------- UI helpers ----------
    def toggle_theme(self, enabled: bool):
        if enabled:
            self._apply_dark()
        else:
            self._apply_light()

    def _apply_dark(self):
        app = QApplication.instance()
        app.setStyle("Fusion")
        palette = QPalette()
        palette.setColor(QPalette.Window, Qt.black)
        palette.setColor(QPalette.WindowText, Qt.white)
        palette.setColor(QPalette.Base, Qt.black)
        palette.setColor(QPalette.AlternateBase, Qt.black)
        palette.setColor(QPalette.ToolTipBase, Qt.white)
        palette.setColor(QPalette.ToolTipText, Qt.white)
        palette.setColor(QPalette.Text, Qt.white)
        palette.setColor(QPalette.Button, Qt.black)
        palette.setColor(QPalette.ButtonText, Qt.white)
        palette.setColor(QPalette.Highlight, Qt.darkGray)
        palette.setColor(QPalette.HighlightedText, Qt.white)
        app.setPalette(palette)

    def _apply_light(self):
        app = QApplication.instance()
        app.setStyle("Fusion")
        app.setPalette(QPalette())

    # ---------- File ops ----------
    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Abrir arquivo .parquet",
            "",
            "Arquivos Parquet (*.parquet);;Todos os arquivos (*.*)",
        )
        if path:
            self.load_file(path)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            for url in e.mimeData().urls():
                if url.isLocalFile() and url.toLocalFile().lower().endswith(".parquet"):
                    e.acceptProposedAction()
                    return
        e.ignore()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".parquet"):
                self.load_file(url.toLocalFile())
                break

    def load_file(self, path: str):
        if not path or self._loading:
            return
        self._set_loading(True, path)

        self._thread = QThread(self)
        self._worker = ParquetLoader(path)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.loaded.connect(self._on_worker_loaded)
        self._worker.error.connect(self._on_worker_error)

        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(lambda: self._set_loading(False))

        self._thread.start()

    def _set_loading(self, on: bool, path: Optional[str] = None):
        self._loading = on
        self.table.setEnabled(not on)
        self.act_open.setEnabled(not on)
        self.act_export.setEnabled(not on)
        self.act_copy.setEnabled(not on)
        self._progress.setVisible(on)

        if on:
            name = os.path.basename(path) if path else ""
            self._progress.setFormat(f"Carregando {name}…")
            self.status.showMessage(f"Lendo {name}…")
        else:
            self._progress.setFormat("")

    def _on_worker_loaded(self, df: pd.DataFrame, path: str):
        self._df_model.setDataFrame(df)
        self._current_path = path
        self._populate_schema(df)
        self._update_status(df, path)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

    # \[NOVO] callback de erro
    def _on_worker_error(self, msg: str, path: str):
        QMessageBox.critical(self, "Erro ao ler", f"Falha ao ler '{os.path.basename(path)}':\n{msg}")

    def _read_parquet(self, path: str) -> pd.DataFrame:
        # Tenta usar pyarrow quando disponível
        if _HAS_PYARROW:
            return pd.read_parquet(path, engine="pyarrow")
        # Fallback
        return pd.read_parquet(path)

    # ---------- Schema & status ----------
    def _populate_schema(self, df: pd.DataFrame):
        self.schema_tree.clear()
        if df is None or df.empty:
            return

        # Cálculo de nulos com salvaguarda de custo
        max_cells_for_nulls = 1_000_000
        do_nulls = (df.shape[0] * df.shape[1]) <= max_cells_for_nulls

        for col in df.columns:
            dtype = str(df[col].dtype)
            nulls_pct = ""
            if do_nulls:
                try:
                    pct = float(df[col].isna().mean()) * 100.0
                    nulls_pct = f"{pct:.1f}%"
                except Exception:
                    nulls_pct = ""
            item = QTreeWidgetItem([str(col), dtype, nulls_pct])
            self.schema_tree.addTopLevelItem(item)
        self.schema_tree.resizeColumnToContents(0)

    def _update_status(self, df: pd.DataFrame, path: Optional[str]):
        rows, cols = df.shape
        mem = 0
        try:
            mem = int(df.memory_usage(deep=True).sum())
        except Exception:
            pass

        def fmt_bytes(n: int) -> str:
            units = ["B", "KB", "MB", "GB", "TB"]
            s = 0
            f = float(n)
            while f >= 1024.0 and s < len(units) - 1:
                f /= 1024.0
                s += 1
            return f"{f:.1f} {units[s]}"

        name = os.path.basename(path) if path else "(sem arquivo)"
        self.status.showMessage(f"{name}  •  {rows} linhas × {cols} colunas  •  ~{fmt_bytes(mem)}")

    # ---------- Actions ----------
    def export_csv(self):
        if self._df_model.rowCount() == 0:
            QMessageBox.information(self, "Exportar CSV", "Nada para exportar.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Salvar como CSV",
            "",
            "CSV (*.csv);;Todos os arquivos (*.*)",
        )
        if not path:
            return

        # Exporta respeitando o filtro atual (linhas visíveis)
        df = self._export_visible_dataframe()
        try:
            df.to_csv(path, index=False)
            QMessageBox.information(self, "Exportar CSV", "Exportação concluída.")
        except Exception as ex:
            QMessageBox.critical(self, "Erro ao exportar", f"Falha ao salvar CSV:\n{ex}")

    def _export_visible_dataframe(self) -> pd.DataFrame:
        src = self._df_model
        proxy = self._proxy
        rows = proxy.rowCount()
        cols = proxy.columnCount()
        if rows == 0 or cols == 0:
            return pd.DataFrame()

        # Reconstrói DataFrame visível pela ordem atual
        data = {}
        headers = [src._df.columns[proxy.headerData(c, Qt.Horizontal, Qt.DisplayRole) or c] for c in range(cols)]
        # Mapear nomes corretos
        headers = list(src._df.columns)
        for c in range(cols):
            col_name = src._df.columns[c]
            col_vals = []
            for r in range(rows):
                idx = proxy.index(r, c)
                src_idx = proxy.mapToSource(idx)
                val = src._df.iat[src_idx.row(), src_idx.column()]
                col_vals.append(val)
            data[col_name] = col_vals
        return pd.DataFrame(data)

    def copy_selection(self):
        sel = self.table.selectionModel()
        if not sel or not sel.hasSelection():
            return
        indexes = sel.selectedIndexes()
        if not indexes:
            return
        # Organiza por linhas/colunas
        indexes.sort(key=lambda i: (i.row(), i.column()))
        rows = {}
        for idx in indexes:
            r = idx.row()
            rows.setdefault(r, []).append(idx)
        # Monta TSV
        lines = []
        for r in sorted(rows.keys()):
            cols = rows[r]
            cols.sort(key=lambda i: i.column())
            parts = []
            for idx in cols:
                val = self._proxy.data(idx, Qt.DisplayRole)
                # Escapa TAB e quebras de linha
                s = "" if val is None else str(val)
                s = s.replace("\t", "    ").replace("\r", " ").replace("\n", " ")
                parts.append(s)
            lines.append("\t".join(parts))
        text = "\n".join(lines)
        QGuiApplication.clipboard().setText(text)

    # ---------- App start ----------
    def load_from_cli(self, argv):
        if len(argv) >= 2:
            path = argv[1]
            if os.path.isfile(path) and path.lower().endswith(".parquet"):
                self.load_file(path)


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.load_from_cli(sys.argv)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()