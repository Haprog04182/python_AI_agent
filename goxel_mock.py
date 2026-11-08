#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Лёгкий «мок»/адаптер для подключения к редактору Goxel и генерации
3D-моделей через его консольный интерфейс.

Goxel работает как обычное приложение с GUI, но умеет в пакетном режиме
читать текстовый формат вокселей (``X Y Z RRGGBB``) и экспортировать
результат в один из поддерживаемых форматов (obj, ply, vox, qb, txt).
Этот модуль инкапсулирует это взаимодействие и позволяет тестировать
код без реального Goxel (режим mock=True).
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

Voxel = Tuple[int, int, int, str]

SUPPORTED_FORMATS = {"obj", "ply", "vox", "qb", "txt"}

NAMED_COLORS: Dict[str, str] = {
    "white": "FFFFFF",
    "black": "000000",
    "red": "FF0000",
    "green": "00FF00",
    "blue": "0000FF",
    "yellow": "FFFF00",
    "cyan": "00FFFF",
    "magenta": "FF00FF",
    "orange": "FF8800",
    "purple": "8800FF",
    "pink": "FFC0CB",
    "gray": "808080",
    "grey": "808080",
    "brown": "8B4513",
}

AVAILABLE_SHAPES = [
    "cube",
    "sphere",
    "pyramid",
    "cylinder",
    "cone",
    "torus",
    "diamond",
    "heart",
    "cross",
    "spiral",
    "random",
]


class GoxelMockError(RuntimeError):
    """Ошибка взаимодействия с Goxel."""


def normalize_color(color: Any) -> str:
    """Приводит цвет к виду ``RRGGBB``."""
    if color is None:
        return "FFFFFF"
    text = str(color).strip()
    if not text:
        return "FFFFFF"
    if text.lower() in NAMED_COLORS:
        text = NAMED_COLORS[text.lower()]
    text = text.lstrip("#")
    if "," in text:
        try:
            r, g, b = [int(x.strip()) for x in text.split(",")]
            return f"{r:02X}{g:02X}{b:02X}"
        except ValueError:
            pass
    if len(text) == 3 and all(c in "0123456789abcdefABCDEF" for c in text):
        text = "".join(c * 2 for c in text)
    if len(text) == 6 and all(c in "0123456789abcdefABCDEF" for c in text):
        return text.upper()
    return "FFFFFF"


def _coords_for_size(size: int) -> List[int]:
    size = max(1, int(size))
    return [i - size // 2 for i in range(size)]


class GoxelMock:
    """Адаптер/мок для Goxel.

    В обычном режиме (``mock=False``) при наличии бинарника ``goxel``
    использует реальный экспорт.  В режиме ``mock=True`` ничего внешнего
    не запускает и создаёт заглушечные файлы — удобно для тестов.
    """

    def __init__(
        self,
        binary: str = "goxel",
        timeout: int = 30,
        mock: bool = False,
        output_dir: Optional[str] = None,
    ) -> None:
        self.binary = binary
        self.timeout = timeout
        self.mock = bool(mock)
        self.output_dir = Path(output_dir or "generated_models")
        self.connected = False
        self._fallback_to_mock = False
        self.last_command: Optional[List[str]] = None
        self.call_history: List[Dict[str, Any]] = []

    # ----------------------------- Подключение -----------------------------

    def connect(self) -> bool:
        """Проверяет доступность Goxel и «подключается» к нему."""
        if self.mock:
            self.connected = True
            self._record("connect", {"mode": "mock"})
            return True

        path = shutil.which(self.binary)
        if path is None:
            # Не нашли бинарник: переходим в мок-режим, чтобы не падать.
            self.mock = True
            self._fallback_to_mock = True
            self.connected = True
            self._record("connect", {"mode": "fallback-mock"})
            return True

        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
            self.connected = (result.returncode == 0)
        except Exception:  # noqa: BLE001
            self.connected = False

        if not self.connected:
            self.mock = True
            self._fallback_to_mock = True
            self.connected = True
            self._record("connect", {"mode": "fallback-mock"})
            return True

        self._record("connect", {"mode": "real", "binary": path})
        return True

    def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    def __enter__(self) -> "GoxelMock":
        self.connect()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.disconnect()

    # ----------------------------- Генерация вокселей -----------------------------

    def _materialize_shape(
        self,
        shape: str,
        size: int,
        color: str,
        hollow: bool = False,
        seed: int = 42,
        **params: Any,
    ) -> List[Voxel]:
        shape = (shape or "cube").strip().lower()
        color = normalize_color(color)
        size = max(1, min(64, int(size)))
        coords = _coords_for_size(size)
        rng = random.Random(seed)
        voxels: List[Voxel] = []

        if shape == "cube":
            mn, mx = coords[0], coords[-1]
            for x in coords:
                for y in coords:
                    for z in coords:
                        if hollow and not (x in (mn, mx) or y in (mn, mx) or z in (mn, mx)):
                            continue
                        voxels.append((x, y, z, color))

        elif shape == "sphere":
            r = size / 2.0
            r2 = r * r
            for x in coords:
                for y in coords:
                    for z in coords:
                        d2 = x * x + y * y + z * z
                        if d2 <= r2:
                            if hollow and d2 < (r - 1) ** 2:
                                continue
                            voxels.append((x, y, z, color))

        elif shape == "pyramid":
            height = size
            for level in range(height):
                half = (height - level) // 2
                xs = range(-half, half + 1)
                zs = range(-half, half + 1)
                y = level - height // 2
                for x in xs:
                    for z in zs:
                        voxels.append((x, y, z, color))

        elif shape == "cylinder":
            radius = size / 2.0
            radius2 = radius * radius
            height = size
            for y in range(-height // 2, height // 2 + 1):
                for x in coords:
                    for z in coords:
                        if x * x + z * z <= radius2:
                            voxels.append((x, y, z, color))

        elif shape == "cone":
            height = size
            for level in range(height):
                r = max(1, (height - level))
                r2 = (r / 2.0) ** 2
                y = level - height // 2
                for x in coords:
                    for z in coords:
                        if x * x + z * z <= r2:
                            voxels.append((x, y, z, color))

        elif shape == "torus":
            major = max(1, size // 3)
            minor = max(1, size // 6)
            for x in coords:
                for y in coords:
                    for z in coords:
                        q = (x * x + z * z) ** 0.5 - major
                        if q * q + y * y <= minor * minor:
                            voxels.append((x, y, z, color))

        elif shape == "diamond":
            r = size // 2 + 1
            for x in coords:
                for y in coords:
                    for z in coords:
                        if abs(x) + abs(y) + abs(z) <= r:
                            voxels.append((x, y, z, color))

        elif shape == "heart":
            scale = max(1, size // 4)
            for x in coords:
                for y in coords:
                    for z in coords:
                        xx, yy, zz = x / scale, y / scale, z / scale
                        if (xx * xx + (yy - (xx * xx) ** (1.0 / 3.0)) ** 2 + zz * zz) <= 4:
                            voxels.append((x, y, z, color))

        elif shape == "cross":
            arm = size // 3 or 1
            for x in coords:
                for y in coords:
                    for z in coords:
                        if abs(x) <= arm or abs(y) <= arm or abs(z) <= arm:
                            if (abs(x) <= arm) + (abs(y) <= arm) + (abs(z) <= arm) >= 2:
                                voxels.append((x, y, z, color))

        elif shape == "spiral":
            steps = size * size
            for i in range(steps):
                angle = i * 0.35
                radius = 1 + i * 0.08
                x = int(round(radius * __import__("math").cos(angle)))
                z = int(round(radius * __import__("math").sin(angle)))
                y = i // (size + 1) - size // 2
                voxels.append((x, y, z, color))

        elif shape == "random":
            count = max(1, (size ** 3) // 5)
            pool = [(x, y, z) for x in coords for y in coords for z in coords]
            rng.shuffle(pool)
            for x, y, z in pool[:count]:
                voxels.append((x, y, z, rng.choice(
                    ["FF0000", "00FF00", "0000FF", "FFFF00", "FF00FF", "00FFFF"]
                )))

        else:
            raise GoxelMockError(f"Неизвестная форма: {shape!r}. Доступные: {AVAILABLE_SHAPES}")

        # Защита от дубликатов (на всякий случай).
        unique: Dict[Tuple[int, int, int], str] = {}
        for x, y, z, c in voxels:
            unique[(x, y, z)] = c
        return [(x, y, z, c) for (x, y, z), c in unique.items()]

    def _voxel_text(self, voxels: Sequence[Voxel]) -> str:
        return "".join(f"{x} {y} {z} {c}\n" for x, y, z, c in voxels)

    # ----------------------------- Основной API -----------------------------

    def generate_model(
        self,
        shape: str = "cube",
        size: int = 8,
        color: str = "FFFFFF",
        output_path: Optional[str] = None,
        output_format: Optional[str] = None,
        hollow: bool = False,
        seed: int = 42,
        **shape_params: Any,
    ) -> Dict[str, Any]:
        """Генерирует 3D-модель и возвращает метаданные."""
        if not self.connected:
            self.connect()

        fmt = (output_format or "").strip().lower().lstrip(".")
        path = Path(output_path) if output_path else None
        if path is not None and not fmt:
            fmt = path.suffix.lower().lstrip(".")
        if not fmt:
            fmt = "obj"
        if fmt not in SUPPORTED_FORMATS:
            raise GoxelMockError(
                f"Формат {fmt!r} не поддерживается. Доступные: {sorted(SUPPORTED_FORMATS)}"
            )

        voxels = self._materialize_shape(
            shape=shape, size=size, color=color, hollow=hollow, seed=seed, **shape_params
        )
        if not voxels:
            raise GoxelMockError("Генерация дала пустую модель (0 вокселей)")

        if path is None:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self.output_dir / f"{shape}_{size}_{stamp}.{fmt}"
        else:
            path = Path(path)
            if path.suffix == "":
                path = path.with_suffix(f".{fmt}")
            elif path.suffix.lower().lstrip(".") != fmt:
                # Явный format имеет приоритет над расширением.
                path = path.with_suffix(f".{fmt}")
            path.parent.mkdir(parents=True, exist_ok=True)

        voxel_text = self._voxel_text(voxels)

        # Мок-режим: создаём файл-заглушку без вызова Goxel.
        if self.mock:
            if fmt == "txt":
                path.write_text(voxel_text, encoding="utf-8")
            else:
                path.write_text(
                    f"# MOCK GOXEL OUTPUT\n"
                    f"# shape={shape} size={size} voxels={len(voxels)} format={fmt}\n"
                    f"# (реальный Goxel недоступен, сохранён исходный воксельный текст)\n"
                    + voxel_text,
                    encoding="utf-8",
                )
            result = {
                "success": True,
                "mode": "mock",
                "shape": shape,
                "size": size,
                "format": fmt,
                "path": str(path.resolve()),
                "voxel_count": len(voxels),
            }
            self.call_history.append({"action": "generate_model", **result})
            self._record("generate_model", result)
            return result

        # Реальный режим: временный txt -> Goxel -> экспорт.
        fd, tmp_name = tempfile.mkstemp(prefix="goxel_", suffix=".txt")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(voxel_text)

            binary = shutil.which(self.binary) or self.binary
            command = [binary, str(tmp_path), "--export", str(path)]
            self.last_command = command
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
            if completed.returncode != 0:
                stderr = (completed.stderr or completed.stdout or "").strip()
                raise GoxelMockError(
                    f"Goxel завершился с кодом {completed.returncode}: {stderr}"
                )
            if not path.exists() or path.stat().st_size == 0:
                raise GoxelMockError("Goxel не создал выходной файл")
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

        result = {
            "success": True,
            "mode": "real",
            "shape": shape,
            "size": size,
            "format": fmt,
            "path": str(path.resolve()),
            "voxel_count": len(voxels),
        }
        # Сохраняем исходный воксельный текст рядом — бывает полезно для отладки.
        try:
            path.with_suffix(".txt").write_text(voxel_text, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        self.call_history.append({"action": "generate_model", **result})
        self._record("generate_model", result)
        return result

    def generate_from_voxels(
        self,
        voxels: Iterable[Voxel],
        output_path: Optional[str] = None,
        output_format: str = "obj",
        color_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Экспортирует готовый список вокселей через Goxel."""
        voxel_list = list(voxels)
        if color_override is not None:
            voxel_list = [(x, y, z, normalize_color(color_override)) for x, y, z, _ in voxel_list]
        if not voxel_list:
            raise GoxelMockError("Список вокселей пуст")
        shape = "custom"
        # Для совместимости используем generate_model с предварительно заданными данными:
        # создаём временный «кусок» через прямой экспорт.
        if not self.connected:
            self.connect()

        fmt = output_format.strip().lower().lstrip(".")
        if fmt not in SUPPORTED_FORMATS:
            raise GoxelMockError(f"Формат {fmt!r} не поддерживается")

        path = Path(output_path) if output_path else None
        if path is None:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self.output_dir / f"custom_{stamp}.{fmt}"
        else:
            path = Path(path)
            if path.suffix == "" or path.suffix.lower().lstrip(".") != fmt:
                path = path.with_suffix(f".{fmt}")
            path.parent.mkdir(parents=True, exist_ok=True)

        voxel_text = self._voxel_text(voxel_list)

        if self.mock:
            path.write_text(
                "# MOCK GOXEL OUTPUT\n" + voxel_text if fmt != "txt" else voxel_text,
                encoding="utf-8",
            )
            result = {
                "success": True,
                "mode": "mock",
                "shape": "custom",
                "format": fmt,
                "path": str(path.resolve()),
                "voxel_count": len(voxel_list),
            }
            self.call_history.append({"action": "generate_from_voxels", **result})
            return result

        fd, tmp_name = tempfile.mkstemp(prefix="goxel_", suffix=".txt")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(voxel_text)
            binary = shutil.which(self.binary) or self.binary
            command = [binary, str(tmp_path), "--export", str(path)]
            self.last_command = command
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
            if completed.returncode != 0:
                stderr = (completed.stderr or completed.stdout or "").strip()
                raise GoxelMockError(
                    f"Goxel завершился с кодом {completed.returncode}: {stderr}"
                )
            if not path.exists() or path.stat().st_size == 0:
                raise GoxelMockError("Goxel не создал выходной файл")
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

        result = {
            "success": True,
            "mode": "real",
            "shape": "custom",
            "format": fmt,
            "path": str(path.resolve()),
            "voxel_count": len(voxel_list),
        }
        try:
            path.with_suffix(".txt").write_text(voxel_text, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        self.call_history.append({"action": "generate_from_voxels", **result})
        return result

    def generate_scene(
        self,
        shapes: Sequence[Dict[str, Any]],
        output_path: Optional[str] = None,
        output_format: str = "obj",
    ) -> Dict[str, Any]:
        """Собирает сцену из нескольких фигур и экспортирует её."""
        all_voxels: List[Voxel] = []
        offset_x = 0
        for spec in shapes:
            spec = dict(spec or {})
            shape = spec.pop("shape", "cube")
            size = spec.pop("size", 6)
            color = spec.pop("color", "FFFFFF")
            dx = int(spec.pop("offset_x", offset_x))
            dy = int(spec.pop("offset_y", 0))
            dz = int(spec.pop("offset_z", 0))
            hollow = bool(spec.pop("hollow", False))
            seed = int(spec.pop("seed", 42))
            voxels = self._materialize_shape(shape, size, color, hollow, seed, **spec)
            for x, y, z, c in voxels:
                all_voxels.append((x + dx, y + dy, z + dz, c))
            offset_x += size + 2
        return self.generate_from_voxels(
            all_voxels, output_path=output_path, output_format=output_format
        )

    @staticmethod
    def available_shapes() -> List[str]:
        return list(AVAILABLE_SHAPES)

    @staticmethod
    def supported_formats() -> List[str]:
        return sorted(SUPPORTED_FORMATS)

    def _record(self, action: str, payload: Dict[str, Any]) -> None:
        self.call_history.append({"action": action, "timestamp": datetime.now().isoformat(), **payload})


if __name__ == "__main__":
    # Небольшая демонстрация.
    with GoxelMock() as goxel:
        for sh in ["cube", "sphere", "pyramid", "torus"]:
            print(goxel.generate_model(shape=sh, size=8, color="cyan"))
