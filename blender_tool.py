#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Адаптер для взаимодействия с Blender (background mode) и генерации 3D-моделей.

Blender вызывается в безголовом режиме (``--background``) с временным
Python-скриптом, который строит сцену из примитивов и экспортирует её в
один из поддерживаемых форматов. Адаптер повторяет основной контракт
GoxelMock: ``connect()``, ``generate_model()``, ``generate_scene()``.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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

SUPPORTED_FORMATS = {"obj", "stl", "ply", "gltf", "glb", "fbx", "blend"}

AVAILABLE_SHAPES = [
    "cube",
    "sphere",
    "uv_sphere",
    "ico_sphere",
    "cylinder",
    "cone",
    "torus",
    "plane",
    "monkey",
    "grid",
    "circle",
    "text",
]

_DEFAULT_OUTPUT_DIR = "generated_models"
_DEFAULT_TIMEOUT = int(os.environ.get("BLENDER_TIMEOUT", "180"))


class BlenderToolError(RuntimeError):
    """Ошибка взаимодействия с Blender."""


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


# Скрипт, выполняемый внутри Blender. Маркер PARAMS_PLACEHOLDER заменяется
# на JSON-представление параметров (через repr, поэтому безопасно).
_BLENDER_SCRIPT_TEMPLATE = r'''
import bpy
import json
import os
import math

params = json.loads(PARAMS_PLACEHOLDER)


def hex_to_rgba(h):
    h = (h or "FFFFFF").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        h = "FFFFFF"
    try:
        return (
            int(h[0:2], 16) / 255.0,
            int(h[2:4], 16) / 255.0,
            int(h[4:6], 16) / 255.0,
            1.0,
        )
    except ValueError:
        return (1.0, 1.0, 1.0, 1.0)


def deg2rad(v):
    return float(v or 0.0) * math.pi / 180.0


bpy.ops.wm.read_factory_settings(use_empty=True)


def new_material(color_hex, name="Material"):
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = hex_to_rgba(color_hex)
    return mat


def make_shape(shape, size, location):
    shape = (shape or "cube").lower().strip()
    loc = location if (isinstance(location, (list, tuple)) and len(location) == 3) else (0, 0, 0)
    loc = (float(loc[0]), float(loc[1]), float(loc[2]))
    size = float(size)
    if shape == "cube":
        bpy.ops.mesh.primitive_cube_add(size=size, location=loc)
    elif shape in ("sphere", "uv_sphere"):
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=size / 2.0, segments=32, ring_count=16, location=loc
        )
    elif shape == "ico_sphere":
        bpy.ops.mesh.primitive_ico_sphere_add(radius=size / 2.0, location=loc)
    elif shape == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(
            radius=size / 2.0, depth=size, location=loc
        )
    elif shape == "cone":
        bpy.ops.mesh.primitive_cone_add(
            radius1=size / 2.0, depth=size, location=loc
        )
    elif shape == "torus":
        bpy.ops.mesh.primitive_torus_add(
            major_radius=size / 2.0,
            minor_radius=max(size / 16.0, 0.05),
            location=loc,
        )
    elif shape == "plane":
        bpy.ops.mesh.primitive_plane_add(size=size, location=loc)
    elif shape == "monkey":
        bpy.ops.mesh.primitive_monkey_add(size=size, location=loc)
    elif shape == "grid":
        bpy.ops.mesh.primitive_grid_add(size=size, location=loc)
    elif shape == "circle":
        bpy.ops.mesh.primitive_circle_add(radius=size / 2.0, location=loc)
    elif shape == "text":
        bpy.ops.object.text_add(location=loc)
        txt = bpy.context.active_object
        txt.data.body = str(params.get("text", "Blender"))
    else:
        bpy.ops.mesh.primitive_cube_add(size=size, location=loc)
    return bpy.context.active_object


def apply_hollow(obj, size):
    """Делает объект полым через булеву разность с уменьшенной копией."""
    try:
        inner = obj.copy()
        inner.data = obj.data.copy()
        bpy.context.collection.objects.link(inner)
        factor = 0.8
        inner.scale = (inner.scale[0] * factor, inner.scale[1] * factor, inner.scale[2] * factor)
        bpy.context.view_layer.update()
        mod = obj.modifiers.new(name="Boolean", type="BOOLEAN")
        mod.operation = "DIFFERENCE"
        mod.object = inner
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.modifier_apply(modifier=mod.name)
        bpy.data.objects.remove(inner, do_unlink=True)
    except Exception:
        try:
            for o in list(bpy.context.scene.objects):
                if o.name.startswith("inner"):
                    bpy.data.objects.remove(o, do_unlink=True)
        except Exception:
            pass


created = []
for idx, spec in enumerate(params.get("objects", [])):
    spec = dict(spec or {})
    obj = make_shape(
        spec.get("shape", "cube"),
        spec.get("size", 2.0),
        spec.get("location", (0, 0, 0)),
    )
    if obj is None:
        continue
    name = spec.get("name")
    obj.name = str(name) if name else f"Object_{idx}"

    sc = spec.get("scale", 1.0)
    if isinstance(sc, (list, tuple)):
        obj.scale = tuple(float(x) for x in sc)
    else:
        s = float(sc)
        if s != 1.0:
            obj.scale = (obj.scale[0] * s, obj.scale[1] * s, obj.scale[2] * s)

    rot = spec.get("rotation_deg", spec.get("rotation"))
    if isinstance(rot, (list, tuple)) and len(rot) == 3:
        obj.rotation_euler = (
            deg2rad(rot[0]),
            deg2rad(rot[1]),
            deg2rad(rot[2]),
        )

    if bool(spec.get("hollow", False)):
        apply_hollow(obj, spec.get("size", 2.0))

    color = spec.get("color", params.get("color", "FFFFFF"))
    mat = new_material(color, f"Mat_{idx}")
    if getattr(obj, "data", None) is not None and hasattr(obj.data, "materials"):
        obj.data.materials.append(mat)

    created.append(obj)

fmt = (params.get("output_format") or "obj").lower().strip().lstrip(".")
path = params["output_path"]
parent = os.path.dirname(path)
if parent:
    os.makedirs(parent, exist_ok=True)

# Защита: если расширение не указано — подставляем.
if not os.path.splitext(path)[1]:
    path = path + "." + fmt
    params["output_path"] = path

if fmt in ("obj",):
    bpy.ops.export_scene.obj(filepath=path, use_selection=False)
elif fmt == "stl":
    bpy.ops.export_mesh.stl(filepath=path)
elif fmt == "ply":
    bpy.ops.export_mesh.ply(filepath=path)
elif fmt == "gltf":
    bpy.ops.export_scene.gltf(filepath=path, export_format="GLTF_SEPARATE")
elif fmt == "glb":
    bpy.ops.export_scene.gltf(filepath=path, export_format="GLB")
elif fmt == "fbx":
    bpy.ops.export_scene.fbx(filepath=path)
elif fmt == "blend":
    bpy.ops.wm.save_as_mainfile(filepath=path)
else:
    bpy.ops.export_scene.obj(filepath=path, use_selection=False)

verts = 0
for o in created:
    if hasattr(o, "data") and hasattr(o.data, "vertices"):
        try:
            verts += len(o.data.vertices)
        except Exception:
            pass

result = {"path": path, "format": fmt, "vertices": verts, "objects": len(created)}
print("BLENDER_RESULT:" + json.dumps(result))
'''


class BlenderTool:
    """Адаптер для Blender."""

    def __init__(
        self,
        binary: Optional[str] = None,
        timeout: int = _DEFAULT_TIMEOUT,
        output_dir: Optional[str] = None,
        mock: bool = False,
    ) -> None:
        self.binary = (
            binary
            or os.environ.get("BLENDER_BIN")
            or os.environ.get("BLENDER_BINARY")
            or "/opt/blender-3/blender"
        )
        self.timeout = int(timeout)
        self.output_dir = Path(output_dir or _DEFAULT_OUTPUT_DIR)
        self.mock = bool(mock)
        self.connected = False
        self._fallback_to_mock = False
        self.last_command: Optional[List[str]] = None
        self.call_history: List[Dict[str, Any]] = []

    # ----------------------------- Подключение -----------------------------

    def _resolve_binary(self) -> Optional[str]:
        candidate = self.binary
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
        found = shutil.which(candidate) if candidate else None
        if found:
            return found
        for path in ("/opt/blender-3/blender", "/usr/local/bin/blender", "/usr/bin/blender"):
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return None

    def connect(self) -> bool:
        if self.mock:
            self.connected = True
            self._record("connect", {"mode": "mock"})
            return True

        binary = self._resolve_binary()
        if binary is None:
            self.mock = True
            self._fallback_to_mock = True
            self.connected = True
            self._record("connect", {"mode": "fallback-mock"})
            return True

        try:
            result = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(self.timeout, 60),
            )
            self.connected = result.returncode == 0
        except Exception:  # noqa: BLE001
            self.connected = False

        if not self.connected:
            self.mock = True
            self._fallback_to_mock = True
            self.connected = True
            self._record("connect", {"mode": "fallback-mock"})
            return True

        self._record("connect", {"mode": "real", "binary": binary})
        return True

    def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    def __enter__(self) -> "BlenderTool":
        self.connect()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.disconnect()

    # ----------------------------- Служебное -----------------------------

    def _record(self, action: str, payload: Dict[str, Any]) -> None:
        entry = {"action": action, "timestamp": time.time()}
        entry.update(payload)
        self.call_history.append(entry)

    @property
    def available_shapes(self) -> List[str]:
        return list(AVAILABLE_SHAPES)

    def info(self) -> Dict[str, Any]:
        binary = self._resolve_binary()
        version = ""
        if binary is not None:
            try:
                result = subprocess.run(
                    [binary, "--version"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                )
                lines = (result.stdout or "").splitlines()
                if lines:
                    version = lines[0].strip()
            except Exception:  # noqa: BLE001
                version = ""
        return {
            "binary": binary,
            "version": version,
            "output_dir": str(self.output_dir.resolve() if self.output_dir.exists() else self.output_dir),
            "mode": "mock" if (self.mock or self._fallback_to_mock) else "real",
            "connected": self.connected,
            "shapes": self.available_shapes,
            "formats": sorted(SUPPORTED_FORMATS),
        }

    def _clean_format(self, output_format: Optional[str], output_path: Optional[Path]) -> str:
        fmt = (output_format or "").strip().lower().lstrip(".")
        if output_path is not None and not fmt:
            fmt = output_path.suffix.lower().lstrip(".")
        if not fmt:
            fmt = "obj"
        if fmt == "gltf" and output_path is not None and output_path.suffix.lower() == ".glb":
            fmt = "glb"
        if fmt not in SUPPORTED_FORMATS:
            raise BlenderToolError(
                f"Формат {fmt!r} не поддерживается. Доступные: {sorted(SUPPORTED_FORMATS)}"
            )
        return fmt

    def _prepare_output_path(
        self,
        output_path: Optional[str],
        fmt: str,
        default_stem: str = "model",
    ) -> Path:
        if output_path:
            path = Path(output_path)
        else:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = self.output_dir / f"{default_stem}_{stamp}.{fmt}"
        if path.suffix == "":
            path = path.with_suffix(f".{fmt}")
        elif path.suffix.lower().lstrip(".") not in SUPPORTED_FORMATS:
            path = path.with_suffix(f".{fmt}")
        elif path.suffix.lower().lstrip(".") != fmt:
            # Явный формат имеет приоритет над расширением.
            path = path.with_suffix(f".{fmt}")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _build_script(self, payload: Dict[str, Any]) -> str:
        payload_text = repr(json.dumps(payload, ensure_ascii=False))
        return _BLENDER_SCRIPT_TEMPLATE.replace("PARAMS_PLACEHOLDER", payload_text)

    def _run_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.connected:
            self.connect()

        binary = self._resolve_binary()
        if binary is None:
            raise BlenderToolError("Бинарник Blender не найден.")

        fd, script_path = tempfile.mkstemp(prefix="blender_", suffix=".py")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(self._build_script(payload))
            command = [binary, "--background", "--python", script_path]
            self.last_command = command
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        finally:
            try:
                Path(script_path).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""

        parsed: Optional[Dict[str, Any]] = None
        for line in stdout.splitlines():
            if line.startswith("BLENDER_RESULT:"):
                try:
                    parsed = json.loads(line[len("BLENDER_RESULT:"):])
                    break
                except json.JSONDecodeError:
                    continue

        if completed.returncode != 0:
            message = stderr.strip() or stdout.strip() or "Blender завершился с ошибкой."
            raise BlenderToolError(
                f"Blender завершился с кодом {completed.returncode}: {message[:3000]}"
            )

        if parsed is None:
            parsed = {
                "path": payload.get("output_path"),
                "format": payload.get("output_format", "obj"),
                "vertices": 0,
                "objects": len(payload.get("objects", [])),
            }

        output_path = Path(parsed.get("path") or payload.get("output_path"))
        if not output_path.exists():
            fallback = Path(payload.get("output_path"))
            if fallback.exists():
                parsed["path"] = str(fallback)
                output_path = fallback
        if not output_path.exists():
            raise BlenderToolError(
                f"Blender не создал выходной файл. stdout:\n{stdout[:1000]}\nstderr:\n{stderr[:1000]}"
            )
        return parsed

    def _mock_generate(
        self,
        payload: Dict[str, Any],
        fmt: str,
        path: Path,
        object_count: int,
    ) -> Dict[str, Any]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# MOCK BLENDER OUTPUT\n"
            f"# format={fmt} objects={object_count}\n"
            f"# (Blender недоступен, создан заглушечный файл)\n",
            encoding="utf-8",
        )
        result = {
            "success": True,
            "mode": "mock",
            "format": fmt,
            "path": str(path.resolve()),
            "objects": object_count,
            "vertex_count": 0,
        }
        self.call_history.append({"action": "generate_scene", **result})
        self._record("generate_scene", result)
        return result

    # ----------------------------- Публичный API -----------------------------

    def generate_model(
        self,
        shape: str = "cube",
        size: Any = 2.0,
        color: str = "FFFFFF",
        output_path: Optional[str] = None,
        output_format: Optional[str] = None,
        hollow: bool = False,
        seed: int = 42,
        **shape_params: Any,
    ) -> Dict[str, Any]:
        """Генерирует одиночную 3D-модель."""
        if not self.connected:
            self.connect()

        spec: Dict[str, Any] = {
            "shape": shape or "cube",
            "size": size,
            "color": normalize_color(color),
            "hollow": bool(hollow),
        }
        # Полезные дополнительные параметры (scale, location, ...).
        for key in ("scale", "location", "rotation", "rotation_deg", "name", "text"):
            if key in shape_params:
                spec[key] = shape_params[key]
        spec.setdefault("seed", seed)

        objects = [spec]
        fmt = self._clean_format(output_format, Path(output_path) if output_path else None)
        path = self._prepare_output_path(
            output_path, fmt, default_stem=str(shape or "model").lower()
        )

        payload = {
            "objects": objects,
            "color": normalize_color(color),
            "output_format": fmt,
            "output_path": str(path),
        }

        if self.mock or self._fallback_to_mock or self._resolve_binary() is None:
            return self._mock_generate(payload, fmt, path, len(objects))

        result_payload = self._run_payload(payload)
        result = {
            "success": True,
            "mode": "real",
            "shape": shape,
            "size": size,
            "format": result_payload.get("format", fmt),
            "path": str(Path(result_payload.get("path", path)).resolve()),
            "objects": result_payload.get("objects", len(objects)),
            "vertex_count": result_payload.get("vertices", 0),
        }
        self.call_history.append({"action": "generate_model", **result})
        self._record("generate_model", result)
        return result

    def generate_scene(
        self,
        shapes: Sequence[Any],
        output_path: Optional[str] = None,
        output_format: Optional[str] = None,
        color: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Генерирует сцену из списка спецификаций фигур."""
        if not self.connected:
            self.connect()

        if isinstance(shapes, str):
            import json as _json
            shapes = _json.loads(shapes)
        if not isinstance(shapes, (list, tuple)):
            raise BlenderToolError("shapes должен быть списком спецификаций.")

        objects: List[Dict[str, Any]] = []
        for raw in shapes:
            spec = dict(raw or {})
            if "color" in spec:
                spec["color"] = normalize_color(spec["color"])
            elif color is not None:
                spec["color"] = normalize_color(color)
            else:
                spec.setdefault("color", "FFFFFF")
            spec.setdefault("shape", "cube")
            spec.setdefault("size", 2.0)
            objects.append(spec)

        if not objects:
            raise BlenderToolError("Список фигур пуст.")

        fmt = self._clean_format(
            output_format, Path(output_path) if output_path else None
        )
        default_stem = str(objects[0].get("shape", "scene")).lower()
        path = self._prepare_output_path(output_path, fmt, default_stem=default_stem)

        payload = {
            "objects": objects,
            "color": normalize_color(color or "FFFFFF"),
            "output_format": fmt,
            "output_path": str(path),
        }

        if self.mock or self._fallback_to_mock or self._resolve_binary() is None:
            return self._mock_generate(payload, fmt, path, len(objects))

        result_payload = self._run_payload(payload)
        result = {
            "success": True,
            "mode": "real",
            "format": result_payload.get("format", fmt),
            "path": str(Path(result_payload.get("path", path)).resolve()),
            "objects": result_payload.get("objects", len(objects)),
            "vertex_count": result_payload.get("vertices", 0),
        }
        self.call_history.append({"action": "generate_scene", **result})
        self._record("generate_scene", result)
        return result

    def run_python(self, script: str, timeout: Optional[int] = None) -> str:
        """Выполняет произвольный Python-скрипт внутри Blender."""
        if not self.connected:
            self.connect()
        binary = self._resolve_binary()
        if binary is None:
            return "Blender недоступен (режим mock)."

        fd, script_path = tempfile.mkstemp(prefix="blender_run_", suffix=".py")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(script)
            command = [binary, "--background", "--python", script_path]
            self.last_command = command
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=int(timeout) if timeout else self.timeout,
            )
        finally:
            try:
                Path(script_path).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            body = stderr or stdout or "вывод пуст"
            return f"Ошибка выполнения (код {completed.returncode}):\n{body[:4000]}"
        parts = []
        if stdout:
            # Убираем шумный прогресс Blender, оставляем полезный вывод?
            # Оставляем как есть, но обрезаем.
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        answer = "\n".join(parts) if parts else "Скрипт выполнен успешно (вывод пуст)."
        if len(answer) > 80000:
            answer = answer[:80000] + "\n... [вывод обрезан]"
        return answer



# Псевдонимы для совместимости с разными ожидаемыми именами модулей/классов.
BlenderMock = BlenderTool
BlenderAdapter = BlenderTool
BlenderClient = BlenderTool


# Простые удобные функции (как в Goxel-adapter-стиле).

def _default_client() -> BlenderTool:
    client = BlenderTool(mock=False)
    try:
        client.connect()
    except Exception:  # noqa: BLE001
        pass
    return client


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Генерация 3D-модели через Blender")
    parser.add_argument("shape", nargs="?", default="cube", help="Форма модели")
    parser.add_argument("--size", type=float, default=2.0, help="Размер")
    parser.add_argument("--color", default="FFFFFF", help="Цвет")
    parser.add_argument("--format", default="obj", dest="output_format", help="Формат экспорта")
    parser.add_argument("--output", default="", dest="output_path", help="Путь к файлу")
    parser.add_argument("--hollow", action="store_true", help="Сделать полой")
    args = parser.parse_args()

    client = BlenderTool(mock=False)
    client.connect()
    result = client.generate_model(
        shape=args.shape,
        size=args.size,
        color=args.color,
        output_format=args.output_format,
        output_path=args.output_path or None,
        hollow=args.hollow,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
