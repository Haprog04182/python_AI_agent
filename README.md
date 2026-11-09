# python_AI_agent

Просто ИИ агент для ollama(локальных моделей). 
Написан с помощью этого самого ollama. 
подключены возможности:
* взаимодействия с terminal
* с goxel
* и есть методика ТРИЗ для усиления работы.

## Веб-версия агента

Запуск:
```bash
./run_web.sh
```
По умолчанию сервер доступен на `http://127.0.0.1:8080`.
Переменные окружения: `WEB_HOST`, `WEB_PORT`, а также все переменные из `tool_agent.py` (`OLLAMA_HOST`, `OLLAMA_MODEL` и т.д.).

⚠️ Веб-версия даёт агенту те же возможности, включая `bash_exec`. Запускайте только в доверенной среде и никогда не выставляйте наружу без авторизации.

## Blender (3D-модели)

В проект добавлен адаптер `blender_tool.py` (и совместимые модули `blender_mock.py`, `blender_adapter.py`, `blender_client.py`) для безголового Blender:

- `BlenderTool` / `BlenderMock` — класс с методами `connect()`, `info()`, `generate_model()`, `generate_scene()`, `run_python()`;
- CLI: `python blender_tool.py cube --format glb --size 2.0`;
- Инструменты агента: `blender_info`, `list_blender_shapes`, `blender_generate_model`, `blender_generate_scene`, `blender_run_python`.

Требуется установленный Blender (по умолчанию `/opt/blender-3/blender`, можно указать через `BLENDER_BIN`).
