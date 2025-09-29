# app/ui/components/machine_avatar.py
# -*- coding: utf-8 -*-
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QWidget, QLabel, QSizePolicy, QVBoxLayout

from app.ui.components.anim_utils import AvatarHoverAnimator

logger = logging.getLogger("[MachineAvatarExt]")
if not hasattr(logger, "warn"):
    logger.warn = logger.warning


class MachineIconProvider:
    """
    Provider com cache para imagens de role/status, com escala.
    Regras de arquivo:
      - attacker_01.png: online
      - attacker_02.png: offline
      - sensor_01.png:   online
      - sensor_02.png:   offline
      - general_01.png:  online
      - general_02.png:  offline
    """
    def __init__(self):
        self._pix_cache: dict[str, QPixmap] = {}
        self._scaled_cache: dict[tuple[str, int], QPixmap] = {}

    def resource_dir(self) -> Path:
        try:
            # .../app/ui/components/machine_avatar.py -> .../app/ui/resources
            return Path(__file__).resolve().parent.parent / "resources"
        except Exception as e:
            logger.error(f"[Icons] resource_dir: {e}")
            return Path("ui/resources")

    def _pick_filename(self, role: str, status: str) -> str:
        try:
            r = (role or "general").lower().strip()
            s = "online" if status == "online" else "offline"
            if r == "attacker":
                return "attacker_01.png" if s == "online" else "attacker_02.png"
            if r == "sensor":
                return "sensor_01.png" if s == "online" else "sensor_02.png"
            return "general_01.png" if s == "online" else "general_02.png"
        except Exception as e:
            logger.error(f"[Icons] _pick_filename falhou: {e}")
            return "general_02.png"

    def _load_pixmap(self, name: str) -> QPixmap:
        try:
            if name in self._pix_cache and not self._pix_cache[name].isNull():
                return self._pix_cache[name]
            path = self.resource_dir() / name
            pm = QPixmap(str(path))
            if pm.isNull():
                logger.error(f"[Icons] imagem não encontrada/ inválida: {path}")
            self._pix_cache[name] = pm
            return pm
        except Exception as e:
            logger.error(f"[Icons] _load_pixmap: {e}")
            return QPixmap()

    def get_icon(self, role: str, status: str, size_px: int) -> QPixmap:
        """
        Retorna QPixmap escalado e cacheado para (role,status,size_px)
        """
        try:
            name = self._pick_filename(role, status)
            key = (name, int(size_px))
            if key in self._scaled_cache and not self._scaled_cache[key].isNull():
                return self._scaled_cache[key]

            base = self._load_pixmap(name)
            if base.isNull():
                return QPixmap()
            scaled = base.scaled(
                int(size_px),
                int(size_px),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            self._scaled_cache[key] = scaled
            return scaled
        except Exception as e:
            logger.error(f"[Icons] get_icon: {e}")
            return QPixmap()


# Singleton para uso em toda a UI
ICONS = MachineIconProvider()


class MachineAvatarExt(QWidget):
    """
    Avatar baseado em imagens quadradas (ícone).
    Papel (role): 'attacker', 'sensor' ou 'general'
    Status: 'online' | 'offline'
    """
    DEFAULT_ICON_PX = 256
    MIN_ICON_PX = 96
    MAX_ICON_PX = 512

    def __init__(self, parent=None):
        super().__init__(parent)
        try:
            self.setObjectName("MachineAvatar")
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

            self._role = "general"
            self._status = "offline"
            self._icon_px = self.DEFAULT_ICON_PX

            self._img = QLabel(self)
            self._img.setObjectName("MachineAvatarImage")
            self._img.setAlignment(Qt.AlignCenter)
            self._img.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self._img.setFixedSize(self._icon_px, self._icon_px)

            lay = QVBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)
            lay.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
            lay.addWidget(self._img, 0, Qt.AlignHCenter)

            try:
                self._hover = AvatarHoverAnimator(
                    self._img,
                )
            except Exception as e:
                logger.error(f"[MachineAvatar] hover init: {e}")

            try:
                self._refresh_pixmap()
            except Exception as e:
                logger.error(f"[MachineAvatar] init refresh: {e}")
        except Exception as e:
            logger.error(f"[MachineAvatar] erro ao iniciar: {e}")

    # ---------- API ----------
    def setRole(self, role: str):
        try:
            new_role = (role or "").lower().strip()
            if new_role not in ("attacker", "sensor", "general"):
                new_role = "general"
            if new_role != self._role:
                self._role = new_role
                self._refresh_pixmap()
                logger.info(f"[MachineAvatar] role={self._role}")
        except Exception as e:
            logger.error(f"[MachineAvatar] setRole: {e}")

    def setStatus(self, status: str):
        try:
            new_st = "online" if status == "online" else "offline"
            if new_st != self._status:
                self._status = new_st
                self._refresh_pixmap()
                logger.info(f"[MachineAvatar] status={self._status}")
        except Exception as e:
            logger.error(f"[MachineAvatar] setStatus: {e}")

    def setIconSize(self, px: int):
        try:
            px = int(px)
            px = max(self.MIN_ICON_PX, min(self.MAX_ICON_PX, px))
            if px != self._icon_px:
                self._icon_px = px
                self._img.setFixedSize(self._icon_px, self._icon_px)
                self._refresh_pixmap()
                logger.info(f"[MachineAvatar] icon_px={self._icon_px}")
        except Exception as e:
            logger.error(f"[MachineAvatar] setIconSize: {e}")

    # ---------- Helpers ----------
    def sizeHint(self) -> QSize:
        return QSize(self._icon_px, self._icon_px)

    def minimumSizeHint(self) -> QSize:
        return QSize(self.MIN_ICON_PX, self.MIN_ICON_PX)

    def _refresh_pixmap(self):
        try:
            pm: QPixmap | None = ICONS.get_icon(self._role, self._status, self._icon_px)
            if pm and not pm.isNull():
                self._img.setPixmap(pm)
            self._img.setProperty("vis", self._status)
            self._img.repaint()
            self.repaint()
        except Exception as e:
            logger.error(f"[MachineAvatar] _refresh_pixmap: {e}")