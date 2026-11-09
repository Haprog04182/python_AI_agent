#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Улучшенный агент с инструментами на базе Ollama.

Улучшения по сравнению с предыдущей версией:
- конфигурация через переменные окружения;
- безопасный калькулятор на базе AST (без eval);
- реестр инструментов с декоратором;
- валидация и безопасное выполнение инструментов;
- защита от бесконечного цикла шагов агента;
- повторные попытки при ошибках Ollama;
- команды управления диалогом (очистка, история, сохранение);
- корректная обработка Ctrl+C / EOF;
- обрезка длинного вывода команд;
- логирование и аккуратное форматирование вывода.
"""

import ast
import json
import logging
import math
import operator
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ollama import Client

try:
    from goxel_mock import GoxelMock
except Exception:  # noqa: BLE001
    GoxelMock = None

try:
    from blender_tool import BlenderTool
except Exception:  # noqa: BLE001
    BlenderTool = None


# ----------------------------- Конфигурация -----------------------------

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "192.168.15.230:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "beam:cloud")

BASH_TIMEOUT = int(os.environ.get("BASH_TIMEOUT", "30"))
MAX_OUTPUT_CHARS = int(os.environ.get("MAX_OUTPUT_CHARS", "80000"))
MAX_AGENT_STEPS = int(os.environ.get("MAX_AGENT_STEPS", "1200"))
MAX_HISTORY_MESSAGES = int(os.environ.get("MAX_HISTORY_MESSAGES", "500"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
RETRY_DELAY = float(os.environ.get("RETRY_DELAY", "1.0"))

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "Ты полезный ассистент. Используй инструменты, если нужна "
    "точная информация (время, расчёт, команды). Можешь генерировать 3D-модели "
    "через Goxel инструментами generate_3d_model / generate_3d_scene, а также "
    "через Blender инструментами blender_generate_model / blender_generate_scene "
    "/ blender_run_python. "
    "Отвечай по-русски. "
    "учти, что ЗАПРЕЩЕННО пользоваться рут правами",

)

EXIT_COMMANDS = {"выход", "exit", "quit", "q"}
CLEAR_COMMANDS = {"очистить", "clear", "reset"}
HELP_COMMANDS = {"помощь", "help", "?"}
TOOLS_COMMANDS = {"инструменты", "tools"}

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("tool_agent.log", encoding="utf-8")],
)
logger = logging.getLogger("tool_agent")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ----------------------------- Безопасный калькулятор -----------------------------

_BIN_OPS: Dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS: Dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_MATH_CONSTANTS: Dict[str, Any] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
}

_MATH_FUNCTIONS: Dict[str, Callable[..., Any]] = {
    name: getattr(math, name)
    for name in dir(math)
    if not name.startswith("_") and callable(getattr(math, name))
}
_MATH_FUNCTIONS.update(
    {
        "abs": abs,
        "round": round,
        "min": min,
        "max": max,
        "sum": sum,
        "int": int,
        "float": float,
    }
)


def _eval_ast(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, complex)):
            return node.value
        raise ValueError("недопустимая константа")

    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _BIN_OPS:
            raise ValueError("недопустимая арифметическая операция")
        left = _eval_ast(node.left)
        right = _eval_ast(node.right)
        if op_type is ast.Pow:
            if not isinstance(right, (int, float)) or abs(right) > 1000:
                raise ValueError("слишком большой показатель степени")
        return _BIN_OPS[op_type](left, right)

    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _UNARY_OPS:
            raise ValueError("недопустимая унарная операция")
        return _UNARY_OPS[op_type](_eval_ast(node.operand))

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("недопустимый вызов функции")
        func_name = node.func.id
        if func_name not in _MATH_FUNCTIONS:
            raise ValueError(f"неизвестная функция: {func_name}")
        if node.keywords:
            raise ValueError("ключевые аргументы не поддерживаются")
        arguments = [_eval_ast(arg) for arg in node.args]
        try:
            return _MATH_FUNCTIONS[func_name](*arguments)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(str(exc)) from exc

    if isinstance(node, ast.Name):
        if node.id in _MATH_CONSTANTS:
            return _MATH_CONSTANTS[node.id]
        raise ValueError(f"неизвестное имя: {node.id}")

    raise ValueError("недопустимое выражение")


def safe_eval(expression: str) -> Any:
    """Безопасно вычисляет математическое выражение."""
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("выражение не задано")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"синтаксическая ошибка: {exc}") from exc
    return _eval_ast(tree.body)


# ----------------------------- Реестр инструментов -----------------------------

ToolFunc = Callable[..., str]

_TOOLS: Dict[str, ToolFunc] = {}
_TOOL_SCHEMAS: List[dict] = []


def register_tool(name: str, description: str, parameters: dict) -> Callable[[ToolFunc], ToolFunc]:
    """Декоратор для регистрации инструмента и его JSON-схемы."""

    def decorator(func: ToolFunc) -> ToolFunc:
        if name in _TOOLS:
            raise ValueError(f"Инструмент '{name}' уже зарегистрирован")
        _TOOLS[name] = func
        _TOOL_SCHEMAS.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            }
        )
        logger.debug("Зарегистрирован инструмент: %s", name)
        return func

    return decorator


@register_tool(
    "get_current_time",
    description="Возвращает текущие дату и время сервера.",
    parameters={"type": "object", "properties": {}, "required": []},
)
def get_current_time() -> str:
    """Текущие дата и время."""
    now = datetime.now().astimezone()
    return now.strftime("%Y-%m-%d %H:%M:%S (%A)")


@register_tool(
    "calculator",
    description="Вычисляет математическое выражение, например '2 + 2 * 3' или 'sqrt(16)'.",
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Математическое выражение для вычисления",
            }
        },
        "required": ["expression"],
    },
)
def calculator(expression: str) -> str:
    """Безопасный калькулятор."""
    try:
        result = safe_eval(expression)
    except Exception as exc:  # noqa: BLE001
        return f"Ошибка: {exc}"

    # Делаем вывод аккуратнее: целые числа без ".0".
    if isinstance(result, float) and result.is_integer():
        result = int(result)
    return str(result)


@register_tool(
    "bash_exec",
    description="Выполняет bash-команду и возвращает её вывод (stdout) и/или ошибку (stderr).",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Текст bash-команды для выполнения",
            }
        },
        "required": ["command"],
    },
)
def bash_exec(command: str) -> str:
    """Выполняет bash-команду с защитой от зависания и обрезкой вывода."""
    if not isinstance(command, str) or not command.strip():
        return "Ошибка: команда не задана."

    cmd = command.strip()
    logger.info("bash_exec: %s", cmd)

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=BASH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"Ошибка: превышено время ожидания ({BASH_TIMEOUT} сек)."
    except Exception as exc:  # noqa: BLE001
        logger.exception("bash_exec failed")
        return f"Непредвиденная ошибка: {exc}"

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()

    if result.returncode != 0:
        body = stderr or stdout or "вывод пуст"
        answer = f"Ошибка выполнения (код {result.returncode}):\n{body}"
    else:
        parts = []
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"[stderr]\n{stderr}")
        answer = "\n".join(parts) if parts else "Команда выполнена успешно (вывод пуст)."

    if len(answer) > MAX_OUTPUT_CHARS:
        answer = answer[:MAX_OUTPUT_CHARS] + "\n... [вывод обрезан]"
    return answer


# ----------------------------- Goxel (3D-модели) -----------------------------

_GOXEL_CLIENT = None


def _get_goxel_client():
    """Ленивая инициализация клиента Goxel."""
    global _GOXEL_CLIENT
    if _GOXEL_CLIENT is None and GoxelMock is not None:
        _GOXEL_CLIENT = GoxelMock(mock=False)
        try:
            _GOXEL_CLIENT.connect()
        except Exception:  # noqa: BLE001
            _GOXEL_CLIENT = None
    return _GOXEL_CLIENT


if GoxelMock is not None:
    @register_tool(
        "list_3d_shapes",
        description="Возвращает список 3D-форм, которые можно сгенерировать через Goxel.",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    def list_3d_shapes() -> str:
        client = _get_goxel_client()
        if client is None:
            return "Goxel недоступен."
        return "Доступные формы: " + ", ".join(client.available_shapes())

    @register_tool(
        "generate_3d_model",
        description=(
            "Генерирует 3D-модель из вокселей через Goxel и сохраняет её "
            "в указанный формат (obj, vox, ply, qb, txt)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "shape": {
                    "type": "string",
                    "description": "Форма модели: cube, sphere, pyramid, cylinder, cone, torus, diamond, heart, cross, spiral, random.",
                },
                "size": {
                    "type": "integer",
                    "description": "Размер модели в вокселях (оси), по умолчанию 8.",
                },
                "color": {
                    "type": "string",
                    "description": "Цвет в HEX (#RRGGBB) или имя (red, blue...), по умолчанию белый.",
                },
                "output_format": {
                    "type": "string",
                    "description": "Формат экспорта: obj, vox, ply, qb, txt.",
                },
                "output_path": {
                    "type": "string",
                    "description": "Путь к выходному файлу (необязательно).",
                },
                "hollow": {
                    "type": "boolean",
                    "description": "Сделать фигуру полой, по умолчанию false.",
                },
                "seed": {
                    "type": "integer",
                    "description": "Зерно для random-фигур, по умолчанию 42.",
                },
            },
            "required": [],
        },
    )
    def generate_3d_model(
        shape: str = "cube",
        size: int = 8,
        color: str = "FFFFFF",
        output_format: str = "obj",
        output_path: str = "",
        hollow: bool = False,
        seed: int = 42,
    ) -> str:
        client = _get_goxel_client()
        if client is None:
            return "Ошибка: Goxel/GoxelMock недоступен."
        try:
            result = client.generate_model(
                shape=shape or "cube",
                size=size,
                color=color,
                output_path=output_path or None,
                output_format=output_format or "obj",
                hollow=bool(hollow),
                seed=seed,
            )
        except Exception as exc:  # noqa: BLE001
            return f"Ошибка генерации 3D-модели: {exc}"
        return (
            f"3D-модель создана: {result['path']} "
            f"(формат {result['format']}, вокселей {result['voxel_count']}, режим {result['mode']})"
        )

    @register_tool(
        "generate_3d_scene",
        description="Собирает сцену из нескольких 3D-фигур и экспортирует через Goxel.",
        parameters={
            "type": "object",
            "properties": {
                "shapes": {
                    "type": "string",
                    "description": (
                        "JSON-список фигур. Каждый элемент: "
                        '{"shape":"cube","size":6,"color":"red","offset_x":0,...}'
                    ),
                },
                "output_format": {"type": "string", "description": "Формат экспорта."},
                "output_path": {"type": "string", "description": "Путь к выходному файлу."},
            },
            "required": ["shapes"],
        },
    )
    def generate_3d_scene(
        shapes: str,
        output_format: str = "obj",
        output_path: str = "",
    ) -> str:
        client = _get_goxel_client()
        if client is None:
            return "Ошибка: Goxel/GoxelMock недоступен."
        try:
            import json as _json
            specs = _json.loads(shapes)
            if not isinstance(specs, list):
                raise ValueError("shapes должен быть списком")
            result = client.generate_scene(
                shapes=specs,
                output_path=output_path or None,
                output_format=output_format or "obj",
            )
        except Exception as exc:  # noqa: BLE001
            return f"Ошибка генерации сцены: {exc}"
        return (
            f"3D-сцена создана: {result['path']} "
            f"(формат {result['format']}, вокселей {result['voxel_count']})"
        )


# ----------------------------- Агент -----------------------------


# ----------------------------- Blender (3D-модели) -----------------------------

_BLENDER_CLIENT = None


def _get_blender_client():
    """Ленивая инициализация клиента Blender."""
    global _BLENDER_CLIENT
    if _BLENDER_CLIENT is None and BlenderTool is not None:
        _BLENDER_CLIENT = BlenderTool(mock=False)
        try:
            _BLENDER_CLIENT.connect()
        except Exception:  # noqa: BLE001
            _BLENDER_CLIENT = None
    return _BLENDER_CLIENT


if BlenderTool is not None:
    @register_tool(
        "blender_info",
        description="Возвращает информацию о доступном Blender (версия, путь, форматы, формы).",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    def blender_info() -> str:
        client = _get_blender_client()
        if client is None:
            return "Blender недоступен."
        info = client.info()
        return (
            f"Blender: {info.get('version') or 'unknown'}\n"
            f"путь: {info.get('binary')}\n"
            f"режим: {info.get('mode')}\n"
            f"форматы: {', '.join(info.get('formats', []))}\n"
            f"формы: {', '.join(info.get('shapes', []))}"
        )

    @register_tool(
        "list_blender_shapes",
        description="Возвращает список 3D-форм, доступных в Blender.",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    def list_blender_shapes() -> str:
        client = _get_blender_client()
        if client is None:
            return "Blender недоступен."
        return "Доступные формы Blender: " + ", ".join(client.available_shapes)

    @register_tool(
        "blender_generate_model",
        description=(
            "Генерирует 3D-модель через Blender (безголовый режим) и сохраняет её "
            "в указанный формат (obj, stl, ply, gltf, glb, fbx, blend). "
            "Поддерживает примитивы Blender и материалы."
        ),
        parameters={
            "type": "object",
            "properties": {
                "shape": {
                    "type": "string",
                    "description": (
                        "Форма модели: cube, sphere, uv_sphere, ico_sphere, cylinder, "
                        "cone, torus, plane, monkey, grid, circle, text."
                    ),
                },
                "size": {
                    "type": "number",
                    "description": "Размер модели в единицах Blender, по умолчанию 2.",
                },
                "color": {
                    "type": "string",
                    "description": "Цвет в HEX (#RRGGBB) или имя (red, blue...), по умолчанию белый.",
                },
                "output_format": {
                    "type": "string",
                    "description": "Формат экспорта: obj, stl, ply, gltf, glb, fbx, blend.",
                },
                "output_path": {
                    "type": "string",
                    "description": "Путь к выходному файлу (необязательно).",
                },
                "hollow": {
                    "type": "boolean",
                    "description": "Сделать фигуру полой, по умолчанию false.",
                },
                "seed": {
                    "type": "integer",
                    "description": "Зарезервировано для совместимости, по умолчанию 42.",
                },
                "location": {
                    "type": "array",
                    "description": "Позиция [x, y, z] (необязательно).",
                },
                "rotation_deg": {
                    "type": "array",
                    "description": "Вращение в градусах [x, y, z] (необязательно).",
                },
                "scale": {
                    "type": "number",
                    "description": "Дополнительный масштаб, по умолчанию 1.",
                },
                "text": {
                    "type": "string",
                    "description": "Текст для формы text.",
                },
            },
            "required": [],
        },
    )
    def blender_generate_model(
        shape: str = "cube",
        size: Any = 2.0,
        color: str = "FFFFFF",
        output_format: str = "obj",
        output_path: str = "",
        hollow: bool = False,
        seed: int = 42,
        location: Any = None,
        rotation_deg: Any = None,
        scale: Any = None,
        text: str = "",
    ) -> str:
        client = _get_blender_client()
        if client is None:
            return "Ошибка: Blender недоступен."
        kwargs = {}
        if location is not None:
            kwargs["location"] = location
        if rotation_deg is not None:
            kwargs["rotation_deg"] = rotation_deg
        if scale is not None:
            kwargs["scale"] = scale
        if text:
            kwargs["text"] = text
        try:
            result = client.generate_model(
                shape=shape or "cube",
                size=size,
                color=color,
                output_path=output_path or None,
                output_format=output_format or "obj",
                hollow=bool(hollow),
                seed=seed,
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            return f"Ошибка генерации 3D-модели через Blender: {exc}"
        return (
            f"3D-модель создана через Blender: {result['path']} "
            f"(формат {result['format']}, объектов {result['objects']}, "
            f"вершин {result['vertex_count']}, режим {result['mode']})"
        )

    @register_tool(
        "blender_generate_scene",
        description=(
            "Создаёт сцену из нескольких объектов через Blender и экспортирует её "
            "в указанный формат (obj, stl, ply, gltf, glb, fbx, blend). "
            "shapes — JSON-список фигур: "
            '[{"shape":"cube","size":2,"color":"red","location":[0,0,0],...}].'
        ),
        parameters={
            "type": "object",
            "properties": {
                "shapes": {
                    "type": "string",
                    "description": "JSON-список фигур Blender.",
                },
                "output_format": {"type": "string", "description": "Формат экспорта."},
                "output_path": {"type": "string", "description": "Путь к выходному файлу."},
            },
            "required": ["shapes"],
        },
    )
    def blender_generate_scene(
        shapes: str,
        output_format: str = "obj",
        output_path: str = "",
    ) -> str:
        client = _get_blender_client()
        if client is None:
            return "Ошибка: Blender недоступен."
        try:
            import json as _json
            specs = _json.loads(shapes)
            if not isinstance(specs, list):
                raise ValueError("shapes должен быть списком")
            result = client.generate_scene(
                shapes=specs,
                output_path=output_path or None,
                output_format=output_format or "obj",
            )
        except Exception as exc:  # noqa: BLE001
            return f"Ошибка генерации сцены через Blender: {exc}"
        return (
            f"3D-сцена создана через Blender: {result['path']} "
            f"(формат {result['format']}, объектов {result['objects']}, "
            f"вершин {result['vertex_count']}, режим {result['mode']})"
        )

    @register_tool(
        "blender_run_python",
        description=(
            "Выполняет произвольный Python-скрипт внутри Blender (background mode) "
            "и возвращает его вывод. Используется для сложной генерации/обработки моделей."
        ),
        parameters={
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "Python-код для выполнения в Blender (bpy доступен).",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Таймаут в секундах (необязательно).",
                },
            },
            "required": ["script"],
        },
    )
    def blender_run_python(script: str, timeout: int = 0) -> str:
        client = _get_blender_client()
        if client is None:
            return "Ошибка: Blender недоступен."
        try:
            return client.run_python(script, timeout=timeout or None)
        except Exception as exc:  # noqa: BLE001
            return f"Ошибка выполнения скрипта в Blender: {exc}"




class ToolAgent:
    """Управляет диалогом, вызовами инструментов и историей сообщений."""

    def __init__(
        self,
        client: Client,
        model: str,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self.client = client
        self.model = model
        self.system_prompt = system_prompt
        self.messages: List[dict] = [{"role": "system", "content": system_prompt}]

    # ---------- Низкоуровневое взаимодействие ----------

    def _chat_with_retry(self) -> Any:
        last_exc: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return self.client.chat(
                    model=self.model,
                    messages=self.messages,
                    tools=_TOOL_SCHEMAS,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("Ошибка обращения к модели (попытка %s/%s): %s", attempt, MAX_RETRIES, exc)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
        assert last_exc is not None
        raise last_exc

    # ---------- Управление историей ----------

    def _trim_history(self) -> None:
        """Ограничивает размер истории, сохраняя системное сообщение и целостность диалога."""
        if len(self.messages) <= MAX_HISTORY_MESSAGES + 1:
            return
        kept = self.messages[-(MAX_HISTORY_MESSAGES):]
        # Не начинаем «обрезанный» диалог с сообщения инструмента: ищем ближайшее
        # сообщение пользователя, чтобы не сломать последовательность tool_call -> tool.
        start = 0
        while start < len(kept) and kept[start].get("role") != "user":
            start += 1
        if start > 0:
            kept = kept[start:]
        self.messages = [self.messages[0]] + kept
        logger.debug("История обрезана до %s сообщений", len(self.messages))

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self.system_prompt}]

    def history_text(self) -> str:
        lines = []
        for msg in self.messages[1:]:
            role = msg.get("role")
            content = msg.get("content")
            if role == "user" and content:
                lines.append(f"Вы: {content}")
            elif role == "assistant" and content:
                lines.append(f"Агент: {content}")
        return "\n".join(lines) if lines else "(история пуста)"

    def save_history(self, path: str = "history.json") -> str:
        try:
            target = Path(path)
            with target.open("w", encoding="utf-8") as stream:
                json.dump(self.messages, stream, ensure_ascii=False, indent=2)
            return f"История сохранена в {target.resolve()}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("Не удалось сохранить историю")
            return f"Ошибка сохранения истории: {exc}"

    # ---------- Основной цикл агента ----------

    def handle_user_message(self, user_input: str) -> str:
        self.messages.append({"role": "user", "content": user_input})

        for step in range(1, MAX_AGENT_STEPS + 1):
            self._trim_history()

            try:
                resp = self._chat_with_retry()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Сбой при обращении к модели")
                return f"Ошибка обращения к модели: {exc}"

            assistant_msg = resp.get("message") if isinstance(resp, dict) else getattr(resp, "message", None)
            if assistant_msg is None:
                return "Модель вернула пустой ответ."

            # Аккуратно приводим сообщение ассистента к словарю (включая вложенные объекты).
            if hasattr(assistant_msg, "model_dump"):
                assistant_msg = assistant_msg.model_dump()
            elif not isinstance(assistant_msg, dict):
                try:
                    assistant_msg = dict(assistant_msg)
                except Exception:  # noqa: BLE001
                    assistant_msg = {"role": "assistant", "content": str(assistant_msg)}

            self.messages.append(assistant_msg)

            tool_calls = assistant_msg.get("tool_calls") or []
            if not tool_calls:
                return assistant_msg.get("content", "") or ""

            for tc in tool_calls:
                # Поддерживаем и dict-, и object-представление вызова инструмента.
                if isinstance(tc, dict):
                    tc_id = tc.get("id")
                    fn_info = tc.get("function", {})
                else:
                    tc_id = getattr(tc, "id", None)
                    fn_info = getattr(tc, "function", {})

                if hasattr(fn_info, "model_dump"):
                    fn_info = fn_info.model_dump()
                elif not isinstance(fn_info, dict):
                    try:
                        fn_info = dict(fn_info)
                    except Exception:  # noqa: BLE001
                        fn_info = {}

                if not fn_info and isinstance(tc, dict) and "name" in tc:
                    fn_info = tc

                name = fn_info.get("name") if isinstance(fn_info, dict) else getattr(fn_info, "name", None)
                args = fn_info.get("arguments", {}) if isinstance(fn_info, dict) else getattr(fn_info, "arguments", {})

                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                if not isinstance(args, dict):
                    args = {}

                if name not in _TOOLS:
                    result = f"Неизвестный инструмент: {name}"
                else:
                    print(f"  -> вызываю {name}({args})")
                    try:
                        result = _TOOLS[name](**args)
                    except TypeError as exc:
                        result = f"Ошибка аргументов инструмента '{name}': {exc}"
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Инструмент '%s' завершился ошибкой", name)
                        result = f"Ошибка инструмента '{name}': {exc}"
                print(f"  <- {result}")

                tool_message: Dict[str, Any] = {"role": "tool", "content": str(result)}
                if name:
                    tool_message["name"] = name
                if tc_id:
                    tool_message["tool_call_id"] = tc_id
                self.messages.append(tool_message)

        logger.warning("Достигнут лимит шагов агента (%s)", MAX_AGENT_STEPS)
        return "Достигнут лимит шагов агента. Попробуйте переформулировать запрос."

    # ---------- Интерактивный запуск ----------

    def run(self) -> None:
        print("Агент с инструментами запущен. Выход — 'выход'.")
        print("Команды: помощь, очистить, история, инструменты, сохранить [файл].\n")

        while True:
            try:
                user_input = input("Вы: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nЗавершение работы.")
                break

            if not user_input:
                continue

            lowered = user_input.lower()

            if lowered in EXIT_COMMANDS:
                print("Завершение работы.")
                break

            if lowered in CLEAR_COMMANDS:
                self.reset()
                print("История очищена.\n")
                continue

            if lowered in HELP_COMMANDS:
                print(
                    "Доступные команды:\n"
                    "  выход / exit / quit — завершить работу\n"
                    "  очистить / clear    — очистить историю диалога\n"
                    "  история / history   — показать историю диалога\n"
                    "  инструменты / tools — список доступных инструментов\n"
                    "  сохранить [файл]     — сохранить историю в JSON (по умолч. history.json)\n"
                    "  помощь / help        — показать эту справку\n"
                )
                continue

            if lowered in TOOLS_COMMANDS:
                print("Доступные инструменты: " + ", ".join(_TOOLS.keys()) + "\n")
                continue

            if lowered in {"история", "history"}:
                print(self.history_text() + "\n")
                continue

            if lowered.startswith("сохранить") or lowered.startswith("save"):
                parts = user_input.split(maxsplit=1)
                path = parts[1].strip() if len(parts) > 1 else "history.json"
                print(self.save_history(path) + "\n")
                continue

            try:
                answer = self.handle_user_message(user_input)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Неожиданная ошибка обработки сообщения")
                answer = f"Неожиданная ошибка: {exc}"

            print(f"Агент: {answer}\n")


def _create_client() -> Client:
    headers = None
    raw_headers = os.environ.get("OLLAMA_HEADERS")
    if raw_headers:
        try:
            headers = json.loads(raw_headers)
            if not isinstance(headers, dict):
                headers = None
        except json.JSONDecodeError:
            logger.warning("OLLAMA_HEADERS не удалось разобрать как JSON.")
    return Client(host=OLLAMA_HOST, headers=headers)


def run_agent() -> None:
    client = _create_client()
    agent = ToolAgent(client=client, model=MODEL)
    agent.run()


if __name__ == "__main__":
    run_agent()
