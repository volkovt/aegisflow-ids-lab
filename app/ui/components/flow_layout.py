# --- Pills futuristas e FlowLayout para quebrar linha automaticamente ---
import logging
from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QFontMetrics, QGuiApplication, QIcon
from PySide6.QtWidgets import QPushButton, QLayout, QWidget, QToolTip

_ui_logger = logging.getLogger("[FlowLayout]")

class FlowLayout(QLayout):
    def __init__(self, parent=None, margin=0, hspacing=6, vspacing=6, alignment=Qt.AlignLeft):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(margin, margin, margin, margin)
        self._hspace = hspacing
        self._vspace = vspacing
        self._alignment = alignment

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return QSize(200, self.heightForWidth(200))

    def _do_layout(self, rect, test_only):
        x = rect.x()
        y = rect.y()
        line_height = 0
        line_items = []
        line_width = 0

        for item in self._items:
            w = item.widget()
            if not w.isVisible():
                continue
            space_x = self._hspace
            space_y = self._vspace
            item_width = item.sizeHint().width()
            next_x = x + item_width + space_x
            if next_x - space_x > rect.right() and line_height > 0:
                if self._alignment == Qt.AlignHCenter and line_items:
                    offset = (rect.width() - line_width + space_x) // 2
                    for i, it in enumerate(line_items):
                        if not test_only:
                            it.setGeometry(QRect(QPoint(rect.x() + offset, y), it.sizeHint()))
                        offset += it.sizeHint().width() + space_x
                line_items = []
                line_width = 0
                x = rect.x()
                y = y + line_height + space_y
                next_x = x + item_width + space_x
                line_height = 0
            line_items.append(item)
            line_width += item_width + space_x
            if not test_only and self._alignment != Qt.AlignHCenter:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))
            x = next_x
            line_height = max(line_height, item.sizeHint().height())
        # Última linha
        if self._alignment == Qt.AlignHCenter and line_items:
            offset = (rect.width() - line_width + space_x) // 2
            for i, it in enumerate(line_items):
                if not test_only:
                    it.setGeometry(QRect(QPoint(rect.x() + offset, y), it.sizeHint()))
                offset += it.sizeHint().width() + space_x
        result_height = y + line_height - rect.y()
        return result_height
